"""Worker settlement cost per request, measured in-process (docs/relay-acceptance.md).

Facts are seeded the way a Relay writes them, through the node's ``gateway_v1`` functions with
no Worker running: ``reserve_request``, ``start_attempt``, then ``attempt.finished`` in the
``openai.responses.v1`` format and ``request.closed``. The Worker's own ``UsageProcessor`` then
drains the queue in this process, with the Worker's database role and pool. Each profile records
wall and CPU time per settlement, client-side SQL statements and their time, the statements the
server logged for the Worker role (including BEGIN, COMMIT and pool pings), and a cProfile
breakdown by phase. Run it with:

    SETTLEMENT_BENCH=1 LC_ALL=en_US.UTF-8 TMPDIR=/tmp/nx uv run python scripts/run_integration.py \\
        --capacity -s -q tests/integration/test_settlement_capacity.py

``SETTLEMENT_BENCH_OUT`` appends one JSON line per profile.
"""

import asyncio
import cProfile
import hashlib
import json
import os
import pstats
import re
import time
from collections import Counter
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from relay_load_kit import NODE_CAPABILITIES
from sqlalchemy import event
from test_billing import funding, headers, make_budget, post_funding
from test_catalog import make_policy
from test_identity_api import auth, create_org
from test_plane import provision_plane
from test_project_credentials import account, key, project

from nexus.bootstrap import Application
from nexus.config import RuntimeSettings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.capacity,
    pytest.mark.skipif(not os.environ.get("SETTLEMENT_BENCH"), reason="set SETTLEMENT_BENCH=1"),
]
FUNDS = "100000000"
PROFILE = {
    "endpoint": "openai_responses",
    "transports": ["http", "websocket"],
    "input_ceiling": 4,
    "output_ceiling": 6,
    "streaming": True,
    "tools": True,
    "continuation": True,
    "cancellation": True,
    "bound_reference": "settlement-bench",
    "count_rule": "deployment_ceiling.v1",
}
# What the Relay reports for the upstream double's Responses usage (scripts/relay_upstream.py).
RAW = {
    "input_tokens": 2,
    "output_tokens": 3,
    "total_tokens": 5,
    "cached_input_tokens": 0,
    "reasoning_tokens": 0,
}
# Functions whose cumulative time is reported per settlement.
PHASES = {
    "claim": "claim_usage",
    "facts": "usage_facts",
    "attempts": "priced_attempts",
    "corrections": "corrected_attempts",
    "normalize": "normalize",
    "cost": "record_cost_in",
    "normalization_row": "save_normalization",
    "settle": "settle_in",
    "post": "post_settlement",
    "budgets": "settle_budget_holds",
    "ledger": "append_entry",
    "finish": "finish_usage",
    "policy_row": "settings",
    "orm_flush": "flush",
    "commit": "commit",
    "digest": "digest",
}


class Statements:
    """Client-side statements executed on the Worker engine and their wall time."""

    def __init__(self, engine):
        self.count = 0
        self.seconds = 0.0
        self.kinds = Counter()
        self._started = {}
        event.listen(engine, "before_cursor_execute", self.before)
        event.listen(engine, "after_cursor_execute", self.after)
        self.engine = engine

    def before(self, conn, cursor, statement, parameters, context, executemany):
        self._started[id(cursor)] = time.perf_counter()

    def after(self, conn, cursor, statement, parameters, context, executemany):
        self.seconds += time.perf_counter() - self._started.pop(id(cursor), time.perf_counter())
        self.count += 1
        words = re.sub(r"\s+", " ", statement.strip()).split(" ")
        head = " ".join(words[:3])[:60]
        self.kinds[head] += 1

    def close(self):
        event.remove(self.engine, "before_cursor_execute", self.before)
        event.remove(self.engine, "after_cursor_execute", self.after)


class ServerLog:
    """Statements the server logged for the Worker role (log_min_duration_statement=0)."""

    LINE = re.compile(rb"^\S+ \S+ \S+ \[\d+\] nexus_worker LOG:  duration: ([\d.]+) ms  (.*)$")

    def __init__(self, path):
        self.path = Path(path)
        self.offset = self.path.stat().st_size

    def read(self):
        kinds = Counter()
        seconds = 0.0
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            for line in stream:
                match = self.LINE.match(line)
                # Extended-protocol parse and bind steps are logged separately from execute.
                if match and not match.group(2).startswith((b"parse", b"bind")):
                    seconds += float(match.group(1)) / 1000
                    text = match.group(2).decode(errors="replace")
                    text = text.split(": ", 1)[-1] if ": " in text else text
                    kinds[" ".join(text.split()[:3])[:50]] += 1
        return sum(kinds.values()), seconds, kinds


async def seed_tenants(http, admin, first, first_key, model, deployment, cid, tenants):
    client, root, app, _ = http
    receiver = {"id": str(admin.execute("SELECT id FROM billing.receiving_accounts").fetchone()[0])}
    members = [(first, first_key)]
    for i in range(1, tenants):
        tenant = await create_org(client, root, f"Bench tenant {i}")
        await app.state.process_billing()
        await make_policy(client, root, tenant, [model["id"]])
        proj = await project(client, root, tenant, "bench")
        sa = await account(client, root, tenant, proj["id"])
        secret = (await key(client, root, tenant, sa["id"])).json()["secret"]
        r = await client.put(
            f"/api/v1/route-policies/{tenant['id']}/{model['id']}",
            headers=auth(root),
            json={"candidates": [{"deployment_id": deployment["id"]}]},
        )
        assert r.status_code == 201, r.text
        record = await funding(client, root, tenant, receiver, FUNDS)
        await post_funding(client, root, tenant, record)
        await make_budget(client, root, tenant, FUNDS)
        members.append((tenant, secret))
    r = await client.patch(
        f"/api/v1/deployments/{deployment['id']}",
        headers=auth(root, deployment["version"]),
        json={"relay_profile": PROFILE},
    )
    assert r.status_code == 200, r.text
    version = admin.execute(
        "SELECT version FROM publication.gateway_clusters WHERE id=%s", (cid,)
    ).fetchone()[0]
    r = await client.patch(
        f"/api/v1/gateway-clusters/{cid}",
        headers=auth(root, version),
        json={"organization_ids": [m[0]["id"] for m in members]},
    )
    assert r.status_code == 200, r.text
    # The simulator node must accept a snapshot with a Relay profile.
    nid, role = admin.execute(
        "SELECT node_id,database_role FROM publication.gateway_principals WHERE cluster_id=%s",
        (cid,),
    ).fetchone()
    r = await client.put(
        f"/api/v1/gateway-clusters/{cid}/principals/{nid}",
        headers=auth(root),
        json={"node_id": str(nid), "database_role": role, "capabilities": NODE_CAPABILITIES},
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        f"/api/v1/gateway-clusters/{cid}/releases",
        headers=headers(root, uuid4().hex),
        json={},
    )
    assert r.status_code == 202, r.text
    while await app.state.publication.distribute():
        pass
    return members


async def relay_refresh(peer):
    """Accept the current snapshot the way a Relay does (the simulator refuses Relay profiles)."""
    head = await peer.call("config_head")
    rev = int(head["revision"])
    data = json.loads(bytes(await peer.call("get_config_snapshot", rev)))
    await peer._keys(rev, data["key_set"])
    if head["applied_revision"] != head["revision"]:
        await peer.call("ack_config", rev, head["sha256"], True, "")
    peer.node_id = head["node_id"]
    peer.snapshot = data
    return data


async def seed_requests(peer, model_id, dep_id, owners, members, *, failed_first=False):
    """Relay-shaped facts for one request per owner; returns the request ids."""
    data = await relay_refresh(peer)
    keys = [UUID(peer.keys[hashlib.sha256(s.encode()).hexdigest()]["id"]) for _, s in members]
    revision = int(data["revision"])
    ids = []
    for owner in owners:
        rid = uuid4()
        result = await peer.call(
            "reserve_request", rid, keys[owner], UUID(str(model_id)), revision, 4, 6
        )
        assert result["state"] == "reserved", result
        attempts = []
        for seq, outcome in enumerate(["failed", "succeeded"] if failed_first else ["succeeded"]):
            aid = uuid4()
            await peer.call("start_attempt", rid, aid, seq + 1, dep_id)
            attempts.append(aid)
            payload = {
                "outcome": outcome,
                "upstream_request_id": None if outcome == "failed" else "resp_" + aid.hex,
                "usage_format": "openai.responses.v1",
                "raw_usage": None if outcome == "failed" else RAW,
                "elapsed_ms": 3,
            }
            reply = await peer.event(
                rid, "attempt.finished", payload, attempt_id=aid, revision=result["config_revision"]
            )
            assert reply["status"] == "accepted", reply
        closed = {
            "delivery_outcome": "succeeded",
            "attempt_ids": sorted(str(a) for a in attempts),
            "delivered_attempt_id": str(attempts[-1]),
        }
        reply = await peer.event(rid, "request.closed", closed, revision=result["config_revision"])
        assert reply["status"] == "accepted", reply
        ids.append(rid)
    return ids


def phase_breakdown(profile, settled):
    stats = pstats.Stats(profile)
    out = {}
    for (filename, _, name), (_, _, _, cumulative, _) in stats.stats.items():
        for label, wanted in PHASES.items():
            if name == wanted and ("nexus" in filename or label in {"commit", "orm_flush"}):
                out[label] = out.get(label, 0) + cumulative
    return {k: round(1000 * v / settled, 3) for k, v in sorted(out.items())}


def top_functions(profile, limit=25):
    stats = pstats.Stats(profile)
    rows = sorted(stats.stats.items(), key=lambda item: item[1][2], reverse=True)[:limit]
    return [
        {"fn": f"{Path(f).name}:{n}", "tottime_ms": round(1000 * v[2], 1), "calls": v[1]}
        for (f, _, n), v in rows
    ]


async def drain(application, profile=None, batch=16):
    settled = 0
    started, cpu = time.perf_counter(), time.process_time()
    if profile:
        profile.enable()
    try:
        while count := await application.usage.process(batch):
            settled += count
    finally:
        if profile:
            profile.disable()
    return settled, time.perf_counter() - started, time.process_time() - cpu


def emit(record):
    print("settlement_capacity " + json.dumps(record))
    if out := os.environ.get("SETTLEMENT_BENCH_OUT"):
        with open(out, "a") as stream:
            stream.write(json.dumps(record) + "\n")


@pytest.mark.parametrize(
    ("name", "requests", "tenants", "hot", "failed_first"),
    [
        ("one-attempt-hot", 600, 8, True, False),
        ("one-attempt-uniform", 600, 8, False, False),
        ("failover-declined", 300, 8, False, True),
    ],
)
async def test_settlement_cost(
    http, admin, service_directory, name, requests, tenants, hot, failed_first
):
    async with provision_plane(http, admin, balance=FUNDS) as plane:
        peer, _, org, model, dep, _, raw, cid = plane
        client, root, app, _ = http
        await make_budget(client, root, org, FUNDS)
        members = await seed_tenants(http, admin, org, raw, model, dep, cid, tenants)
        if hot:
            other = requests // 10
            owners = [0] * (requests - other) + [1 + i % (tenants - 1) for i in range(other)]
        else:
            owners = [i % tenants for i in range(requests)]
        ids = await seed_requests(
            peer, model["id"], UUID(dep["id"]), owners, members, failed_first=failed_first
        )
        settings = RuntimeSettings(
            _env_file=service_directory / ".env.worker", environment="test", process_role="worker"
        )
        settings = settings.model_copy(update={"redis_namespace": "nexus:test:" + uuid4().hex})
        admin.execute("ALTER ROLE nexus_worker SET log_min_duration_statement=0")
        application = Application(settings)
        try:
            engine = application.database._engine.sync_engine
            statements = Statements(engine)
            server = ServerLog(Path(service_directory) / "postgres.log")
            settled, wall, cpu = await drain(application)
            statements.close()
            # Leave the log a moment to flush.
            await asyncio.sleep(0.2)
            server_count, server_seconds, server_kinds = server.read()
            projected_started = time.perf_counter()
            projected_cpu = time.process_time()
            projected = 0
            while count := await application.reports.project():
                projected += count
            projection_wall = time.perf_counter() - projected_started
            projection_cpu = time.process_time() - projected_cpu
            # A second, profiled run on fresh facts gives the per-function breakdown.
            more = await seed_requests(
                peer, model["id"], UUID(dep["id"]), owners, members, failed_first=failed_first
            )
            profile = cProfile.Profile()
            profiled, profiled_wall, _ = await drain(application, profile)
            while await application.reports.project():
                pass
        finally:
            admin.execute("ALTER ROLE nexus_worker RESET log_min_duration_statement")
            await application.close()
        assert settled == len(ids) and profiled == len(more)
        debits = admin.execute(
            "SELECT count(*), count(DISTINCT source_id) FROM billing.ledger_entries "
            "WHERE kind='usage_debit' AND source_id=ANY(%s)",
            (ids + more,),
        ).fetchone()
        assert debits == (len(ids) + len(more), len(ids) + len(more)), debits
        reviews = admin.execute(
            "SELECT reason, count(*) FROM billing.review_cases WHERE request_id=ANY(%s) GROUP BY 1",
            (ids + more,),
        ).fetchall()
        emit(
            {
                "profile": name,
                "requests": settled,
                "tenants": tenants,
                "hot": hot,
                "load1": round(os.getloadavg()[0], 2),
                "per_settlement_ms": {
                    "wall": round(1000 * wall / settled, 3),
                    "cpu": round(1000 * cpu / settled, 3),
                    "client_sql": round(1000 * statements.seconds / settled, 3),
                    "server_sql": round(1000 * server_seconds / settled, 3),
                    "profiled_wall": round(1000 * profiled_wall / profiled, 3),
                },
                "drain_per_s": round(settled / wall, 1),
                "statements_per_settlement": {
                    "client": round(statements.count / settled, 2),
                    "server": round(server_count / settled, 2),
                },
                "server_statement_kinds": {
                    k: round(v / settled, 2) for k, v in server_kinds.most_common(40)
                },
                "client_statement_kinds": {
                    k: round(v / settled, 2) for k, v in statements.kinds.most_common(40)
                },
                "projection": {
                    "rows": projected,
                    "wall_ms_per_request": round(1000 * projection_wall / max(projected, 1), 3),
                    "cpu_ms_per_request": round(1000 * projection_cpu / max(projected, 1), 3),
                },
                "phases_ms": phase_breakdown(profile, profiled),
                "top_tottime": top_functions(profile),
                "reviews": {r: n for r, n in reviews},
            }
        )
