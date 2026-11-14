"""Multi-node ingress latency and burst drain on an isolated, funded gateway cluster."""

import asyncio
import json
import math
import random
from collections import Counter, defaultdict
from contextlib import AsyncExitStack
from time import monotonic
from uuid import uuid4

import pytest
from test_billing import funding, headers, make_budget, post_funding
from test_catalog import make_policy
from test_control_plane_acceptance import submit, worker
from test_identity_api import auth, create_org
from test_plane import plane, register_node  # noqa: F401
from test_project_credentials import account, key, project

from nexus.gateway_simulator import GatewaySimulator

pytestmark = [pytest.mark.integration, pytest.mark.capacity]


def p99(values):
    if not values:
        return None
    return round(sorted(values)[math.ceil(len(values) * 0.99) - 1], 3)


def timed_call(call, samples):
    async def measured(operation, *args):
        label = operation
        if operation == "append_usage_event":
            label = json.loads(args[0])["event_type"]
        start = monotonic()
        result = await call(operation, *args)
        samples[label].append(1000 * (monotonic() - start))
        return result

    return measured


async def provision_tenants(http, admin, first, first_key, model, deployment, cluster):
    client, root, app, _ = http
    receiver = {"id": str(admin.execute("SELECT id FROM billing.receiving_accounts").fetchone()[0])}
    tenants = [first]
    secrets = [first_key]
    for i in range(1, 8):
        tenant = await create_org(client, root, f"Load tenant {i}")
        await app.state.process_billing()
        await make_policy(client, root, tenant, [model])
        proj = await project(client, root, tenant, "load")
        sa = await account(client, root, tenant, proj["id"])
        secrets.append((await key(client, root, tenant, sa["id"])).json()["secret"])
        tenants.append(tenant)
        route = await client.post(
            "/api/v1/route-policies",
            headers=auth(root),
            json={
                "organization_id": tenant["id"],
                "model_id": model,
                "candidates": [{"deployment_id": deployment}],
            },
        )
        assert route.status_code == 201, route.text
    for tenant in tenants:
        record = await funding(client, root, tenant, receiver, "100000")
        await post_funding(client, root, tenant, record)
        await make_budget(client, root, tenant, "100000")
    path = f"/api/v1/gateway-clusters/{cluster}"
    version = admin.execute(
        "SELECT version FROM publication.gateway_clusters WHERE id=%s", (cluster,)
    ).fetchone()[0]
    changed = await client.patch(
        path, headers=auth(root, version), json={"organization_ids": [t["id"] for t in tenants]}
    )
    assert changed.status_code == 200, changed.text
    published = await client.post(
        path + "/releases", headers=headers(root, "gateway-load"), json={}
    )
    assert published.status_code == 202, published.text
    return tenants, secrets


async def monitor(admin, stop, peaks):
    def sample():
        return admin.execute("""
            SELECT (SELECT count(*) FROM billing.usage_jobs WHERE state='ready'),
                   (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()
                    AND wait_event_type='Lock' AND usename LIKE 'nexus_node_%')
        """).fetchone()

    while not stop.is_set():
        ready, locks = await asyncio.to_thread(sample)
        peaks["ready"] = max(peaks["ready"], ready)
        peaks["lock_waiters"] = max(peaks["lock_waiters"], locks)
        await asyncio.sleep(0.05)


def accounting(admin, ids):
    # Use the same PostgreSQL clock for the end of ingress and the final job commit.
    # The final evidence and the ledger entry are immutable. The job stops changing at done.
    return admin.execute(
        """
        WITH requests AS (SELECT unnest(%s::uuid[]) id),
        evidence AS (
          SELECT request_id, count(*) n, max(pg_xact_commit_timestamp(xmin)) last_event
          FROM billing.usage_events WHERE request_id=ANY(%s) GROUP BY request_id
        ), charges AS (
          SELECT source_id,count(*) n,sum(signed_amount) charged,
                 max(pg_xact_commit_timestamp(xmin)) booked
          FROM billing.ledger_entries WHERE kind='usage_debit' AND source_id=ANY(%s)
          GROUP BY source_id
        ), facts AS (
          SELECT j.state, e.n events, e.last_event, c.n debits,c.charged,c.booked,
            max(e.last_event) OVER() ingress_end,
            CASE WHEN j.state='done' THEN pg_xact_commit_timestamp(j.xmin) END done_at
          FROM requests r LEFT JOIN evidence e ON e.request_id=r.id
          LEFT JOIN billing.usage_jobs j ON j.request_id=r.id
          LEFT JOIN charges c ON c.source_id=r.id
        )
        SELECT count(*) FILTER(WHERE state IS DISTINCT FROM 'done'),
          count(*) FILTER(WHERE state='review'), count(*) FILTER(WHERE events IS DISTINCT FROM 2),
          count(*) FILTER(WHERE debits IS DISTINCT FROM 1 OR charged IS DISTINCT FROM -2),
          count(*) FILTER(WHERE done_at>ingress_end),
          extract(epoch FROM max(done_at)-max(ingress_end)),
          percentile_disc(0.99) WITHIN GROUP(ORDER BY extract(epoch FROM booked-last_event))
        FROM facts
    """,
        (ids, ids, ids),
    ).fetchone()


async def run_profile(admin, nodes, samples, secrets, model, deployment, total, rate, hot, trial):
    # Give every node one independent login and at most one in-flight request.
    # Distribute a deterministic, shuffled tenant mix independently of the node index.
    if hot:
        other = total // 10
        owners = [0] * (total - other) + [1 + i % 7 for i in range(other)]
    else:
        owners = [i % 8 for i in range(total)]
    random.Random(326).shuffle(owners)
    ids = [uuid4() for _ in owners]
    samples.clear()
    errors = Counter()
    peaks = Counter()
    lag = []
    stop = asyncio.Event()
    sampler = asyncio.create_task(monitor(admin, stop, peaks))
    started = monotonic()

    async def lane(index, node):
        for i in range(index, total, len(nodes)):
            if rate:
                due = started + i / rate
                await asyncio.sleep(max(0, due - monotonic()))
                lag.append(1000 * (monotonic() - due))
            try:
                async with asyncio.timeout(15):
                    await submit(node, secrets[owners[i]], model, deployment, ids[i])
            except Exception as exc:
                errors[getattr(exc, "sqlstate", None) or type(exc).__name__] += 1

    try:
        await asyncio.gather(*(lane(i, n) for i, n in enumerate(nodes)))
        elapsed = monotonic() - started
        deadline = monotonic() + 90
        while monotonic() < deadline:
            done = admin.execute(
                "SELECT count(*) FROM billing.usage_jobs WHERE state='done' AND request_id=ANY(%s)",
                (ids,),
            ).fetchone()[0]
            if done >= total - sum(errors.values()):
                break
            await asyncio.sleep(0.1)
    finally:
        stop.set()
        await sampler
    unfinished, review, bad_events, bad_debits, tail, drain, settle = accounting(admin, ids)
    writes = samples["attempt.finished"] + samples["request.closed"]
    report = {
        "trial": trial,
        "active_nodes": len(nodes),
        "mix": "90% hot" if hot else "uniform",
        "target_rps": rate or "burst",
        "requests": total,
        "tenant_counts": dict(sorted(Counter(owners).items())),
        "ingress_s": round(elapsed, 3),
        "completed_ingress_rps": round((total - sum(errors.values())) / elapsed, 2),
        "schedule_lag_p99_ms": p99(lag),
        "sql_p99_ms": {name: p99(durations) for name, durations in samples.items()},
        "usage_combined_p99_ms": p99(writes),
        "reserve_samples": len(samples["reserve_request"]),
        "usage_samples": len(writes),
        "peaks": dict(peaks),
        "unsettled_at_last_event_commit": tail,
        "drain_s": round(float(drain), 3) if drain is not None else None,
        "settlement_p99_s": round(float(settle), 3) if settle is not None else None,
        "errors": dict(errors),
        "unfinished": unfinished,
        "review": review,
        "wrong_event_count": bad_events,
        "wrong_debit": bad_debits,
    }
    print("gateway_load " + json.dumps(report), flush=True)
    assert not (errors or unfinished or review or bad_events or bad_debits), report
    assert len(samples["reserve_request"]) == total and len(writes) == 2 * total
    return Counter(owners)


async def test_gateway_load(plane, http, admin, service_directory, monkeypatch):  # noqa: F811
    first, _, org, model, dep, _, raw, cluster = plane
    client, root, _, settings = http
    tenants, secrets = await provision_tenants(
        http, admin, org, raw, model["id"], dep["id"], cluster
    )
    counts = Counter()
    samples = defaultdict(list)
    async with AsyncExitStack() as stack:
        nodes = [first]
        for _ in range(63):
            dsn = await register_node(client, root, admin, cluster)
            nodes.append(
                await stack.enter_async_context(
                    GatewaySimulator.connect(
                        dsn, settings.redis_url.get_secret_value(), first.namespace
                    )
                )
            )
        await asyncio.gather(*(p.refresh() for p in nodes))
        for node in nodes:
            monkeypatch.setattr(node, "call", timed_call(node.call, samples))
        async with worker(service_directory, first.redis, first.namespace):
            # Reverse the order on the second pass to expose run-to-run variability.
            profiles = [(8, False), (8, True), (64, True), (64, False)]
            for trial in range(2):
                for active, hot in profiles if trial == 0 else reversed(profiles):
                    counts.update(
                        await run_profile(
                            admin,
                            nodes[:active],
                            samples,
                            secrets,
                            model["id"],
                            dep["id"],
                            1500,
                            50,
                            hot,
                            trial + 1,
                        )
                    )
                for hot in [False, True] if trial == 0 else [True, False]:
                    counts.update(
                        await run_profile(
                            admin,
                            nodes,
                            samples,
                            secrets,
                            model["id"],
                            dep["id"],
                            512,
                            0,
                            hot,
                            trial + 1,
                        )
                    )
    for i, tenant in enumerate(tenants):
        row = admin.execute(
            """
          SELECT a.balance,a.held_amount,(SELECT sum(signed_amount) FROM billing.ledger_entries
            WHERE account_id=a.id),p.spent,p.reserved FROM billing.accounts a
          JOIN billing.budget_periods p ON p.organization_id=a.organization_id
          WHERE a.organization_id=%s
        """,
            (tenant["id"],),
        ).fetchone()
        balance = 100000 + (100 if i == 0 else 0) - 2 * counts[i]
        assert row == (balance, 0, balance, 2 * counts[i], 0), i
    print(
        "gateway_accounting",
        sum(counts.values()),
        "requests",
        len(tenants),
        "tenants OK",
        flush=True,
    )
