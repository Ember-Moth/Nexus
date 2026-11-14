"""Stateless compaction through the Rust relay, real reservation and settlement."""

import asyncio
import json
from decimal import Decimal

import pytest
from test_billing import account_amounts
from test_relay import relay_binary, tls, wait_events  # noqa: F401
from test_relay_streams import stream_plane

pytestmark = pytest.mark.integration


async def test_native_compact_to_ledger(http, admin, tls, relay_binary):  # noqa: F811
    sent = []
    historical = [{"role": "user", "content": "private-input-marker"}]
    output = [
        *historical,
        {"id": "cmp_part", "type": "compaction", "encrypted_content": "opaque-output-marker"},
    ]
    usage = {
        "input_tokens": 3,
        "input_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 0},
        "output_tokens": 2,
        "output_tokens_details": {"reasoning_tokens": 1},
        "total_tokens": 5,
    }
    compacted = {
        "object": "response.compaction",
        "id": "cmp_result",
        "created_at": 1700000000,
        "output": output,
        "usage": usage,
    }
    generated = {
        "object": "response",
        "id": "resp_followup",
        "status": "completed",
        "output": [],
        "usage": usage,
    }
    mode = "ok"

    async def upstream(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            headers = {
                k.lower(): v.strip() for line in head[1:] if line for k, v in [line.split(":", 1)]
            }
            payload = json.loads(await reader.readexactly(int(headers["content-length"])))
            path = head[0].split()[1]
            sent.append((path, headers, payload))
            answer = compacted if path.endswith("/compact") else generated
            if mode == "missing":
                answer = {k: v for k, v in answer.items() if k != "usage"}
            elif mode == "wrong_object":
                answer = generated
            elif mode == "over_bound":
                answer = compacted | {"usage": usage | {"output_tokens": 7, "total_tokens": 10}}
            data = json.dumps(answer).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode()
                + data
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0, ssl=tls[0])
    async with (
        server,
        stream_plane(
            http,
            admin,
            relay_binary,
            tls[1],
            server.sockets[0].getsockname()[1],
            "openai_compatible",
            auto_cache=True,
        ) as (client, model, key, dropped, plane),
    ):
        peer, _, org, _, deployment, *_ = plane
        path = "/v1/responses/compact"
        body = {"model": model, "instructions": "keep decisions", "input": historical}
        auth = {"Authorization": f"Bearer {key}", "X-Nexus-Cache-Key": "private-workflow"}
        assert (await client.post(path, json=body)).status_code == 401
        assert (await client.get(path, headers=auth)).status_code == 405
        for extra in [
            {"stream": True},
            {"previous_response_id": "resp_other"},
            {"max_output_tokens": 1},
        ]:
            assert (await client.post(path, headers=auth, json=body | extra)).status_code == 400
        assert sent == [] and admin.execute(
            "SELECT count(*) FROM billing.request_runs"
        ).fetchone() == (0,)
        original_balance = account_amounts(admin, org)[0]
        try:
            admin.execute(
                "UPDATE billing.accounts SET balance=0 WHERE organization_id=%s", (org["id"],)
            )
            unfunded = await client.post(path, headers=auth, json=body)
            assert (
                unfunded.status_code == 402
                and unfunded.json()["error"]["code"] == "insufficient_funds"
            )
            assert sent == []
        finally:
            admin.execute(
                "UPDATE billing.accounts SET balance=%s WHERE organization_id=%s",
                (original_balance, org["id"]),
            )
        reply = await client.post(path, headers=auth, json=body)
        assert reply.status_code == 200, reply.text
        assert reply.json() == compacted
        assert len(sent) == 1 and sent[0][0] == "/v1/responses/compact"
        assert sent[0][1]["authorization"] == "Bearer provider-secret-do-not-echo"
        assert sent[0][1]["accept"] == "application/json" and "x-nexus-cache-key" not in sent[0][1]
        forwarded = sent[0][2]
        assert len(forwarded.pop("prompt_cache_key")) == 64
        assert forwarded == body | {"model": deployment["upstream_model"]}
        await wait_events(admin, 2)
        assert dropped == ["reserve_request", "start_attempt", "append_usage_event"]
        for (envelope,) in admin.execute("SELECT envelope FROM billing.usage_events"):
            assert (await peer.call("append_usage_event", json.dumps(envelope)))[
                "status"
            ] == "accepted"
        assert await http[2].state.usage.process() == 1
        rid = reply.headers["x-request-id"]
        assert admin.execute(
            "SELECT input_limit,output_limit,charged_amount FROM billing.request_runs WHERE id=%s",
            (rid,),
        ).fetchone() == (4, 6, Decimal("3.2"))
        assert admin.execute(
            "SELECT expected_usage_format,cost_amount FROM billing.request_attempts "
            "WHERE request_id=%s",
            (rid,),
        ).fetchall() == [("openai.responses.v1", Decimal("1.28"))]
        assert account_amounts(admin, org) == (Decimal("96.8"), 0)
        assert admin.execute("SELECT spent,reserved FROM billing.budget_periods").fetchone() == (
            Decimal("3.2"),
            0,
        )
        assert admin.execute(
            "SELECT signed_amount FROM billing.ledger_entries WHERE kind='usage_debit'"
        ).fetchall() == [(Decimal("-3.2"),)]
        evidence = json.dumps(admin.execute("SELECT envelope FROM billing.usage_events").fetchall())
        for secret in ["private-input-marker", "opaque-output-marker", key]:
            assert secret not in evidence and secret not in client.relay_log.read_text()
        # The compacted output must remain usable verbatim as explicit history.
        follow = await client.post(
            "/v1/responses", headers=auth, json={"model": model, "input": reply.json()["output"]}
        )
        assert follow.status_code == 200 and follow.json() == generated
        assert sent[-1][0] == "/v1/responses" and sent[-1][2]["input"] == output
        await wait_events(admin, 4)
        assert await http[2].state.usage.process() == 1
        mode = "missing"
        missing = await client.post(path, headers=auth, json=body)
        assert missing.status_code == 200 and "usage" not in missing.json()
        await wait_events(admin, 6)
        assert await http[2].state.usage.process() == 1
        assert admin.execute(
            "SELECT state,reservation_state FROM billing.request_runs WHERE id=%s",
            (missing.headers["x-request-id"],),
        ).fetchone() == ("review", "held")
        assert admin.execute(
            "SELECT count(*) FROM billing.ledger_entries WHERE kind='usage_debit'"
        ).fetchone() == (2,)
        mode = "wrong_object"
        invalid = await client.post(path, headers=auth, json=body)
        assert invalid.status_code == 502
        await wait_events(admin, 8)
        assert await http[2].state.usage.process() == 1
        assert admin.execute(
            "SELECT state,reservation_state FROM billing.request_runs WHERE id=%s",
            (invalid.headers["x-request-id"],),
        ).fetchone() == ("review", "held")
        mode = "over_bound"
        excessive = await client.post(path, headers=auth, json=body)
        assert excessive.status_code == 200
        await wait_events(admin, 10)
        assert await http[2].state.usage.process() == 1
        assert admin.execute(
            "SELECT state,review_reason,reservation_state FROM billing.request_runs WHERE id=%s",
            (excessive.headers["x-request-id"],),
        ).fetchone() == ("review", "authorized_bound_exceeded", "held")
        assert admin.execute(
            "SELECT count(*) FROM billing.ledger_entries WHERE kind='usage_debit'"
        ).fetchone() == (2,)
