"""Relay failover: real client -> Rust relay -> two local TLS upstreams -> SQL Worker settlement."""

import asyncio
import json
from collections import defaultdict
from contextlib import asynccontextmanager
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from test_billing import headers, make_budget, publish_price
from test_catalog import make_deployment, make_upstream
from test_identity_api import auth
from test_plane import provision_plane
from test_relay import (  # noqa: F401
    relay_binary,
    relay_logs,
    relay_metrics,
    running_relay,
    tls,
    wait_events,
)
from test_relay_streams import events

pytestmark = pytest.mark.integration
BACKUP_SECRET = "backup-secret-do-not-echo"
OVERLOADED = {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
USAGE = {
    "input_tokens": 2,
    "output_tokens": 3,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
}


def marker(body):
    return body["messages"][0]["content"]


class Double:
    """A TLS Messages upstream whose behavior follows the client's text."""

    def __init__(self, name, tls_context, behave):
        self.name = name
        self.tls = tls_context
        self.behave = behave
        self.received = []
        self.arrived = defaultdict(asyncio.Event)
        self.server = None
        self.port = None

    async def handle(self, reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            hdr = {
                k.lower(): v.strip() for line in head[1:] if line for k, v in [line.split(":", 1)]
            }
            body = json.loads(await reader.readexactly(int(hdr["content-length"])))
            self.received.append((hdr, body))
            self.arrived[marker(body)].set()
            await self.behave(body, reader, writer)
        except ConnectionError, asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()

    async def start(self):
        self.server = await asyncio.start_server(
            self.handle, "127.0.0.1", self.port or 0, ssl=self.tls
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    def count(self, text):
        return sum(marker(body) == text for _, body in self.received)


async def reply(writer, status, payload):
    data = json.dumps(payload).encode()
    writer.write(
        f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n".encode()
        + f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode()
        + data
    )
    await writer.drain()


async def stream_head(writer):
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
    await writer.drain()


def frame(event):
    return f"event: {event['type']}\r\ndata: {json.dumps(event)}\r\n\r\n".encode()


async def primary(body, reader, writer):
    match marker(body):
        case "busy" | "fail":
            await reply(writer, "503 Service Unavailable", OVERLOADED)
        case "hang":
            await reader.read()
        case "stall":
            await stream_head(writer)
            await reader.read()
        case "drop":
            await stream_head(writer)
            writer.write(frame(events(True, "r-primary")[0]))
            await writer.drain()
        case _:
            await reply(writer, "500 Internal Server Error", OVERLOADED)


async def backup(body, reader, writer):
    if marker(body) == "fail":
        await reply(writer, "529 Overloaded", OVERLOADED)
    elif body.get("stream"):
        await stream_head(writer)
        for event in events(True, "r-backup"):
            writer.write(frame(event))
        await writer.drain()
    else:
        await reply(
            writer,
            "200 OK",
            {
                "id": "msg_backup",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "from backup"}],
                "stop_reason": "end_turn",
                "usage": USAGE,
            },
        )


@asynccontextmanager
async def failover_plane(http, admin, ports):
    client, root, app, _ = http
    async with provision_plane(
        http, admin, "anthropic", base_url=f"https://127.0.0.1:{ports[0]}/v1"
    ) as plane:
        peer, private, org, model, first, admission, token, cid = plane
        await make_budget(client, root, org, "100")
        upstream = await make_upstream(
            client, root, "Backup", "anthropic", base_url=f"https://127.0.0.1:{ports[1]}/v1"
        )
        credential = await client.post(
            f"/api/v1/upstreams/{upstream['id']}/credentials",
            headers=auth(root),
            json={"name": "Backup", "secret": BACKUP_SECRET},
        )
        assert credential.status_code == 201, credential.text
        second = await make_deployment(client, root, model, upstream, credential.json())
        await publish_price(client, root, "cost", second, "0.2")
        profile = {
            "endpoint": "anthropic_messages",
            "transports": ["http"],
            "input_ceiling": 4,
            "output_ceiling": 6,
            "streaming": True,
            "tools": True,
            "bound_reference": "isolated-failover",
            "count_rule": "deployment_ceiling.v1",
        }
        for dep in (first, second):
            r = await client.patch(
                f"/api/v1/deployments/{dep['id']}",
                headers=auth(root, dep["version"]),
                json={"relay_profile": profile},
            )
            assert r.status_code == 200, r.text
        r = await client.put(
            f"/api/v1/route-policies/{org['id']}/{model['id']}",
            headers=auth(root, 1),
            json={
                "candidates": [
                    {"deployment_id": first["id"], "priority": 0},
                    {"deployment_id": second["id"], "priority": 1},
                ],
            },
        )
        assert r.status_code == 200, r.text
        version = admin.execute(
            "SELECT version FROM publication.gateway_clusters WHERE id=%s", (cid,)
        ).fetchone()[0]
        r = await client.patch(
            f"/api/v1/gateway-clusters/{cid}",
            headers=auth(root, version),
            json={"deployment_ids": [first["id"], second["id"]]},
        )
        assert r.status_code == 200, r.text
        nid, role = admin.execute(
            "SELECT node_id,database_role FROM publication.gateway_principals WHERE cluster_id=%s",
            (cid,),
        ).fetchone()
        caps = ["reservation.v1", "usage-events.v1", "relay.native.v1", "relay.sse.v1"]
        r = await client.put(
            f"/api/v1/gateway-clusters/{cid}/principals/{nid}",
            headers=auth(root),
            json={"node_id": str(nid), "database_role": role, "capabilities": caps},
        )
        assert r.status_code == 200, r.text
        r = await client.post(
            f"/api/v1/gateway-clusters/{cid}/releases",
            headers=headers(root, "failover"),
            json={},
        )
        assert r.status_code == 202, r.text
        while await app.state.publication.distribute():
            pass
        yield plane, UUID(first["id"]), UUID(second["id"])


class Ledger:
    """Per-request facts, so that concurrent requests do not blur the assertions."""

    def __init__(self, admin, peer):
        self.admin = admin
        self.peer = peer
        self.seen = set()

    def new(self):
        rows = {r for (r,) in self.admin.execute("SELECT id FROM billing.request_runs")}
        fresh = rows - self.seen
        self.seen |= fresh
        assert len(fresh) == 1, fresh
        return fresh.pop()

    def attempts(self, rid):
        return self.admin.execute(
            "SELECT id,sequence,deployment_id,cost_amount FROM billing.request_attempts "
            "WHERE request_id=%s ORDER BY sequence",
            (rid,),
        ).fetchall()

    def finished(self, rid):
        return {
            attempt: envelope["payload"]
            for attempt, envelope in self.admin.execute(
                "SELECT attempt_id,envelope FROM billing.usage_events "
                "WHERE request_id=%s AND kind='attempt.finished'",
                (rid,),
            )
        }

    def closed(self, rid):
        (envelope,) = self.admin.execute(
            "SELECT envelope FROM billing.usage_events WHERE request_id=%s "
            "AND kind='request.closed'",
            (rid,),
        ).fetchone()
        return envelope["payload"]

    def debits(self, rid):
        return self.admin.execute(
            "SELECT signed_amount FROM billing.ledger_entries "
            "WHERE kind='usage_debit' AND source_id=%s",
            (rid,),
        ).fetchall()

    def reviews(self, rid):
        return dict(
            self.admin.execute(
                "SELECT source_id,reason FROM billing.review_cases "
                "WHERE request_id=%s AND source_type='attempt'",
                (rid,),
            ).fetchall()
        )

    async def status(self, rid):
        return await self.peer.call("get_request_status", rid)


async def test_failover_to_backup_deployment(http, admin, tls, relay_binary, monkeypatch):  # noqa: F811
    app = http[2]
    settings = http[3]
    monkeypatch.setenv("NEXUS_RELAY_STREAM_IDLE_MS", "1000")
    first_upstream = Double("primary", tls[0], primary)
    second_upstream = Double("backup", tls[0], backup)
    await first_upstream.start()
    await second_upstream.start()
    ports = (first_upstream.port, second_upstream.port)
    async with failover_plane(http, admin, ports) as (plane, first, second):
        peer, private, _, model, _, _, token, cid = plane
        ledger = Ledger(admin, peer)
        info = peer.connection.info
        hdr = {"x-api-key": token}
        events_seen = 0

        def body(text, stream=False):
            return {
                "model": model["public_name"],
                "max_tokens": 6,
                "stream": stream,
                "messages": [{"role": "user", "content": text}],
            }

        async def settled(expected):
            nonlocal events_seen
            events_seen += expected
            await wait_events(admin, events_seen)
            await app.state.usage.process()

        def relay():
            return running_relay(relay_binary, peer, private, cid, settings, tls[1], info.port)

        # One attempt is the previous behavior: the primary's 503 reaches the client.
        monkeypatch.setenv("NEXUS_RELAY_MAX_ATTEMPTS", "1")
        async with relay() as client:
            result = await client.post("/v1/messages", headers=hdr, json=body("busy"))
            assert result.status_code == 503 and result.json() == OVERLOADED
            rid = ledger.new()
            await settled(2)
            assert [a[2] for a in ledger.attempts(rid)] == [first]
            assert ledger.closed(rid)["delivery_outcome"] == "upstream_failed"
            assert second_upstream.received == []
            assert (await ledger.status(rid))["reservation_state"] == "released"

        monkeypatch.setenv("NEXUS_RELAY_MAX_ATTEMPTS", "2")
        async with relay() as client:
            # The primary receives this POST and never answers. A read timeout after the
            # request was sent may follow execution, so it is never sent again.
            hanging = asyncio.create_task(
                client.post("/v1/messages", headers=hdr, json=body("hang"), timeout=45)
            )
            await asyncio.wait_for(first_upstream.arrived["hang"].wait(), 5)
            hang_rid = ledger.new()

            # A 503 was declined before execution: one new attempt on the backup.
            result = await client.post("/v1/messages", headers=hdr, json=body("busy"))
            assert result.status_code == 200, result.text
            assert result.json()["id"] == "msg_backup"
            rid = ledger.new()
            await settled(3)
            assert first_upstream.count("busy") == 2 and second_upstream.count("busy") == 1
            backup_hdr, backup_body = second_upstream.received[-1]
            assert backup_hdr["x-api-key"] == BACKUP_SECRET and backup_body["max_tokens"] == 6
            (a1, seq1, dep1, cost1), (a2, seq2, dep2, cost2) = ledger.attempts(rid)
            assert (seq1, dep1, seq2, dep2) == (1, first, 2, second)
            facts = ledger.finished(rid)
            assert facts[a1]["outcome"] == "failed" and facts[a1]["raw_usage"] is None
            assert facts[a2]["outcome"] == "succeeded"
            assert ledger.closed(rid) == {
                "delivery_outcome": "succeeded",
                "attempt_ids": [str(a1), str(a2)],
                "delivered_attempt_id": str(a2),
            }
            status = await ledger.status(rid)
            assert status["charged_amount"] == "5.000000000000"
            assert status["reservation_state"] == "captured"
            assert ledger.debits(rid) == [(Decimal(-5),)]
            # Each attempt's cost stands alone: the backup's priced usage and the primary's
            # missing evidence, which goes to its own review instead of a zero.
            assert cost1 is None and cost2 == Decimal("1.0")
            assert ledger.reviews(rid) == {a1: "missing_usage"}

            # A 2xx stream already executed the generation even before any client byte.
            result = await client.post("/v1/messages", headers=hdr, json=body("stall", True))
            assert result.status_code == 502
            rid = ledger.new()
            await settled(2)
            assert len(ledger.attempts(rid)) == 1 and second_upstream.count("stall") == 0
            assert ledger.closed(rid)["delivery_outcome"] == "unknown"

            # Streamed output reached the client before the upstream disconnected.
            lines = []
            with pytest.raises(httpx.RemoteProtocolError):
                async with client.stream(
                    "POST", "/v1/messages", headers=hdr, json=body("drop", True)
                ) as stream:
                    assert stream.status_code == 200
                    async for line in stream.aiter_lines():
                        lines.append(line)
            assert lines[0] == "event: message_start"
            rid = ledger.new()
            await settled(2)
            assert len(ledger.attempts(rid)) == 1 and second_upstream.count("drop") == 0
            assert ledger.closed(rid)["delivery_outcome"] == "unknown"

            # Every candidate declined: the last error is returned and the money is released.
            result = await client.post("/v1/messages", headers=hdr, json=body("fail"))
            assert result.status_code == 529 and result.json() == OVERLOADED
            rid = ledger.new()
            await settled(3)
            assert first_upstream.count("fail") == 1 and second_upstream.count("fail") == 1
            (a1, _, dep1, _), (a2, _, dep2, _) = ledger.attempts(rid)
            assert (dep1, dep2) == (first, second)
            assert {f["outcome"] for f in ledger.finished(rid).values()} == {"failed"}
            assert ledger.closed(rid) == {
                "delivery_outcome": "upstream_failed",
                "attempt_ids": [str(a1), str(a2)],
                "delivered_attempt_id": None,
            }
            status = await ledger.status(rid)
            assert status["reservation_state"] == "released" and ledger.debits(rid) == []
            assert status["charged_amount"] == "0.000000000000"

            result = await hanging
            assert result.status_code == 502
            await settled(2)
            assert len(ledger.attempts(hang_rid)) == 1 and second_upstream.count("hang") == 0
            assert ledger.closed(hang_rid)["delivery_outcome"] == "unknown"
            status = await ledger.status(hang_rid)
            assert status["reservation_state"] == "held" and status["state"] == "review"

            # The primary now refuses connections: its request never left this node.
            await first_upstream.stop()
            before = len(first_upstream.received)
            result = await client.post("/v1/messages", headers=hdr, json=body("refused"))
            assert result.status_code == 200 and result.json()["id"] == "msg_backup"
            rid = ledger.new()
            await settled(3)
            (a1, _, dep1, _), (a2, _, dep2, _) = ledger.attempts(rid)
            assert (dep1, dep2) == (first, second)
            assert ledger.finished(rid)[a1]["outcome"] == "failed"
            assert ledger.closed(rid)["delivered_attempt_id"] == str(a2)
            assert ledger.debits(rid) == [(Decimal(-5),)]

            async with client.stream(
                "POST", "/v1/messages", headers=hdr, json=body("refused-stream", True)
            ) as stream:
                assert stream.status_code == 200
                data = [
                    json.loads(line[6:])
                    async for line in stream.aiter_lines()
                    if line.startswith("data: ")
                ]
            assert data == events(True, "r-backup")
            rid = ledger.new()
            await settled(3)
            assert [a[2] for a in ledger.attempts(rid)] == [first, second]
            assert ledger.debits(rid) == [(Decimal(-5),)]
            assert len(first_upstream.received) == before
            assert second_upstream.count("refused-stream") == 1

            # busy and fail switched after a 503, refused and refused-stream after a refused
            # connection; fail's backup then declined with 529 at the attempt limit.
            samples = await relay_metrics(client)
            decisions = {
                name: value
                for name, value in samples.items()
                if name.startswith("nexus_relay_failover_decisions_total")
            }
            label = 'nexus_relay_failover_decisions_total{{reason="{}",result="{}"}}'
            assert decisions == {
                label.format("status_503", "switched"): 2,
                label.format("connect", "switched"): 2,
                label.format("status_529", "attempt_limit"): 1,
            }
            # HTTP: hang 1, busy 2, fail 2, refused 2. SSE: stall 1, drop 1, refused-stream 2.
            for transport, count, total in [("http", 4, 7), ("sse", 3, 4)]:
                series = f'endpoint="anthropic_messages",transport="{transport}"'
                assert samples[f"nexus_relay_request_attempts_count{{{series}}}"] == count
                assert samples[f"nexus_relay_request_attempts_sum{{{series}}}"] == total
            declined = [
                r for r in relay_logs(client) if r["message"] == "upstream attempt declined"
            ]
            switched = [r for r in declined if r["outcome"] == "switched"]
            assert len(declined) == 5 and len(switched) == 4
            assert {
                (r["attempt"], r["deployment_id"], r["next_deployment_id"]) for r in switched
            } == {(1, str(first), str(second))}
            (limit,) = [r for r in declined if r["outcome"] == "attempt_limit"]
            assert (limit["attempt"], limit["deployment_id"], limit["reason"]) == (
                2,
                str(second),
                "status_529",
            )
            assert "next_deployment_id" not in limit
            finished = [r for r in relay_logs(client) if r["message"] == "request finished"]
            assert sorted(r["attempts"] for r in finished) == [1, 1, 1, 2, 2, 2, 2]
            logs = client.relay_log.read_text()
            for secret in [BACKUP_SECRET, "provider-secret-do-not-echo", token]:
                assert secret not in logs

        # One attempt keeps the previous single-attempt report for the same failure.
        monkeypatch.setenv("NEXUS_RELAY_MAX_ATTEMPTS", "1")
        async with relay() as client:
            calls = len(second_upstream.received)
            result = await client.post("/v1/messages", headers=hdr, json=body("refused"))
            assert result.status_code == 502
            rid = ledger.new()
            await settled(2)
            assert [a[2] for a in ledger.attempts(rid)] == [first]
            assert ledger.closed(rid)["delivery_outcome"] == "unknown"
            assert len(second_upstream.received) == calls
        await second_upstream.stop()


@pytest.mark.parametrize("gate", ["budget", "lease", "disconnect"])
async def test_failover_rechecks_after_attempt_commit(
    http,
    admin,
    tls,  # noqa: F811
    relay_binary,  # noqa: F811
    monkeypatch,
    gate,  # noqa: F811
):  # noqa: F811
    # Delay only the first attempt's evidence, after attempt two has committed.
    # This is the window between fallback selection and the actual backup POST.
    monkeypatch.setenv("NEXUS_RELAY_FAILOVER_BUDGET_MS", "200" if gate == "budget" else "10000")
    one = Double("primary", tls[0], primary)
    two = Double("backup", tls[0], backup)
    await one.start()
    await two.start()
    try:
        async with failover_plane(http, admin, (one.port, two.port)) as (plane, first, second):
            peer, private, _, model, _, _, key, cid = plane
            ledger = Ledger(admin, peer)
            async with running_relay(
                relay_binary, peer, private, cid, http[3], tls[1], peer.connection.info.port
            ) as client:
                # A SHARE lock on just the inbox does not prevent start_attempt from reading it.
                with admin.transaction():
                    admin.execute("LOCK TABLE billing.usage_events IN SHARE MODE")
                    request = asyncio.create_task(
                        client.post(
                            "/v1/messages",
                            headers={"x-api-key": key},
                            json={
                                "model": model["public_name"],
                                "messages": [{"role": "user", "content": "busy"}],
                                "max_tokens": 6,
                            },
                        )
                    )
                    async with asyncio.timeout(5):
                        while admin.execute(  # noqa: ASYNC110 - observe a separate SQL transaction
                            "SELECT count(*) FROM billing.request_attempts"
                        ).fetchone() != (2,):
                            await asyncio.sleep(0.01)
                    if gate == "disconnect":
                        request.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await request
                        await asyncio.sleep(0.1)
                    else:
                        await asyncio.sleep(0.35 if gate == "budget" else 0.85)
                    assert not two.received
                if gate != "disconnect":
                    response = await request
                    assert response.status_code == 503 and response.json() == OVERLOADED
                await wait_events(admin, 3)
                await http[2].state.usage.process()
                rid = ledger.new()
                (a1, _, d1, _), (a2, _, d2, _) = ledger.attempts(rid)
                assert (d1, d2) == (first, second) and not two.received
                assert set(ledger.finished(rid)) == {a1, a2}
                assert ledger.closed(rid)["attempt_ids"] == [str(a1), str(a2)]
                assert (
                    ledger.closed(rid)["delivered_attempt_id"] is None and ledger.debits(rid) == []
                )
                assert ledger.closed(rid)["delivery_outcome"] == "upstream_failed"
                assert ledger.finished(rid)[a2]["outcome"] == (
                    "cancelled" if gate == "disconnect" else "failed"
                )
                assert (await ledger.status(rid))["reservation_state"] == "released"
                assert ledger.finished(rid)[a2]["raw_usage"] is None
    finally:
        await one.stop()
        await two.stop()


async def test_ambiguous_gateway_errors_do_not_replay_generation(http, admin, tls, relay_binary):  # noqa: F811
    async def intermediary(body, reader, writer):
        if marker(body) == "gateway":
            await reply(writer, "502 Bad Gateway", OVERLOADED)
        else:
            await reply(
                writer,
                "503 Service Unavailable",
                {"type": "error", "error": {"type": "gateway_error", "message": "lost response"}},
            )

    one = Double("primary", tls[0], intermediary)
    two = Double("backup", tls[0], backup)
    await one.start()
    await two.start()
    try:
        async with failover_plane(http, admin, (one.port, two.port)) as (plane, _, _):
            peer, private, _, model, _, _, key, cid = plane
            ledger = Ledger(admin, peer)
            async with running_relay(
                relay_binary, peer, private, cid, http[3], tls[1], peer.connection.info.port
            ) as client:
                for n, (label, code) in enumerate([("gateway", 502), ("ambiguous", 503)], start=1):
                    response = await client.post(
                        "/v1/messages",
                        headers={"x-api-key": key},
                        json={
                            "model": model["public_name"],
                            "messages": [{"role": "user", "content": label}],
                            "max_tokens": 6,
                            "stream": n == 2,
                        },
                    )
                    assert response.status_code == code
                    await wait_events(admin, 2 * n)
                    await http[2].state.usage.process()
                    rid = ledger.new()
                    assert one.count(label) == 1 and not two.received
                    assert len(ledger.attempts(rid)) == 1 and ledger.debits(rid) == []
                    assert ledger.closed(rid)["delivery_outcome"] == "unknown"
                    assert (await ledger.status(rid))["reservation_state"] == "held"
    finally:
        await one.stop()
        await two.stop()
