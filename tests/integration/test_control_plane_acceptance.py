"""Reproducible control-plane acceptance on the native integration cluster."""

import asyncio
import json
import os
import signal
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4

import psycopg
import pytest
from test_billing import account_amounts, funding, headers, post_funding
from test_catalog import make_policy
from test_identity_api import auth, create_org
from test_plane import list_keys_in_manifests, plane, register_node  # noqa: F401
from test_project_credentials import account, key, project

from nexus.gateway_simulator import GatewaySimulator

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


@asynccontextmanager
async def worker(directory, redis, namespace, *, mock_url=None):
    name = "acceptance-" + uuid4().hex
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "nexus.cli",
        "--env-file",
        str(directory / ".env.worker"),
        "worker",
        env=dict(
            os.environ,
            NEXUS_WORKER_ID=name,
            NEXUS_REDIS_NAMESPACE=namespace,
            **({"NEXUS_MOCK_PAYMENT_URL": mock_url} if mock_url else {}),
            NEXUS_WORKER_INTERVAL="0.25",
        ),
        stdout=asyncio.subprocess.DEVNULL,
    )
    heartbeat = f"{namespace}:workers:{name}"
    try:
        async with asyncio.timeout(15):
            while not json.loads(await redis.get(heartbeat) or "{}").get("ready"):
                assert process.returncode is None
                await asyncio.sleep(0.05)
        yield process, heartbeat
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                process.kill()
                await process.wait()


async def send_usage(peer, rid, attempt, lease):
    for event, payload, aid in [
        (
            "attempt.finished",
            {
                "outcome": "succeeded",
                "upstream_request_id": str(attempt),
                "elapsed_ms": 1,
                "usage_format": "fixture.tokens.v1",
                "raw_usage": {
                    "input_total_tokens": 1,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                },
            },
            attempt,
        ),
        (
            "request.closed",
            {
                "delivery_outcome": "succeeded",
                "delivered_attempt_id": str(attempt),
                "attempt_ids": [str(attempt)],
            },
            None,
        ),
    ]:
        message = peer.prepare_event(rid, event, payload, attempt_id=aid)
        result = await peer.call("append_usage_event", message)
        assert result["status"] == "accepted"
    assert await peer.limits.finish(lease, 2)


async def submit(peer, raw, model, deployment, rid):
    _, lease = await peer.reserve(raw, model, 1, 1, rid)
    aid = uuid4()
    await peer.start_attempt(rid, aid, 1, deployment, lease)
    await send_usage(peer, rid, aid, lease)


async def drain(admin, ids):
    deadline = monotonic() + 20
    while monotonic() < deadline:
        count = admin.execute(
            "SELECT count(*) FROM billing.usage_jobs WHERE request_id=ANY(%s) AND state='done'",
            (ids,),
        ).fetchone()[0]
        if count == len(ids):
            return
        await asyncio.sleep(0.05)


def profile_results(admin, ids):
    # xmin belongs to the immutable inbox/ledger insert. These are the two commit times,
    # including lock waits and commit latency, not application timestamps sampled before commit.
    row = admin.execute(
        """
        WITH sample AS (
          SELECT id FROM unnest(%s::uuid[]) AS input(id)
        ), facts AS (
          SELECT s.id, j.state, count(DISTINCT l.id) AS debits,
            max(pg_xact_commit_timestamp(l.xmin)) - max(pg_xact_commit_timestamp(e.xmin)) AS lag
          FROM sample s LEFT JOIN billing.usage_jobs j ON j.request_id=s.id
          LEFT JOIN billing.usage_events e ON e.request_id=s.id
          LEFT JOIN billing.ledger_entries l ON l.source_id=s.id AND l.kind='usage_debit'
          GROUP BY s.id,j.state
        )
        SELECT count(*) FILTER (WHERE state IS DISTINCT FROM 'done'),
          count(*) FILTER (WHERE state='review'),count(*) FILTER (WHERE debits<>1 OR lag IS NULL),
          percentile_disc(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM lag)),
          percentile_disc(0.99) WITHIN GROUP (ORDER BY extract(epoch FROM lag))
        FROM facts
    """,
        (ids,),
    ).fetchone()
    return dict(
        zip(
            ("unfinished", "review", "missing_or_duplicate_debit", "p95_s", "p99_s"),
            row,
            strict=True,
        )
    )


async def sample_queues(admin, stop, peaks):
    while not stop.is_set():
        row = admin.execute("""
            SELECT (SELECT count(*) FROM billing.usage_jobs WHERE state='ready'),
              (SELECT count(*) FROM billing.request_runs WHERE reservation_state='held'),
              (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()
                 AND wait_event_type='Lock')
        """).fetchone()
        for name, count in zip(("ready", "held", "lock_waiters"), row, strict=True):
            peaks[name] = max(peaks.get(name, 0), count)
        await asyncio.sleep(0.05)


async def load_phase(admin, nodes, keys, model, deployment, total, rate):
    ids = [uuid4() for _ in range(total)]
    slots = asyncio.Semaphore(16)
    started = monotonic()
    stop = asyncio.Event()
    peaks = {}
    monitor = asyncio.create_task(sample_queues(admin, stop, peaks))

    async def invoke(index, rid):
        async with slots, asyncio.timeout(10):
            await submit(nodes[index % len(nodes)], keys[index % len(keys)], model, deployment, rid)

    try:
        tasks = []
        for index, rid in enumerate(ids):
            if rate:
                await asyncio.sleep(max(0, started + index / rate - monotonic()))
            tasks.append(asyncio.create_task(invoke(index, rid)))
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = monotonic() - started
        await drain(admin, ids)
    finally:
        stop.set()
        await monitor
    report = profile_results(admin, ids)
    report.update(
        attempted=total,
        submit_rate_target=rate,
        timeout_seconds=10,
        ingress_seconds=round(elapsed, 3),
        requests_per_second=round(total / elapsed, 2),
        errors=sum(isinstance(e, BaseException) for e in outcomes),
        timeouts=sum(isinstance(e, TimeoutError) for e in outcomes),
        peaks=peaks,
    )
    print("capacity " + json.dumps(report, default=str))
    assert not any(
        report[k]
        for k in ("errors", "timeouts", "unfinished", "review", "missing_or_duplicate_debit")
    ), report
    assert report["p95_s"] <= 2 and report["p99_s"] <= 5, report


@pytest.mark.capacity
async def test_settlement_capacity(plane, http, admin, service_directory):  # noqa: F811
    peer, _, org, model, deployment, _, raw, cid = plane
    client, root, app, settings = http
    cold = await create_org(client, root, "Second enterprise")
    await app.state.process_billing()
    receiver = {"id": str(admin.execute("SELECT id FROM billing.receiving_accounts").fetchone()[0])}
    for tenant in [org, cold]:
        credit = await funding(client, root, tenant, receiver, "5000")
        await post_funding(client, root, tenant, credit)
    await make_policy(client, root, cold, [model["id"]])
    proj = await project(client, root, cold, "load")
    sa = await account(client, root, cold, proj["id"])
    cold_key = (await key(client, root, cold, sa["id"])).json()["secret"]
    route = await client.put(
        f"/api/v1/route-policies/{cold['id']}/{model['id']}",
        headers=auth(root),
        json={"candidates": [{"deployment_id": deployment["id"]}]},
    )
    assert route.status_code == 201
    cluster_path = f"/api/v1/gateway-clusters/{cid}"
    version = admin.execute(
        "SELECT version FROM publication.gateway_clusters WHERE id=%s", (cid,)
    ).fetchone()[0]
    assert (
        await client.patch(
            cluster_path,
            headers=auth(root, version),
            json={"organization_ids": [org["id"], cold["id"]]},
        )
    ).status_code == 200
    assert (
        await client.post(
            cluster_path + "/releases", headers=headers(root, "load-snapshot"), json={}
        )
    ).status_code == 202
    async with AsyncExitStack() as stack:
        nodes = [peer]
        for _ in range(3):
            dsn = await register_node(client, root, admin, cid)
            nodes.append(
                await stack.enter_async_context(
                    GatewaySimulator.connect(
                        dsn, settings.redis_url.get_secret_value(), peer.namespace
                    )
                )
            )
        await asyncio.gather(*(node.refresh() for node in nodes))
        async with worker(service_directory, peer.redis, peer.namespace) as (_, heartbeat):
            # Steady 30 s at 8 requests/s; a separate 16-in-flight burst hits one account.
            await load_phase(
                admin, nodes, [raw, raw, raw, cold_key], model["id"], deployment["id"], 240, 8
            )
            await load_phase(admin, nodes, [raw], model["id"], deployment["id"], 32, 0)
            await asyncio.sleep(0.4)
            metrics = json.loads(await peer.redis.get(heartbeat))["metrics"]["database"]
            print("worker_pool " + json.dumps(metrics))
    for tenant, expected in [(org, 5100 - 2 * (180 + 32)), (cold, 5000 - 2 * 60)]:
        row = admin.execute(
            "SELECT balance,held_amount FROM billing.accounts WHERE organization_id=%s",
            (tenant["id"],),
        ).fetchone()
        assert row == (expected, 0)
        check = await client.post(
            f"/api/v1/organizations/{tenant['id']}/account/reconcile", headers=auth(root)
        )
        assert check.json()["consistent"]
    info = await peer.redis.info()
    pg = admin.execute("""
        SELECT current_setting('server_version'),current_setting('max_connections'),
        current_setting('shared_buffers'),current_setting('fsync'),
        current_setting('synchronous_commit'),
        min(octet_length(envelope::text)),max(octet_length(envelope::text))
        FROM billing.usage_events
    """).fetchone()
    print("environment", pg, info["redis_version"], info["maxmemory"], info["maxmemory_policy"])


async def test_backlog_replay_and_worker_takeover(plane, http, admin, service_directory):  # noqa: F811
    peer, _, org, model, dep, admission, raw, _ = plane
    await peer.refresh()
    async with worker(service_directory, peer.redis, peer.namespace) as (process, _):
        process.kill()
        await process.wait()
    admin.execute("UPDATE billing.settings SET settlement_backlog_limit=16 WHERE id=1")
    ids = [uuid4() for _ in range(16)]
    for rid in ids:
        await submit(peer, raw, model["id"], dep["id"], rid)
    assert account_amounts(admin, org) == (100, 32)
    with pytest.raises(psycopg.errors.RaiseException, match="settlement_backlog"):
        await peer.reserve(raw, model["id"], 1, 1, uuid4())
    # Response-loss retries and already-admitted work remain accessible during backpressure.
    res = await peer.call(
        "reserve_request", ids[0], admission.api_key_id, UUID(model["id"]), 1, 1, 1
    )
    assert res["replayed"]
    started = monotonic()
    async with worker(service_directory, peer.redis, peer.namespace):
        await drain(admin, ids)
        report = profile_results(admin, ids)
        assert report["unfinished"] == report["missing_or_duplicate_debit"] == 0
        print("restart_drain_seconds", round(monotonic() - started, 3), "jobs", len(ids))
        assert account_amounts(admin, org) == (
            68,
            0,
        )
        rid = uuid4()
        await submit(peer, raw, model["id"], dep["id"], rid)
        await drain(admin, [rid])
        assert account_amounts(admin, org) == (
            66,
            0,
        )


async def test_inflight_gateway_across_migrations(plane, http, admin, migration_settings):  # noqa: F811
    from alembic import command
    from alembic.config import Config

    peer, _, org, model, dep, admission, raw, _ = plane
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["settings"] = migration_settings
    await peer.refresh()
    rid, aid = admission.request_id, uuid4()
    # The same connected gateway and its cached revision straddle both upgrades.
    await asyncio.to_thread(command.downgrade, config, "0008_gateway_protocol")
    try:
        # Under 0008 the reservation reads the release's keys from its manifest.
        list_keys_in_manifests(admin, peer.keys.values())
        _, lease = await peer.reserve(raw, model["id"], 1, 1, rid)
        await peer.start_attempt(rid, aid, 1, dep["id"], lease)
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")
    assert admin.execute(
        "SELECT generation>copied_generation FROM billing.report_changes WHERE request_id=%s",
        (rid,),
    ).fetchone() == (True,)
    await send_usage(peer, rid, aid, lease)
    await http[2].state.usage.process()
    assert await http[2].state.reports.project() == 1
    assert (await peer.call("get_request_status", rid))["charged_amount"] == "2.000000000000"
    assert account_amounts(admin, org) == (98, 0)


async def test_native_dependency_outages(plane, http, admin, service_directory):  # noqa: F811
    from redis.exceptions import RedisError

    from nexus.gateway_simulator import GatewayOutcomeUnknown
    from scripts.native_services import NativeServices

    peer, _, org, model, dep, _, raw, cid = plane
    services = NativeServices(service_directory, database=admin.info.dbname, track_commits=True)
    await http[2].state.publication.distribute()
    await peer.refresh()
    rid, aid = uuid4(), uuid4()
    _, lease = await peer.reserve(raw, model["id"], 1, 1, rid)
    await peer.start_attempt(rid, aid, 1, dep["id"], lease)
    # Kill only the authenticated ephemeral instance belonging to this integration runner.
    pid = (await peer.redis.info("server"))["process_id"]
    assert pid == int((service_directory / "redis.pid").read_text())

    os.kill(pid, signal.SIGTERM)
    try:
        async with asyncio.timeout(5):
            while (service_directory / "redis.pid").exists():  # noqa: ASYNC110
                await asyncio.sleep(0.025)
        assert (await http[0].get("/health/ready")).status_code == 503
        with pytest.raises(RedisError):
            await peer.reserve(raw, model["id"], 1, 1, uuid4())
        assert admin.execute(
            "SELECT count(*),sum(reserved_amount) FROM billing.request_runs"
        ).fetchone() == (1, 2)
    finally:
        await asyncio.to_thread(services.start)
    assert not await peer.limits.renew(lease)
    assert (await peer.refresh())["revision"] == "1"
    admin.execute("UPDATE publication.config_outbox SET next_attempt_at=clock_timestamp()")
    await http[2].state.publication.distribute()
    assert await peer.redis.get(f"{peer.namespace}:cfg:{{{cid}}}:current") == "1"
    await asyncio.to_thread(services.stop_postgres)
    try:
        assert (await http[0].get("/health/ready")).status_code == 503
        with pytest.raises((GatewayOutcomeUnknown, psycopg.OperationalError)):
            await peer.reserve(raw, model["id"], 1, 1, uuid4())
    finally:
        await asyncio.to_thread(services.start)
    assert (await http[0].get("/health/ready")).status_code == 200
    assert (await peer.call("get_request_status", rid))["reservation_state"] == "held"
    # Usage already observed at the upstream survives loss of the Redis execution lease.
    await peer.event(
        rid,
        "attempt.finished",
        {
            "outcome": "succeeded",
            "upstream_request_id": "recovered",
            "usage_format": "fixture.tokens.v1",
            "elapsed_ms": 1,
            "raw_usage": {"input_total_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
        },
        attempt_id=aid,
    )
    await peer.event(
        rid,
        "request.closed",
        {
            "delivery_outcome": "succeeded",
            "attempt_ids": [str(aid)],
            "delivered_attempt_id": str(aid),
        },
    )
    await http[2].state.usage.process()
    with services.admin_connection() as conn:
        assert conn.execute("SELECT count(*) FROM billing.request_runs").fetchone() == (1,)
        assert account_amounts(conn, org) == (
            98,
            0,
        )


@pytest.mark.parametrize("operation", ["cancel", "close"])
async def test_start_races_request_completion(operation, plane, http, admin):  # noqa: F811
    original, _, org, model, dep, _, raw, cid = plane
    client, root, app, settings = http
    dsn = await register_node(client, root, admin, cid)
    async with AsyncExitStack() as stack:
        peers = [
            await stack.enter_async_context(
                GatewaySimulator.connect(
                    dsn, settings.redis_url.get_secret_value(), original.namespace
                )
            )
            for _ in range(2)
        ]
        await asyncio.gather(*(p.refresh() for p in peers))
        charged = 0
        for i in range(8):
            peer, second = peers if i % 2 else reversed(peers)
            rid, aid = uuid4(), uuid4()
            _, lease = await peer.reserve(raw, model["id"], 1, 1, rid)
            close = {
                "delivery_outcome": "client_cancelled",
                "attempt_ids": [],
                "delivered_attempt_id": None,
            }
            end = (
                second.call("cancel_unstarted_request", rid, "client_cancel")
                if operation == "cancel"
                else second.event(rid, "request.closed", close)
            )
            started, completed = await asyncio.gather(
                peer.call("start_attempt", rid, aid, 1, UUID(dep["id"])),
                end,
                return_exceptions=True,
            )
            assert (
                sum(isinstance(x, psycopg.errors.RaiseException) for x in [started, completed]) == 1
            )
            if isinstance(started, dict):
                await send_usage(peer, rid, aid, lease)
                charged += 2
            else:
                assert isinstance(completed, dict)
                assert await peer.limits.finish(lease, 0)
            await app.state.usage.process()
            status = await peer.call("get_request_status", rid)
            assert status["reservation_state"] != "held"
        assert account_amounts(admin, org) == (
            100 - charged,
            0,
        )
