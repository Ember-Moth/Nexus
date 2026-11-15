"""P5 capacity profiles for release-built Rust Relays (docs/relay-acceptance.md).

Real Relay processes with their own node logins, isolated PostgreSQL/Redis, the SQL Worker and
the TLS upstream double scripts/relay_upstream.py. Run through scripts/relay_load.py; every
profile runs once per round, the second round in reverse order, and appends one JSON object to
RELAY_LOAD_OUT. A profile fails on lost or duplicate charges and on reservations left behind.
"""

import asyncio
import json
import os
import random
import time
from collections import Counter
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import pytest
from psycopg import sql
from relay_load_kit import (
    MONEY_FAILURES,
    NODE_CAPABILITIES,
    Fleet,
    LogTail,
    PgLog,
    Upstream,
    WorkerHandle,
    calm_host,
    emit,
    exchange,
    foreign_limit,
    host_state,
    known_requests,
    metric_delta,
    metric_total,
    money,
    new_requests,
    postgres_pids,
    redis_pid,
    register_node,
    rounds,
    sampling,
    selected,
    settlement,
    statement_summary,
    summary_ms,
    terminal_ok,
    wait_settled,
)
from test_billing import funding, headers, make_budget, post_funding, publish_price
from test_catalog import make_deployment, make_policy, make_upstream
from test_identity_api import auth, create_org
from test_plane import provision_plane
from test_project_credentials import account, key, project
from test_relay import tls  # noqa: F401 - pytest fixture
from websockets.asyncio.client import connect

pytestmark = [
    pytest.mark.integration,
    pytest.mark.capacity,
    pytest.mark.skipif(
        not os.environ.get("RELAY_LOAD_RUN"), reason="run through scripts/relay_load.py"
    ),
]
FUNDS = "100000000"
STATEMENTS = [
    "reserve_request",
    "start_attempt",
    "append_usage_event",
    "get_credential_envelope",
    "get_config_snapshot",
    "ack_config",
]


@pytest.fixture(scope="session")
def binary():
    return Path(os.environ["RELAY_LOAD_BINARY"])


class Plane:
    """One provisioned cluster: Relays, upstream double, tenants, Worker and samplers."""

    def __init__(self, **values):
        self.__dict__.update(values)

    def groups(self):
        return {
            "relays": [r.pid for r in self.fleet.relays],
            "postgres": postgres_pids(self.services),
            "redis": [p for p in [redis_pid(self.services)] if p],
            "worker": self.worker.pids(),
            "upstream": [self.upstream.process.pid],
            "client": [os.getpid()],
        }

    def own_pids(self):
        return {pid for group in self.groups().values() for pid in group}

    def headers(self, secret):
        if self.family == "anthropic":
            return {"x-api-key": secret, "anthropic-version": "2023-06-01"}
        return {"Authorization": f"Bearer {secret}"}

    def body(self, marker, *, stream=False, padding="", count=False):
        text = f"lt:{marker}{padding}"
        if count:
            return {"model": self.model, "messages": [{"role": "user", "content": text}]}
        if self.family == "anthropic":
            return {
                "model": self.model,
                "max_tokens": self.output_ceiling,
                "stream": stream,
                "messages": [{"role": "user", "content": text}],
            }
        return {"model": self.model, "input": text, "stream": stream}


@asynccontextmanager
async def load_plane(
    http,
    admin,
    tls,  # noqa: F811
    binary,
    service_directory,
    tmp_path,
    *,
    family="openai_compatible",
    nodes=4,
    tenants=8,
    profile=None,
    relay_env=None,
    backup=False,
):
    client, root, app, settings = http
    endpoint = "anthropic_messages" if family == "anthropic" else "openai_responses"
    relay_profile = {
        "endpoint": endpoint,
        "transports": ["http", "websocket"] if endpoint == "openai_responses" else ["http"],
        "input_ceiling": 4,
        "output_ceiling": 6,
        "streaming": True,
        "tools": True,
        "continuation": endpoint == "openai_responses",
        "cancellation": endpoint == "openai_responses",
        "bound_reference": "isolated-load",
        "count_rule": "deployment_ceiling.v1",
        **(profile or {}),
    }
    async with AsyncExitStack() as stack:
        upstream = await stack.enter_async_context(Upstream.start(tls[1].parent / "upstream.pem"))
        plane = await stack.enter_async_context(
            provision_plane(http, admin, family, balance=FUNDS, base_url=upstream.url("a"))
        )
        peer, private, org, model, dep, admission, raw, cid = plane
        await make_budget(client, root, org, FUNDS)
        deployments = [dep]
        if backup:
            second_upstream = await make_upstream(
                client, root, "Backup", family, base_url=upstream.url("b")
            )
            credential = await client.post(
                f"/api/v1/upstreams/{second_upstream['id']}/credentials",
                headers=auth(root),
                json={"name": "Backup", "secret": "backup-secret-do-not-echo"},
            )
            assert credential.status_code == 201, credential.text
            second = await make_deployment(client, root, model, second_upstream, credential.json())
            await publish_price(client, root, "cost", second, "0.2")
            deployments.append(second)
        for d in deployments:
            r = await client.patch(
                f"/api/v1/deployments/{d['id']}",
                headers=auth(root, d["version"]),
                json={"relay_profile": relay_profile},
            )
            assert r.status_code == 200, r.text
        candidates = [{"deployment_id": d["id"], "priority": i} for i, d in enumerate(deployments)]
        if backup:
            r = await client.put(
                f"/api/v1/route-policies/{org['id']}/{model['id']}",
                headers=auth(root, 1),
                json={"candidates": candidates},
            )
            assert r.status_code == 200, r.text
        receiver = {
            "id": str(admin.execute("SELECT id FROM billing.receiving_accounts").fetchone()[0])
        }
        members = [{"org": org, "secret": raw}]
        for i in range(1, tenants):
            tenant = await create_org(client, root, f"Load tenant {i}")
            await app.state.process_billing()
            await make_policy(client, root, tenant, [model["id"]])
            proj = await project(client, root, tenant, "load")
            sa = await account(client, root, tenant, proj["id"])
            secret = (await key(client, root, tenant, sa["id"])).json()["secret"]
            r = await client.put(
                f"/api/v1/route-policies/{tenant['id']}/{model['id']}",
                headers=auth(root),
                json={"candidates": candidates},
            )
            assert r.status_code == 201, r.text
            record = await funding(client, root, tenant, receiver, FUNDS)
            await post_funding(client, root, tenant, record)
            await make_budget(client, root, tenant, FUNDS)
            members.append({"org": tenant, "secret": secret, "service_account": sa["id"]})
        version = admin.execute(
            "SELECT version FROM publication.gateway_clusters WHERE id=%s", (cid,)
        ).fetchone()[0]
        r = await client.patch(
            f"/api/v1/gateway-clusters/{cid}",
            headers=auth(root, version),
            json={
                "organization_ids": [m["org"]["id"] for m in members],
                "deployment_ids": [d["id"] for d in deployments],
            },
        )
        assert r.status_code == 200, r.text
        # The simulator principal from provision_plane stays idle but must accept the snapshot.
        nid, role = admin.execute(
            "SELECT node_id,database_role FROM publication.gateway_principals WHERE cluster_id=%s",
            (cid,),
        ).fetchone()
        r = await client.put(
            f"/api/v1/gateway-clusters/{cid}/principals/{nid}",
            headers=auth(root),
            json={
                "node_id": str(nid),
                "database_role": role,
                "capabilities": NODE_CAPABILITIES,
            },
        )
        assert r.status_code == 200, r.text
        registered = [await register_node(client, root, admin, cid, auth) for _ in range(nodes)]
        r = await client.post(
            f"/api/v1/gateway-clusters/{cid}/releases", headers=headers(root, "load"), json={}
        )
        assert r.status_code == 202, r.text
        while await app.state.publication.distribute():
            pass
        fleet = Fleet(binary, admin, settings, cid, private, tls[1], tmp_path)
        stack.push_async_callback(fleet.stop)
        await fleet.start(registered, relay_env)
        worker_log = Path(tmp_path) / "worker.jsonl"
        process = Workers(
            [
                WorkerHandle(service_directory, peer.redis, settings.redis_namespace, worker_log)
                for _ in range(int(os.environ.get("RELAY_LOAD_WORKERS", "1")))
            ]
        )
        await process.start()
        stack.push_async_callback(process.stop)
        yield Plane(
            http=http,
            admin=admin,
            app=app,
            client=client,
            root=root,
            settings=settings,
            peer=peer,
            fleet=fleet,
            upstream=upstream,
            worker=process,
            services=service_directory,
            pglog=PgLog(Path(service_directory) / "postgres.log"),
            family=family,
            model=model["public_name"],
            model_id=model["id"],
            deployments=deployments,
            cid=cid,
            tenants=members,
            admission=admission,
            output_ceiling=relay_profile["output_ceiling"],
            dsn=admin_dsn(admin),
            worker_log=LogTail(worker_log),
        )


class Workers:
    """RELAY_LOAD_WORKERS Worker processes, started and paused together."""

    def __init__(self, handles):
        self.handles = handles

    def pids(self):
        return [h.pid for h in self.handles if h.pid]

    async def start(self):
        await asyncio.gather(*(h.start() for h in self.handles))

    async def stop(self):
        await asyncio.gather(*(h.stop() for h in self.handles))


def admin_dsn(admin):
    info = admin.info
    return (
        f"host=127.0.0.1 port={info.port} dbname={info.dbname} user={info.user} "
        f"password={info.password}"
    )


class Measurement:
    pass


@asynccontextmanager
async def measured(plane, relays=None):
    """Host load, Relay metrics, PostgreSQL samples and statement durations around a profile."""
    m = Measurement()
    m.admin = plane.admin
    m.gauges = Counter()
    m.host_before = await calm_host(plane.own_pids())
    await plane.upstream.reset()
    plane.pglog.mark()
    m.known = known_requests(plane.admin)
    plane.fleet.mark_logs()
    plane.worker_log.mark()
    m.relays = plane.fleet.relays if relays is None else relays
    m.before = await plane.fleet.metrics(m.relays)
    async with sampling(plane.dsn, plane.services, plane.groups) as sampler:
        m.sampler = sampler
        m.started = time.monotonic()
        yield m
        m.elapsed = time.monotonic() - m.started
        # The Worker drains inside the sampling window, so its queue and CPU are recorded.
        m.ids = new_requests(plane.admin, m.known)
        m.settle_wait = await wait_settled(plane.admin, m.ids)
    m.after = await plane.fleet.metrics(m.relays)
    m.upstream = await plane.upstream.stats()
    m.statements = plane.pglog.read()
    m.samples = sampler.summary()
    m.host_after = host_state()


def relay_counters(m):
    names = {
        "requests_succeeded": ("nexus_relay_requests_total", {"outcome": "succeeded"}),
        "requests_failed": ("nexus_relay_requests_total", {"outcome": "failed"}),
        "requests_rejected": ("nexus_relay_requests_total", {"outcome": "rejected"}),
        "requests_unknown": ("nexus_relay_requests_total", {"outcome": "unknown"}),
        "completion_replays": ("nexus_relay_completion_event_replays_total", {}),
        "completions_abandoned": ("nexus_relay_completion_events_abandoned_total", {}),
        "control_errors": ("nexus_relay_control_errors_total", {}),
        "failover_switched": ("nexus_relay_failover_decisions_total", {"result": "switched"}),
    }
    out = {k: metric_delta(m.before, m.after, n, **lb) for k, (n, lb) in names.items()}
    out = {k: int(v) for k, v in out.items() if v}
    errors = Counter()
    for sample_set, sign in ((m.after, 1), (m.before, -1)):
        for sample in sample_set:
            for name, value in sample.items():
                if name.startswith("nexus_relay_control_errors_total{"):
                    errors[name.partition("{")[2].rstrip("}")] += sign * value
    if errors := {k: int(v) for k, v in errors.items() if v}:
        out["control_errors_by_label"] = errors
    return out


def evidence_commits(admin, ids):
    rows = admin.execute(
        """SELECT request_id, extract(epoch FROM max(pg_xact_commit_timestamp(xmin)))
           FROM billing.usage_events WHERE request_id=ANY(%s) GROUP BY request_id""",
        (ids,),
    ).fetchall()
    return {str(r): float(t) for r, t in rows}


def client_summary(results, kind, m):
    statuses = Counter(r.status for r in results)
    errors = Counter(r.error for r in results if r.error)
    ok = [r for r in results if terminal_ok(r, kind)]
    first = min(r.sent for r in results)
    last_done = max((r.done for r in results if r.done), default=first)
    records = {rec["marker"]: rec for rec in m.upstream["records"] if rec.get("marker")}
    admission = []
    completion = []
    commits = evidence_commits(m.admin, [r.request_id for r in results if r.request_id])
    for r in results:
        rec = records.get(f"lt:{r.marker}")
        if rec:
            admission.append(rec["arrived"] - r.sent)
            if r.request_id in commits and rec.get("done_wall"):
                completion.append(commits[r.request_id] - rec["done_wall"])
    gaps = [g for r in ok for g in r.gaps()]
    # Like the Python baseline: from the first send to the last usage evidence commit.
    ingress = max(commits.values(), default=None)
    ingress_s = None if ingress is None else ingress - min(r.sent_wall for r in results)
    return {
        "requests": len(results),
        "ok": len(ok),
        "ingress_s": None if ingress_s is None else round(ingress_s, 3),
        "ingress_rps": None if not ingress_s else round(len(ok) / ingress_s, 1),
        "statuses": {str(k): v for k, v in statuses.items()},
        "errors": dict(errors),
        "achieved_rps": round(len(ok) / max(last_done - first, 1e-6), 1),
        "window_s": round(last_done - first, 3),
        "latency_ms": summary_ms([r.latency for r in ok]),
        "ttfb_ms": summary_ms([r.ttfb for r in ok]),
        "pre_upstream_ms": summary_ms(admission),
        "evidence_after_upstream_ms": summary_ms(completion),
        **({"event_gap_ms": summary_ms(gaps)} if gaps else {}),
        **(
            {"events_per_response": round(sum(r.events for r in ok) / len(ok), 1)}
            if ok and ok[0].events
            else {}
        ),
    }


async def finish(plane, m, scenario, profile, round_no, metrics, *, expected=None, check=True):
    """Settle, check money, and emit one record."""
    ledger = money(plane.admin, m.ids, expected)
    record = {
        **metrics,
        "workers": len(plane.worker.handles),
        "settlement": {**settlement(plane.admin, m.ids), "waited_s": round(m.settle_wait, 2)},
        "money": ledger,
        "statements_ms": statement_summary(m.statements, STATEMENTS),
        "postgres": m.samples,
        "relay": relay_counters(m),
        "relay_logs": plane.fleet.log_summary(),
        "worker_logs": plane.worker_log.summary(),
    }
    during = m.samples["foreign_cpu_pct"]["mean"] or 0
    contended = bool(m.host_before.get("contended")) or during > foreign_limit()
    emitted = emit(
        scenario,
        profile,
        round_no,
        record,
        m.host_before,
        m.host_after,
        contended=contended,
        attempt=RERUN.get("attempt", 0),
    )
    if check:
        problems = {k: ledger[k] for k in MONEY_FAILURES if ledger[k]}
        if record["settlement"]["unfinished"]:
            problems["unfinished"] = record["settlement"]["unfinished"]
        if problems:
            FAILURES.append((scenario, profile, round_no, problems))
    return emitted


FAILURES = []


def assert_clean():
    """Fail the test after all of its profiles ran if any profile broke a money invariant."""
    failures, FAILURES[:] = list(FAILURES), []
    assert not failures, failures


RERUN = {}


async def repeat(run, *args):
    """Run a profile; rerun it while other work on the host made the measurement contended."""
    retries = int(os.environ.get("RELAY_LOAD_RETRIES", "2"))
    for attempt in range(retries + 1):
        RERUN["attempt"] = attempt
        record = await run(*args)
        if not record["contended"]:
            break
    RERUN.clear()
    return record


def expected_charges(plane, results, kind, amount):
    return {r.request_id: (amount if terminal_ok(r, kind) else 0) for r in results if r.request_id}


async def run_open_loop(jobs, rate):
    """Start each job at its due time (open loop); rate None starts all at once."""
    started = time.monotonic()
    lag = []
    tasks = []
    for i, job in enumerate(jobs):
        if rate:
            due = started + i / rate
            delay = due - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            lag.append(time.monotonic() - due)
        tasks.append(asyncio.create_task(job()))
    return await asyncio.gather(*tasks), lag


async def run_closed_loop(jobs, concurrency):
    gate = asyncio.Semaphore(concurrency)

    async def guarded(job):
        async with gate:
            return await job()

    return await asyncio.gather(*(guarded(job) for job in jobs))


def http_jobs(plane, relays, owners, tag, *, stream=False, path=None, padding="", slow=None):
    path = path or ("/v1/messages" if plane.family == "anthropic" else "/v1/responses")
    jobs = []
    for i, owner in enumerate(owners):
        relay = relays[i % len(relays)]
        secret = plane.tenants[owner]["secret"]
        marker = f"{tag}-{i}".replace("/", "_")
        count = path.endswith("/count_tokens")
        body = json.dumps(plane.body(marker, stream=stream, padding=padding, count=count))
        body = body.encode()

        def job(relay=relay, secret=secret, marker=marker, body=body):
            return exchange(
                relay.port,
                path,
                plane.headers(secret),
                body,
                marker,
                keep_body=not stream,
                slow=slow,
            )

        jobs.append(job)
    return jobs


def owners_for(total, tenants, hot):
    if hot:
        other = total // 10
        owners = [0] * (total - other) + [1 + i % (tenants - 1) for i in range(other)]
    else:
        owners = [i % tenants for i in range(total)]
    random.Random(326).shuffle(owners)
    return owners


def charge(plane, cfg):
    return cfg.get("input_tokens", 2) + cfg.get("output_tokens", 3)


# ------------------------------------------------------------------ control-plane baseline


async def test_control(http, admin, tls, binary, service_directory, tmp_path):  # noqa: F811
    """8/64 active nodes, uniform and 90% hot tenants, 50 rps x 30 s, and a 512 burst."""
    async with load_plane(
        http, admin, tls, binary, service_directory, tmp_path, nodes=64, tenants=8
    ) as plane:
        await plane.upstream.lanes(a={"latency_ms": 0, "events": 1})
        steady = [(8, False), (8, True), (64, True), (64, False)]
        bursts = [False, True]
        steady = selected(steady, lambda p: f"steady-{p[0]}n-{'hot' if p[1] else 'uniform'}")
        bursts = selected(bursts, lambda hot: f"burst-64n-{'hot' if hot else 'uniform'}")
        for round_no in range(1, rounds() + 1):
            order = steady if round_no % 2 else list(reversed(steady))
            for active, hot in order:
                await repeat(control_profile, plane, round_no, active, hot, 1500, 50)
            for hot in bursts if round_no % 2 else list(reversed(bursts)):
                await repeat(control_profile, plane, round_no, 64, hot, 512, None)
    assert_clean()


async def test_pool_variants(http, admin, tls, binary, service_directory, tmp_path):  # noqa: F811
    """The 90% hot burst and steady state under other node pool sizes and lock settings."""
    variants = [
        ("pool-8", {}, None),
        ("pool-4", {"NEXUS_RELAY_POOL_SIZE": "4"}, None),
        ("pool-2", {"NEXUS_RELAY_POOL_SIZE": "2"}, None),
        ("pool-8-deadlock-10s", {}, "10s"),
    ]
    variants = selected(variants)
    async with load_plane(
        http, admin, tls, binary, service_directory, tmp_path, nodes=64, tenants=8
    ) as plane:
        await plane.upstream.lanes(a={"latency_ms": 0, "events": 1})
        for round_no in range(1, rounds() + 1):
            for variant, env, deadlock in variants if round_no % 2 else list(reversed(variants)):
                await reconfigure(plane, env, deadlock)
                for total, rate in ((512, None), (1500, 50)):
                    await repeat(control_profile, plane, round_no, 64, True, total, rate, variant)
    assert_clean()


async def test_hot_wallet(http, admin, tls, binary, service_directory, tmp_path):  # noqa: F811
    """90% hot tenant at 50 rps with settlement running, and with the Worker paused."""
    async with load_plane(
        http, admin, tls, binary, service_directory, tmp_path, nodes=64, tenants=8
    ) as plane:
        await plane.upstream.lanes(a={"latency_ms": 0, "events": 1})
        # worker-paused-uniform: the same backlog spread evenly over the eight wallets, the
        # case in which several Workers can settle in parallel.
        profiles = selected(
            [
                ("worker-running", False, True),
                ("worker-paused", True, True),
                ("worker-paused-uniform", True, False),
            ]
        )
        for round_no in range(1, rounds() + 1):
            for name, paused, hot in profiles if round_no % 2 else list(reversed(profiles)):
                await repeat(hot_wallet_profile, plane, round_no, name, paused, hot)
    assert_clean()


async def hot_wallet_profile(plane, round_no, name, paused, hot=True):
    # 1000 requests stay below the 1024 ready jobs at which reservations are refused.
    owners = owners_for(1000, len(plane.tenants), hot)
    jobs = http_jobs(plane, plane.fleet.relays, owners, f"w{round_no}{name}")
    if paused:
        await plane.worker.stop()
    async with measured(plane) as m:
        results, lag = await run_open_loop(jobs, 50)
        if paused:
            await plane.worker.start()
    metrics = {
        "worker_during_ingress": "paused" if paused else "running",
        "mix": "90% hot" if hot else "uniform",
        "schedule_lag_ms": summary_ms(lag),
        "client": client_summary(results, "responses", m),
    }
    expected = expected_charges(plane, results, "responses", 5)
    return await finish(plane, m, "hotwallet", name, round_no, metrics, expected=expected)


async def reconfigure(plane, env, deadlock):
    """Restart every Relay with ``env``; ``deadlock`` sets deadlock_timeout for node logins."""
    nodes = list(plane.fleet.nodes)
    await plane.fleet.stop()
    plane.fleet.nodes = []
    for node in nodes:
        statement = (
            sql.SQL("ALTER ROLE {} SET deadlock_timeout = {}").format(
                sql.Identifier(node["role"]), sql.Literal(deadlock)
            )
            if deadlock
            else sql.SQL("ALTER ROLE {} RESET deadlock_timeout").format(
                sql.Identifier(node["role"])
            )
        )
        plane.admin.execute(statement)
    await plane.fleet.start(nodes, env)


async def control_profile(plane, round_no, active, hot, total, rate, variant=None):
    name = f"{'steady' if rate else 'burst'}-{active}n-{'hot' if hot else 'uniform'}"
    scenario = "control"
    if variant:
        name, scenario = f"{variant}/{name}", "pools"
    relays = plane.fleet.relays[:active]
    owners = owners_for(total, len(plane.tenants), hot)
    # Steady: open-loop arrivals at `rate`. Burst: 64 nodes x 8 requests all in flight at once.
    jobs = http_jobs(plane, relays, owners, f"c{round_no}{name}")
    async with measured(plane) as m:
        results, lag = await run_open_loop(jobs, rate)
    sent = [r.sent for r in results]
    metrics = {
        "active_nodes": active,
        "mix": "90% hot" if hot else "uniform",
        "target_rps": rate or "burst",
        "input_window_s": round(max(sent) - min(sent), 3),
        "schedule_lag_ms": summary_ms(lag) if lag else None,
        "client": client_summary(results, "responses", m),
        "tenant_counts": dict(sorted(Counter(owners).items())),
    }
    expected = expected_charges(plane, results, "responses", 5)
    return await finish(plane, m, scenario, name, round_no, metrics, expected=expected)


# ------------------------------------------------------------------ HTTP and SSE


async def test_http(http, admin, tls, binary, service_directory, tmp_path):  # noqa: F811
    async with load_plane(
        http,
        admin,
        tls,
        binary,
        service_directory,
        tmp_path,
        nodes=4,
        tenants=8,
        profile={"input_ceiling": 16, "output_ceiling": 4096},
    ) as plane:
        profiles = [
            ("json-50rps", http_profile_json),
            ("sse-50rps", http_profile_sse),
            ("upstream-2s-50rps", http_profile_latency),
            ("long-sse-100x30s", http_profile_long),
            ("slow-client-20x1MiB", http_profile_slow),
            ("sse-50rps-during-releases", http_profile_releases),
        ]
        profiles = selected(profiles)
        for round_no in range(1, rounds() + 1):
            for name, run in profiles if round_no % 2 else list(reversed(profiles)):
                await repeat(run, plane, round_no, name)
    assert_clean()


async def http_steady(plane, round_no, name, cfg, *, stream, seconds=20, rate=50, during=None):
    await plane.upstream.lanes(a=cfg)
    owners = owners_for(seconds * rate, len(plane.tenants), False)
    jobs = http_jobs(plane, plane.fleet.relays, owners, f"h{round_no}{name}", stream=stream)
    async with measured(plane) as m:
        side = asyncio.create_task(during(plane, seconds)) if during else None
        results, lag = await run_open_loop(jobs, rate)
        extra = await side if side else {}
    metrics = {
        "target_rps": rate,
        "seconds": seconds,
        "upstream": cfg,
        "schedule_lag_ms": summary_ms(lag),
        "client": client_summary(results, "responses", m),
        **extra,
    }
    expected = expected_charges(plane, results, "responses", charge(plane, cfg))
    return await finish(plane, m, "http", name, round_no, metrics, expected=expected)


async def http_profile_json(plane, round_no, name):
    cfg = {"latency_ms": 0, "events": 4, "interval_ms": 0, "output_tokens": 3}
    return await http_steady(plane, round_no, name, cfg, stream=False)


async def http_profile_sse(plane, round_no, name):
    cfg = {"latency_ms": 0, "events": 20, "interval_ms": 10, "output_tokens": 20}
    return await http_steady(plane, round_no, name, cfg, stream=True)


async def http_profile_latency(plane, round_no, name):
    cfg = {"latency_ms": 2000, "events": 4, "interval_ms": 0, "output_tokens": 3}
    return await http_steady(plane, round_no, name, cfg, stream=False)


async def wait_applied(plane, revision, limit):
    """Wait until every running Relay has acknowledged ``revision``."""
    roles = [relay.role for relay in plane.fleet.relays]
    async with asyncio.timeout(limit):
        while True:
            applied = plane.admin.execute(
                """SELECT min(applied_revision) FROM publication.gateway_principals
                   WHERE database_role=ANY(%s)""",
                (roles,),
            ).fetchone()[0]
            if applied >= revision:
                return
            await asyncio.sleep(0.02)


async def publish_loop(plane, seconds, every=3.0):
    """Issue a key and publish a release every few seconds, as identity changes would."""
    client, root = plane.client, plane.root
    tenant = plane.tenants[1]
    published = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(every)
        started = time.monotonic()
        await key(client, root, tenant["org"], tenant["service_account"])
        r = await client.post(
            f"/api/v1/gateway-clusters/{plane.cid}/releases",
            headers=headers(root, f"rel-{time.monotonic_ns()}"),
            json={},
        )
        assert r.status_code == 202, r.text
        while await plane.app.state.publication.distribute():
            pass
        await wait_applied(plane, int(r.json()["revision"]), 30)
        published.append(time.monotonic() - started)
    return {"releases": len(published), "release_to_all_applied_ms": summary_ms(published)}


async def http_profile_releases(plane, round_no, name):
    cfg = {"latency_ms": 0, "events": 20, "interval_ms": 10, "output_tokens": 20}
    return await http_steady(
        plane, round_no, name, cfg, stream=True, seconds=30, during=publish_loop
    )


async def concurrent_streams(plane, round_no, name, cfg, count, spread, slow=None):
    await plane.upstream.lanes(a=cfg)
    owners = owners_for(count, len(plane.tenants), False)
    jobs = http_jobs(
        plane, plane.fleet.relays, owners, f"h{round_no}{name}", stream=True, slow=slow
    )
    async with measured(plane) as m:
        gauges = asyncio.create_task(peak_gauges(plane, m))
        results, _ = await run_open_loop(jobs, count / spread)
        gauges.cancel()
    metrics = {
        "streams": count,
        "upstream": cfg,
        "slow_read": slow,
        "client": client_summary(results, "responses", m),
        "relay_gauges_peak": dict(m.gauges),
        "bytes_per_stream": round(sum(r.bytes for r in results) / len(results)),
    }
    expected = expected_charges(plane, results, "responses", charge(plane, cfg))
    return await finish(plane, m, "http", name, round_no, metrics, expected=expected)


async def peak_gauges(plane, m):
    names = [
        "nexus_relay_sse_streams_active",
        "nexus_relay_capacity_in_use",
        "nexus_relay_websocket_sessions_active",
        "nexus_relay_websocket_turns_active",
        "nexus_relay_completion_events_pending",
    ]
    while True:
        samples = await plane.fleet.metrics(m.relays)
        for n in names:
            m.gauges[n.removeprefix("nexus_relay_")] = max(
                m.gauges[n.removeprefix("nexus_relay_")], int(metric_total(samples, n))
            )
        await asyncio.sleep(0.5)


async def http_profile_long(plane, round_no, name):
    cfg = {"latency_ms": 0, "events": 600, "interval_ms": 50, "output_tokens": 600}
    return await concurrent_streams(plane, round_no, name, cfg, 100, 2.0)


async def http_profile_slow(plane, round_no, name):
    cfg = {
        "latency_ms": 0,
        "events": 2000,
        "interval_ms": 0,
        "delta_bytes": 512,
        "output_tokens": 2000,
    }
    return await concurrent_streams(plane, round_no, name, cfg, 20, 1.0, slow=(4096, 0.05))


# ------------------------------------------------------------------ Responses WebSocket


async def ws_session(port, secret, model, turns, tag, *, slow=None, abort_after=None):
    """Run ``turns`` sequential generations on one connection; return per-turn timings."""
    out = {"turns": [], "error": None, "opened": None}
    started = time.monotonic()
    try:
        async with connect(
            f"ws://127.0.0.1:{port}/v1/responses",
            additional_headers={"Authorization": f"Bearer {secret}"},
            proxy=None,
            open_timeout=30,
            ping_interval=None,
            max_queue=4 if slow else 16,
        ) as ws:
            out["opened"] = time.monotonic() - started
            for turn in range(turns):
                sent = time.monotonic()
                await ws.send(
                    json.dumps(
                        {"type": "response.create", "model": model, "input": f"lt:{tag}-{turn}"}
                    )
                )
                first = None
                events = 0
                kind = None
                while True:
                    if abort_after and time.monotonic() - sent > abort_after:
                        ws.transport.abort()
                        out["aborted"] = True
                        return out
                    message = await asyncio.wait_for(ws.recv(), 60)
                    events += 1
                    if first is None:
                        first = time.monotonic()
                    kind = json.loads(message)["type"]
                    if slow:
                        await asyncio.sleep(slow)
                    if kind in (
                        "response.completed",
                        "response.failed",
                        "error",
                        "response.incomplete",
                    ):
                        break
                out["turns"].append(
                    {
                        "first_event": first - sent,
                        "total": time.monotonic() - sent,
                        "events": events,
                        "terminal": kind,
                    }
                )
    except Exception as error:  # counted per session
        out["error"] = type(error).__name__
    return out


async def ws_idle(port, secret, hold, opened):
    try:
        async with connect(
            f"ws://127.0.0.1:{port}/v1/responses",
            additional_headers={"Authorization": f"Bearer {secret}"},
            proxy=None,
            open_timeout=30,
            ping_interval=None,
        ) as ws:
            opened.append(1)
            deadline = time.monotonic() + hold
            while time.monotonic() < deadline:
                await asyncio.sleep(5)
                await asyncio.wait_for(await ws.ping(), 10)
            return None
    except Exception as error:
        return type(error).__name__


async def test_ws(http, admin, tls, binary, service_directory, tmp_path):  # noqa: F811
    async with load_plane(
        http,
        admin,
        tls,
        binary,
        service_directory,
        tmp_path,
        nodes=4,
        tenants=8,
        profile={"input_ceiling": 16, "output_ceiling": 4096},
    ) as plane:
        profiles = [
            ("idle-200x20s", ws_profile_idle),
            ("multi-turn-64x10", ws_profile_turns),
            ("long-turn-32x30s", ws_profile_long),
            ("slow-read-16x1MiB", ws_profile_slow),
            ("disconnect-32-mid-turn", ws_profile_disconnect),
        ]
        profiles = selected(profiles)
        for round_no in range(1, rounds() + 1):
            for name, run in profiles if round_no % 2 else list(reversed(profiles)):
                await repeat(run, plane, round_no, name)
    assert_clean()


def ws_assign(plane, count):
    relays = plane.fleet.relays
    tenants = plane.tenants
    return [
        (relays[j % len(relays)].port, tenants[(j // len(relays)) % len(tenants)]["secret"])
        for j in range(count)
    ]


def ws_summary(sessions):
    turns = [t for s in sessions for t in s["turns"]]
    first_turn = [s["turns"][0]["first_event"] for s in sessions if s["turns"]]
    later = [t["first_event"] for s in sessions for t in s["turns"][1:]]
    return {
        "sessions": len(sessions),
        "session_errors": dict(Counter(s["error"] for s in sessions if s["error"])),
        "turns": len(turns),
        "terminals": dict(Counter(t["terminal"] for t in turns)),
        "handshake_ms": summary_ms([s["opened"] for s in sessions if s["opened"]]),
        "first_event_ms": summary_ms([t["first_event"] for t in turns]),
        "first_event_first_turn_ms": summary_ms(first_turn),
        "first_event_later_turns_ms": summary_ms(later),
        "turn_ms": summary_ms([t["total"] for t in turns]),
    }


def ws_upstream(m):
    counts = m.upstream["counts"]
    connections = counts.get("ws_accepted", 0)
    turns = counts.get("a:responses:ws", 0)
    return {
        "connections": connections,
        "turns": turns,
        "turns_per_connection": round(turns / connections, 2) if connections else None,
        "open_after": m.upstream["ws_open"],
        "peak_open": m.upstream["ws_peak"],
    }


async def ws_turn_profile(plane, round_no, name, cfg, sessions, turns, *, slow=None, abort=None):
    await plane.upstream.lanes(a=cfg)
    async with measured(plane) as m:
        gauges = asyncio.create_task(peak_gauges(plane, m))
        results = await asyncio.gather(
            *(
                ws_session(
                    port,
                    secret,
                    plane.model,
                    turns,
                    f"w{round_no}{name}-{j}",
                    slow=slow,
                    abort_after=abort,
                )
                for j, (port, secret) in enumerate(ws_assign(plane, sessions))
            )
        )
        # Give the relay time to notice closed sockets and to finish its turn bookkeeping.
        await asyncio.sleep(2 if abort is None else 12)
        gauges.cancel()
        after = await plane.fleet.metrics()
    metrics = {
        "upstream_cfg": cfg,
        "slow_read_s": slow,
        "client": ws_summary(results),
        "upstream": ws_upstream(m),
        "relay_gauges_peak": dict(m.gauges),
        "relay_gauges_after": {
            "sessions": int(metric_total(after, "nexus_relay_websocket_sessions_active")),
            "turns": int(metric_total(after, "nexus_relay_websocket_turns_active")),
        },
        "reservations": len(m.ids),
    }
    expected = None
    if abort is None:
        delivered = sum(len(r["turns"]) for r in results)
        assert delivered == sessions * turns, metrics
        expected = dict.fromkeys(m.ids, charge(plane, cfg))
    return await finish(plane, m, "ws", name, round_no, metrics, expected=expected)


async def ws_profile_idle(plane, round_no, name):
    await plane.upstream.lanes(a={"latency_ms": 0, "events": 4, "interval_ms": 0})
    async with measured(plane) as m:
        gauges = asyncio.create_task(peak_gauges(plane, m))
        opened = []
        errors = await asyncio.gather(
            *(ws_idle(port, secret, 20, opened) for port, secret in ws_assign(plane, 200))
        )
        await asyncio.sleep(2)
        gauges.cancel()
        after = await plane.fleet.metrics()
    metrics = {
        "connections": 200,
        "opened": len(opened),
        "errors": dict(Counter(e for e in errors if e)),
        "relay_gauges_peak": dict(m.gauges),
        "relay_sessions_after": int(metric_total(after, "nexus_relay_websocket_sessions_active")),
        "upstream": ws_upstream(m),
        "reservations": len(m.ids),
    }
    assert not m.ids and metrics["upstream"]["connections"] == 0, metrics
    return await finish(plane, m, "ws", name, round_no, metrics, expected={})


async def ws_profile_turns(plane, round_no, name):
    cfg = {"latency_ms": 0, "events": 10, "interval_ms": 10, "output_tokens": 10}
    return await ws_turn_profile(plane, round_no, name, cfg, 64, 10)


async def ws_profile_long(plane, round_no, name):
    cfg = {"latency_ms": 0, "events": 300, "interval_ms": 100, "output_tokens": 300}
    return await ws_turn_profile(plane, round_no, name, cfg, 32, 1)


async def ws_profile_slow(plane, round_no, name):
    cfg = {
        "latency_ms": 0,
        "events": 2000,
        "interval_ms": 0,
        "delta_bytes": 512,
        "output_tokens": 2000,
    }
    return await ws_turn_profile(plane, round_no, name, cfg, 16, 1, slow=0.005)


async def ws_profile_disconnect(plane, round_no, name):
    cfg = {"latency_ms": 0, "events": 300, "interval_ms": 100, "output_tokens": 300}
    record = await ws_turn_profile(plane, round_no, name, cfg, 32, 1, abort=3.0)
    gauges = record["metrics"]["relay_gauges_after"]
    assert gauges == {"sessions": 0, "turns": 0}, gauges
    assert record["metrics"]["upstream"]["open_after"] == 0, record["metrics"]["upstream"]
    return record


# ------------------------------------------------------------------ token counting


async def test_tokens(http, admin, tls, binary, service_directory, tmp_path):  # noqa: F811
    async with load_plane(
        http,
        admin,
        tls,
        binary,
        service_directory,
        tmp_path,
        family="anthropic",
        nodes=4,
        tenants=8,
        profile={"input_ceiling": 128_000, "output_ceiling": 64},
    ) as plane:
        await plane.upstream.lanes(a={"latency_ms": 0, "events": 4})
        profiles = [
            ("count-1KiB-c64", "/v1/messages/count_tokens", 1024, 2000, 64),
            ("count-1.5MiB-c16", "/v1/messages/count_tokens", 1536 * 1024, 160, 16),
            ("messages-1KiB-c64", "/v1/messages", 1024, 2000, 64),
            ("messages-1.5MiB-c16", "/v1/messages", 1536 * 1024, 160, 16),
        ]
        profiles = selected(profiles)
        for round_no in range(1, rounds() + 1):
            for profile in profiles if round_no % 2 else list(reversed(profiles)):
                await repeat(token_profile, plane, round_no, *profile)
    assert_clean()


async def token_profile(plane, round_no, name, path, size, total, concurrency):
    padding = " " + ("lorem ipsum dolor sit amet " * (size // 27 + 1))[:size]
    owners = owners_for(total, len(plane.tenants), False)
    jobs = http_jobs(
        plane, plane.fleet.relays, owners, f"t{round_no}{name}", path=path, padding=padding
    )
    async with measured(plane) as m:
        results = await run_closed_loop(jobs, concurrency)
    counting = path.endswith("count_tokens")
    metrics = {
        "path": path,
        "input_bytes": len(
            json.dumps(plane.body("x", padding=padding, count=path.endswith("count_tokens")))
        ),
        "concurrency": concurrency,
        "client": client_summary(results, "messages", m),
        "reservations": len(m.ids),
    }
    expected = None if counting else expected_charges(plane, results, "messages", 5)
    record = await finish(plane, m, "tokens", name, round_no, metrics, expected=expected)
    if counting:
        assert not m.ids, "count_tokens must not reserve"
    return record


# ------------------------------------------------------------------ snapshot polling cost


def set_keys(admin, admission, target):
    present = admin.execute("SELECT count(*) FROM identity.api_keys WHERE name='scale'").fetchone()[
        0
    ]
    if target > present:
        admin.execute(
            """INSERT INTO identity.api_keys(id,organization_id,project_id,service_account_id,name,
            digest,prefix) SELECT gen_random_uuid(),%s,%s,%s,'scale',
            encode(sha256(convert_to(gen_random_uuid()::text,'UTF8')),'hex'),'nxk_scale'
            FROM generate_series(1,%s)""",
            (
                admission.organization_id,
                admission.project_id,
                admission.service_account_id,
                target - present,
            ),
        )
    elif target < present:
        admin.execute(
            """DELETE FROM identity.api_keys WHERE id IN (SELECT id FROM identity.api_keys
               WHERE name='scale' LIMIT %s)""",
            (present - target,),
        )


async def test_snapshot(http, admin, tls, binary, service_directory, tmp_path):  # noqa: F811
    nodes = int(os.environ.get("RELAY_LOAD_SNAPSHOT_NODES", "8"))
    sizes = [
        int(n) for n in os.environ.get("RELAY_LOAD_SNAPSHOT_KEYS", "0,10000,100000").split(",")
    ]
    async with load_plane(
        http, admin, tls, binary, service_directory, tmp_path, nodes=nodes, tenants=1
    ) as plane:
        await plane.upstream.lanes(a={"latency_ms": 0, "events": 1})
        for round_no in range(1, rounds() + 1):
            for size in sizes if round_no % 2 else list(reversed(sizes)):
                await repeat(snapshot_profile, plane, round_no, size, nodes)
    assert_clean()


async def snapshot_profile(plane, round_no, size, nodes):
    client, root, admin = plane.client, plane.root, plane.admin
    set_keys(admin, plane.admission, size)
    started = time.monotonic()
    r = await client.post(
        f"/api/v1/gateway-clusters/{plane.cid}/releases",
        headers=headers(root, f"snap-{round_no}-{size}"),
        json={},
    )
    publish_s = time.monotonic() - started
    assert r.status_code == 202, r.text
    revision = int(r.json()["revision"])
    started = time.monotonic()
    while await plane.app.state.publication.distribute():
        pass
    await wait_applied(plane, revision, 300)
    applied_s = time.monotonic() - started
    payload = admin.execute(
        "SELECT octet_length(payload_bytes) FROM publication.config_snapshots "
        "WHERE cluster_id=%s AND revision=%s",
        (plane.cid, revision),
    ).fetchone()[0]
    reply = len(json.dumps(await plane.peer.call("get_config_snapshot")))
    name = f"keys-{size}"
    # Idle: only the periodic snapshot polling of every node.
    async with measured(plane) as m:
        await asyncio.sleep(20)
    polls = m.statements.get("get_config_snapshot", [])
    idle = {
        "seconds": round(m.elapsed, 1),
        "snapshot_polls": len(polls),
        "polls_per_s": round(len(polls) / m.elapsed, 2),
        "poll_ms": summary_ms(polls),
        "reply_mb_per_s": round(len(polls) * reply / m.elapsed / 1e6, 1),
        "postgres": m.samples,
    }
    # Then 20 requests per second for 15 s with the same snapshot.
    owners = [0] * 300
    jobs = http_jobs(plane, plane.fleet.relays, owners, f"s{round_no}{name}")
    async with measured(plane) as m:
        results, lag = await run_open_loop(jobs, 20)
    metrics = {
        "keys": size,
        "nodes": nodes,
        "publish_s": round(publish_s, 3),
        "distribute_to_all_applied_s": round(applied_s, 3),
        "payload_mb": round(payload / 1e6, 2),
        "snapshot_reply_mb": round(reply / 1e6, 2),
        "idle": idle,
        "client": client_summary(results, "responses", m),
        "schedule_lag_ms": summary_ms(lag),
    }
    expected = expected_charges(plane, results, "responses", 5)
    return await finish(plane, m, "snapshot", name, round_no, metrics, expected=expected)


# ------------------------------------------------------------------ failover


async def test_failover(http, admin, tls, binary, service_directory, tmp_path):  # noqa: F811
    async with load_plane(
        http, admin, tls, binary, service_directory, tmp_path, nodes=4, tenants=8, backup=True
    ) as plane:
        profiles = [
            ("primary-healthy", {"error_rate": 0.0}),
            ("primary-429-all", {"error_rate": 1.0, "error_status": 429}),
            ("primary-503-all", {"error_rate": 1.0, "error_status": 503}),
            ("primary-429-half", {"error_rate": 0.5, "error_status": 429}),
        ]
        profiles = selected(profiles)
        for round_no in range(1, rounds() + 1):
            for name, lane in profiles if round_no % 2 else list(reversed(profiles)):
                await repeat(failover_profile, plane, round_no, name, lane)
    assert_clean()


async def failover_profile(plane, round_no, name, lane):
    base = {"latency_ms": 0, "events": 1, "output_tokens": 3}
    await plane.upstream.lanes(a={**base, **lane}, b={**base, "error_rate": 0.0})
    owners = owners_for(1000, len(plane.tenants), False)
    jobs = http_jobs(plane, plane.fleet.relays, owners, f"f{round_no}{name}")
    async with measured(plane) as m:
        results, lag = await run_open_loop(jobs, 50)
    attempts = plane.admin.execute(
        """SELECT count(*), count(DISTINCT request_id) FROM billing.request_attempts
           WHERE request_id=ANY(%s)""",
        (m.ids,),
    ).fetchone()
    states = dict(
        plane.admin.execute(
            """SELECT state, count(*) FROM billing.request_attempts WHERE request_id=ANY(%s)
               GROUP BY state""",
            (m.ids,),
        ).fetchall()
    )
    metrics = {
        "target_rps": 50,
        "primary": lane,
        "schedule_lag_ms": summary_ms(lag),
        "client": client_summary(results, "responses", m),
        "upstream_counts": {
            k: v for k, v in m.upstream["counts"].items() if k.startswith(("a:", "b:"))
        },
        "attempts": {"total": attempts[0], "requests": attempts[1], "states": states},
        "attempts_per_request": round(attempts[0] / max(attempts[1], 1), 3),
        "relay_attempts_mean": round(
            metric_delta(m.before, m.after, "nexus_relay_request_attempts_sum")
            / max(metric_delta(m.before, m.after, "nexus_relay_request_attempts_count"), 1),
            3,
        ),
    }
    expected = expected_charges(plane, results, "responses", 5)
    return await finish(plane, m, "failover", name, round_no, metrics, expected=expected)
