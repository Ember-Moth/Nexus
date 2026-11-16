"""Several Workers settle one queue at the same time; every request is charged exactly once.

Facts are written the way Relays write them (``gateway_v1`` with a node login): attempts are
started first, then ``attempt.finished`` and ``request.closed`` arrive in either order, some are
replayed, and some arrive while Workers are already settling. The Workers are separate
``Application`` instances with the Worker role, each with its own connection pool, so their
transactions really run concurrently in PostgreSQL.
"""

import asyncio
import hashlib
import os
import random
import signal
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from relay_load_kit import MONEY_FAILURES, money
from sqlalchemy import text
from test_billing import make_budget
from test_plane import provision_plane
from test_settlement_capacity import FUNDS, RAW, relay_refresh, seed_tenants

from nexus.bootstrap import Application
from nexus.config import RuntimeSettings

pytestmark = pytest.mark.integration
CHARGE = 5  # RAW is 2 input and 3 output tokens at 1 USD each


class Released(Exception):
    """Leaves a claim's transaction without settling it."""


def worker_application(service_directory):
    settings = RuntimeSettings(
        _env_file=Path(service_directory) / ".env.worker",
        environment="test",
        process_role="worker",
        redis_namespace="nexus:test:" + uuid4().hex,
    )
    return Application(settings)


class Gateway:
    """Reservations, attempts and usage facts for the seeded tenants, as a Relay sends them."""

    def __init__(self, peer, model_id, dep_id, members):
        self.peer, self.model_id, self.dep_id, self.members = peer, model_id, dep_id, members

    async def connect(self):
        data = await relay_refresh(self.peer)
        self.keys = [
            UUID(self.peer.keys[hashlib.sha256(s.encode()).hexdigest()]["id"])
            for _, s in self.members
        ]
        self.revision = int(data["revision"])
        return self

    async def admit(self, owner, *, declined=0, rid=None, aid=None):
        """Reserve one request and start its attempts; returns (request id, its facts)."""
        rid = rid or uuid4()
        result = await self.peer.call(
            "reserve_request", rid, self.keys[owner], UUID(str(self.model_id)), self.revision, 4, 6
        )
        assert result["state"] == "reserved", result
        attempts = []
        for seq in range(declined + 1):
            attempt = aid if aid and seq == declined else uuid4()
            await self.peer.call("start_attempt", rid, attempt, seq + 1, self.dep_id)
            attempts.append(attempt)
        facts = []
        for aid in attempts:
            served = aid == attempts[-1]
            facts.append(
                self.peer.prepare_event(
                    rid,
                    "attempt.finished",
                    {
                        "outcome": "succeeded" if served else "failed",
                        "upstream_request_id": "resp_" + aid.hex if served else None,
                        "usage_format": "openai.responses.v1",
                        "raw_usage": RAW if served else None,
                        "elapsed_ms": 3,
                    },
                    attempt_id=aid,
                    revision=result["config_revision"],
                )
            )
        facts.append(
            self.peer.prepare_event(
                rid,
                "request.closed",
                {
                    "delivery_outcome": "succeeded",
                    "attempt_ids": sorted(str(a) for a in attempts),
                    "delivered_attempt_id": str(attempts[-1]),
                },
                revision=result["config_revision"],
            )
        )
        return rid, facts

    async def send(self, message):
        reply = await self.peer.call("append_usage_event", message)
        assert reply["status"] == "accepted", reply


async def plane_with_tenants(http, admin, tenants):
    context = provision_plane(http, admin, balance=FUNDS)
    peer, _, org, model, dep, _, raw, cid = await context.__aenter__()
    client, root, _, _ = http
    await make_budget(client, root, org, FUNDS)
    members = await seed_tenants(http, admin, org, raw, model, dep, cid, tenants)
    gateway = await Gateway(peer, model["id"], UUID(dep["id"]), members).connect()
    return context, gateway


async def drain(applications, admin, until=None):
    """Run every Worker until no job is due and ``until`` (if any) is set."""

    def due():
        return admin.execute(
            "SELECT count(*) FROM billing.usage_jobs WHERE state='ready'"
        ).fetchone()[0]

    async def run(application):
        settled = 0
        while True:
            count = await application.usage.process(64)
            settled += count
            if count:
                continue
            if (until is None or until.is_set()) and not due():
                return settled
            await asyncio.sleep(0.005)

    return await asyncio.gather(*(run(a) for a in applications))


def check_money(admin, ids):
    ledger = money(admin, ids, dict.fromkeys(ids, CHARGE))
    assert {k: ledger[k] for k in MONEY_FAILURES} == dict.fromkeys(MONEY_FAILURES, 0), ledger
    assert ledger["charged_requests"] == len(ids), ledger
    assert ledger["request_states"] == {"settled/captured": len(ids)}, ledger
    # A close that arrives before an attempt's own fact opens a missing_attempt_cost case,
    # which the attempt's confirmed cost resolves; none may stay open.
    open_cases = dict(
        admin.execute(
            "SELECT reason, count(*) FROM billing.review_cases WHERE request_id=ANY(%s) "
            "AND state='open' GROUP BY 1",
            (ids,),
        ).fetchall()
    )
    assert open_cases == {}, ledger
    assert set(ledger["review_cases"]) <= {"missing_attempt_cost"}, ledger
    jobs = admin.execute(
        "SELECT state, count(*) FROM billing.usage_jobs WHERE request_id=ANY(%s) GROUP BY 1",
        (ids,),
    ).fetchall()
    assert jobs == [("done", len(ids))], jobs
    # Every attempt has exactly one confirmed cost, from its single normalization row.
    attempts = admin.execute(
        """SELECT count(*), count(*) FILTER (WHERE a.state='confirmed'),
                  count(n.event_id), count(DISTINCT n.event_id)
           FROM billing.request_attempts a
           LEFT JOIN billing.normalized_usage n ON n.attempt_id=a.id
           WHERE a.request_id=ANY(%s)""",
        (ids,),
    ).fetchone()
    assert attempts[0] == attempts[1] == attempts[2] == attempts[3], attempts
    conflicts = admin.execute(
        "SELECT count(*) FROM billing.audit_events WHERE action='usage.conflict'"
    ).fetchone()[0]
    assert conflicts == 0
    return ledger


async def test_workers_settle_out_of_order_and_replayed_facts_once(http, admin, service_directory):
    """Four Workers, facts arriving out of order, replayed and during settlement."""
    context, gateway = await plane_with_tenants(http, admin, 4)
    rng = random.Random(7)
    applications = [worker_application(service_directory) for _ in range(4)]
    try:
        # Half of the traffic belongs to one wallet; a third of the requests failed over.
        owners = [0] * 120 + [1 + i % 3 for i in range(120)]
        admitted = [
            await gateway.admit(owner, declined=1 if i % 3 == 0 else 0)
            for i, owner in enumerate(owners)
        ]
        ids = [rid for rid, _ in admitted]
        messages = []
        for _, facts in admitted:
            facts = facts[:]
            rng.shuffle(facts)  # closes before finishes, and attempts in any order
            messages.extend(facts)
            # Replays of identical envelopes, as after a lost commit response.
            messages.extend(rng.sample(facts, k=rng.randint(0, len(facts))))
        # Interleave requests so Workers see partial facts of many requests.
        rng.shuffle(messages)
        done = asyncio.Event()

        async def produce():
            for i, message in enumerate(messages):
                await gateway.send(message)
                if i % 25 == 0:
                    await asyncio.sleep(0.002)
            done.set()

        settled, _ = await asyncio.gather(drain(applications, admin, done), produce())
        # A job is settled at most once more than the number of times its facts changed.
        assert sum(settled) >= len(ids)
        # Late replays after settlement change nothing.
        for message in rng.sample(messages, 40):
            await gateway.send(message)
        assert sum(await drain(applications, admin)) == 0
        check_money(admin, ids)
        declined = admin.execute(
            """SELECT count(*), sum(a.cost_amount) FROM billing.normalized_usage n
               JOIN billing.request_attempts a ON a.id=n.attempt_id
               WHERE n.rule_version='relay.declined/1' AND a.request_id=ANY(%s)""",
            (ids,),
        ).fetchone()
        assert declined == (80, 0)
    finally:
        for application in applications:
            await application.close()
        await context.__aexit__(None, None, None)


async def test_concurrent_claims_never_share_a_wallet(http, admin, service_directory):
    """A Worker that holds a wallet's jobs makes the others skip them, never wait or repeat."""
    context, gateway = await plane_with_tenants(http, admin, 3)
    first, second = (worker_application(service_directory) for _ in range(2))
    try:
        ids = []
        for i in range(60):
            rid, facts = await gateway.admit(i % 3)
            for message in facts:
                await gateway.send(message)
            ids.append(rid)
        with pytest.raises(Released):
            async with first.usage._transactions() as held:
                account, rows, _ = await held.claim_usage(64)
                mine = {request.id for request, _ in rows}
                assert len(mine) == 20
                # The second Worker settles the other two wallets around the held one.
                assert await second.usage.process(64) == 40
                assert await second.usage.process(64) == 0
                held_debits = admin.execute(
                    "SELECT count(*) FROM billing.ledger_entries WHERE source_id=ANY(%s)",
                    (list(mine),),
                ).fetchone()[0]
                assert held_debits == 0
                # Leaving the transaction without settling releases the claim unchanged.
                raise Released
        assert await second.usage.process(64) == 20
        check_money(admin, ids)
    finally:
        await first.close()
        await second.close()
        await context.__aexit__(None, None, None)


async def test_lost_worker_session_returns_its_claim(http, admin, service_directory):
    """A Worker whose session dies mid-batch settles nothing; another one takes over at once."""
    context, gateway = await plane_with_tenants(http, admin, 2)
    lost, survivor = (worker_application(service_directory) for _ in range(2))
    try:
        ids = []
        for i in range(40):
            rid, facts = await gateway.admit(i % 2, declined=i % 4 == 0)
            for message in facts:
                await gateway.send(message)
            ids.append(rid)
        with pytest.raises(Exception):  # noqa: B017 - the session is gone, whatever the driver says
            async with lost.usage._transactions() as repo:
                account, rows, now = await repo.claim_usage(64)
                pid = (await repo._session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
                requests = [request for request, _ in rows]
                async with repo.usage_batch(requests, now):
                    for request in requests[: len(requests) // 2]:
                        await lost.usage._process(repo, account, request)
                    # The process dies before its batch is written and committed.
                    admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        assert sum(await drain([survivor], admin)) == 40
        check_money(admin, ids)
    finally:
        await lost.close()
        await survivor.close()
        await context.__aexit__(None, None, None)


async def test_killed_worker_process_is_replaced_without_double_charges(
    http, admin, service_directory
):
    """SIGKILL one of two Worker processes mid-drain; a replacement finishes the queue."""
    context, gateway = await plane_with_tenants(http, admin, 4)
    try:
        ids = []
        for i in range(600):
            rid, facts = await gateway.admit(0 if i % 2 else 1 + i % 3, declined=i % 5 == 0)
            for message in facts:
                await gateway.send(message)
            ids.append(rid)

        def debited():
            return admin.execute(
                "SELECT count(*) FROM billing.ledger_entries WHERE kind='usage_debit' "
                "AND source_id=ANY(%s)",
                (ids,),
            ).fetchone()[0]

        processes = [await spawn_worker(service_directory) for _ in range(2)]
        try:
            async with asyncio.timeout(60):
                while debited() < 100:  # noqa: ASYNC110 - polling the database
                    await asyncio.sleep(0.01)
            processes[0].send_signal(signal.SIGKILL)
            await processes[0].wait()
            assert debited() < len(ids), "the queue drained before the kill"
            processes[0] = await spawn_worker(service_directory)

            def unfinished():
                return admin.execute(
                    "SELECT count(*) FROM billing.usage_jobs WHERE request_id=ANY(%s) "
                    "AND state<>'done'",
                    (ids,),
                ).fetchone()[0]

            async with asyncio.timeout(120):
                while unfinished():  # noqa: ASYNC110 - polling the database
                    await asyncio.sleep(0.05)
        finally:
            for process in processes:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
        check_money(admin, ids)
    finally:
        await context.__aexit__(None, None, None)


async def spawn_worker(service_directory):
    env = {k: v for k, v in os.environ.items() if not k.startswith("NEXUS_")}
    env.update(
        NEXUS_WORKER_ID="concurrency-" + uuid4().hex[:8],
        NEXUS_REDIS_NAMESPACE="nexus:test:" + uuid4().hex,
        NEXUS_WORKER_INTERVAL="0.25",
    )
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "nexus.cli",
        "--env-file",
        str(Path(service_directory) / ".env.worker"),
        "worker",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def test_declined_attempt_is_confirmed_at_zero_cost(http, admin, service_directory):
    """A last attempt reported as declined releases the hold and opens no cost case."""
    context, gateway = await plane_with_tenants(http, admin, 1)
    worker = worker_application(service_directory)
    try:
        peer = gateway.peer
        ids = []
        for raw in (None, {"input_tokens": 0, "output_tokens": 0}):
            rid = uuid4()
            result = await peer.call(
                "reserve_request",
                rid,
                gateway.keys[0],
                UUID(str(gateway.model_id)),
                gateway.revision,
                4,
                6,
            )
            aid = uuid4()
            await peer.call("start_attempt", rid, aid, 1, gateway.dep_id)
            payload = {
                "outcome": "declined",
                "upstream_request_id": None,
                "usage_format": "openai.responses.v1",
                "raw_usage": raw,
                "elapsed_ms": 1,
            }
            revision = result["config_revision"]
            await gateway.send(
                peer.prepare_event(
                    rid, "attempt.finished", payload, attempt_id=aid, revision=revision
                )
            )
            closed = {
                "delivery_outcome": "upstream_failed",
                "attempt_ids": [str(aid)],
                "delivered_attempt_id": None,
            }
            await gateway.send(peer.prepare_event(rid, "request.closed", closed, revision=revision))
            ids.append(rid)
        # Usage contradicts a declined attempt.
        rid = uuid4()
        result = await peer.call(
            "reserve_request",
            rid,
            gateway.keys[0],
            UUID(str(gateway.model_id)),
            gateway.revision,
            4,
            6,
        )
        aid = uuid4()
        await peer.call("start_attempt", rid, aid, 1, gateway.dep_id)
        payload = {
            "outcome": "declined",
            "upstream_request_id": None,
            "usage_format": "openai.responses.v1",
            "raw_usage": RAW,
            "elapsed_ms": 1,
        }
        with pytest.raises(Exception, match="invalid_attempt_fact"):
            await peer.call(
                "append_usage_event",
                peer.prepare_event(
                    rid,
                    "attempt.finished",
                    payload,
                    attempt_id=aid,
                    revision=result["config_revision"],
                ),
            )
        assert await worker.usage.process() == 2
        rows = admin.execute(
            """SELECT r.state, r.reservation_state, r.charged_amount, a.state, a.cost_amount,
                      n.rule_version, j.state
               FROM billing.request_runs r JOIN billing.request_attempts a ON a.request_id=r.id
               JOIN billing.normalized_usage n ON n.attempt_id=a.id
               JOIN billing.usage_jobs j ON j.request_id=r.id WHERE r.id=ANY(%s)""",
            (ids,),
        ).fetchall()
        assert rows == [("settled", "released", 0, "confirmed", 0, "relay.declined/1", "done")] * 2
        assert (
            admin.execute(
                "SELECT count(*) FROM billing.review_cases WHERE request_id=ANY(%s)", (ids,)
            ).fetchone()[0]
            == 0
        )
        assert (
            admin.execute(
                "SELECT count(*) FROM billing.ledger_entries WHERE source_id=ANY(%s)", (ids,)
            ).fetchone()[0]
            == 0
        )
    finally:
        await worker.close()
        await context.__aexit__(None, None, None)


async def test_one_failing_request_does_not_hold_back_its_batch(http, admin, service_directory):
    """The batch rolls back, the others settle one by one, only the failing one is retried."""
    context, gateway = await plane_with_tenants(http, admin, 1)
    worker = worker_application(service_directory)
    try:
        ids = []
        for _ in range(10):
            rid, facts = await gateway.admit(0)
            for message in facts:
                await gateway.send(message)
            ids.append(rid)
        broken = ids[4]
        process = worker.usage._process

        async def failing(repo, account, request):
            if request.id == broken:
                raise RuntimeError("settlement bug")
            return await process(repo, account, request)

        worker.usage._process = failing
        with pytest.raises(RuntimeError, match="settlement bug"):
            await worker.usage.process()
        debited = {
            r
            for (r,) in admin.execute(
                "SELECT source_id FROM billing.ledger_entries WHERE source_id=ANY(%s)", (ids,)
            ).fetchall()
        }
        assert debited == set(ids) - {broken}
        state, failures, reason, delayed = admin.execute(
            "SELECT state,failures,reason,next_attempt_at>clock_timestamp() "
            "FROM billing.usage_jobs WHERE request_id=%s",
            (broken,),
        ).fetchone()
        assert (state, failures, reason, delayed) == ("ready", 1, "processing_retry", True)
        # Fixed, the request settles at its next due time like any other.
        worker.usage._process = process
        admin.execute(
            "UPDATE billing.usage_jobs SET next_attempt_at=clock_timestamp() WHERE request_id=%s",
            (broken,),
        )
        assert await worker.usage.process() == 1
        check_money(admin, ids)
    finally:
        await worker.close()
        await context.__aexit__(None, None, None)


async def test_batch_and_report_projection_take_report_rows_in_one_order(
    http, admin, service_directory
):
    """A batch must not lock report rows in attempt order against the projection's request order.

    Request A sorts before B, but B's attempt sorts before A's, so the ORM would write B's report
    row first. The projection holds A's row and then wants B's: without a common order, the two
    transactions wait for each other until PostgreSQL's deadlock check aborts one after 1 s.
    """
    context, gateway = await plane_with_tenants(http, admin, 1)
    worker = worker_application(service_directory)
    first, second = sorted([uuid4(), uuid4()])
    late, early = sorted([uuid4(), uuid4()], reverse=True)
    try:
        for rid, aid in ((first, late), (second, early)):
            _, facts = await gateway.admit(0, rid=rid, aid=aid)
            for message in facts:
                await gateway.send(message)
        info = admin.info
        projection = await psycopg.AsyncConnection.connect(
            host=info.host,
            port=info.port,
            dbname=info.dbname,
            user=info.user,
            password=info.password,
        )
        acknowledge = (
            "UPDATE billing.report_changes SET copied_generation=copied_generation "
            "WHERE request_id=%s"
        )
        async with projection:
            await projection.execute(acknowledge, (first,))
            settling = asyncio.create_task(worker.usage.process())
            await asyncio.sleep(0.3)
            started = time.monotonic()
            await projection.execute(acknowledge, (second,))
            waited = time.monotonic() - started
            await projection.commit()
        assert await settling == 2
        assert waited < 0.5, waited
        check_money(admin, [first, second])
    finally:
        await worker.close()
        await context.__aexit__(None, None, None)
