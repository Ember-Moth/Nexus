"""Compare translated and native requests against the same HTTPS deployment."""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from test_billing import account_amounts
from test_relay import relay_binary, tls, wait_events  # noqa: F401
from test_relay_streams import stream_plane, unsealed, usage

pytestmark = pytest.mark.integration


def scenarios(model):
    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
        "additionalProperties": False,
    }
    messages = {
        "model": model,
        "max_tokens": 6,
        "system": "Look up the answer.",
        "tools": [
            {"name": "lookup", "description": "Search", "input_schema": schema, "strict": False}
        ],
        "tool_choice": {"type": "auto", "disable_parallel_tool_use": False},
        "messages": [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "history", "name": "lookup", "input": {"q": "京都"}}
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "history", "content": "ok"}],
            },
        ],
    }
    responses = {
        "model": model,
        "max_output_tokens": 6,
        "store": False,
        "instructions": "Look up the answer.",
        "tools": [
            {
                "type": "function",
                "name": "lookup",
                "description": "Search",
                "parameters": schema,
                "strict": False,
            }
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "input": [
            {"role": "user", "content": "hello"},
            {
                "type": "function_call",
                "name": "lookup",
                "call_id": "history",
                "arguments": '{"q":"京都"}',
            },
            {"type": "function_call_output", "call_id": "history", "output": "ok"},
        ],
    }
    return messages, responses


def output(messages):
    if messages:
        return {
            "type": "message",
            "id": "r1",
            "role": "assistant",
            "model": "deployed",
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": usage(True),
            "content": [
                {"type": "text", "text": "京都"},
                {"type": "tool_use", "name": "lookup", "id": "first", "input": {"x": 1}},
                {"type": "tool_use", "name": "lookup", "id": "second", "input": {"y": 2}},
            ],
        }
    return {
        "object": "response",
        "id": "r1",
        "status": "completed",
        "usage": usage(False),
        "output": [
            {
                "type": "message",
                "id": "msg1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "京都", "annotations": []}],
            },
            {
                "type": "function_call",
                "id": "item1",
                "status": "completed",
                "call_id": "first",
                "name": "lookup",
                "arguments": '{"x":1}',
            },
            {
                "type": "function_call",
                "id": "item2",
                "status": "completed",
                "call_id": "second",
                "name": "lookup",
                "arguments": '{"y":2}',
            },
        ],
    }


def stream_events(messages, full):
    if messages:
        yield {
            "type": "message_start",
            "message": {
                **full,
                "content": [],
                "stop_reason": None,
                "usage": {**usage(True), "output_tokens": 0},
            },
        }
        for i, part in enumerate(full["content"]):
            tool = part["type"] == "tool_use"
            yield {
                "type": "content_block_start",
                "index": i,
                "content_block": {**part, **({"input": {}} if tool else {"text": ""})},
            }
            delta = (
                {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(part["input"], separators=(",", ":")),
                }
                if tool
                else {"type": "text_delta", "text": part["text"]}
            )
            yield {"type": "content_block_delta", "index": i, "delta": delta}
            yield {"type": "content_block_stop", "index": i}
        yield {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 3},
        }
        yield {"type": "message_stop"}
    else:
        yield {
            "type": "response.created",
            "response": {**full, "output": [], "status": "in_progress", "usage": None},
        }
        for i, item in enumerate(full["output"]):
            tool = item["type"] == "function_call"
            yield {
                "type": "response.output_item.added",
                "output_index": i,
                "item": {
                    **item,
                    "status": "in_progress",
                    **({"arguments": ""} if tool else {"content": []}),
                },
            }
            if not tool:
                yield {
                    "type": "response.content_part.added",
                    "output_index": i,
                    "content_index": 0,
                    "item_id": item["id"],
                    "part": {"type": "output_text", "text": "", "annotations": []},
                }
                yield {
                    "type": "response.output_text.delta",
                    "output_index": i,
                    "content_index": 0,
                    "item_id": item["id"],
                    "delta": "京都",
                }
                yield {"type": "response.output_item.done", "output_index": i, "item": item}
        # Two tools interleave. The second finishes first.
        for i, delta in [(1, '{"x":'), (2, '{"y":'), (2, "2}"), (1, "1}")]:
            yield {
                "type": "response.function_call_arguments.delta",
                "output_index": i,
                "item_id": full["output"][i]["id"],
                "delta": delta,
            }
        for i in [2, 1]:
            yield {
                "type": "response.output_item.done",
                "output_index": i,
                "item": full["output"][i],
            }
        yield {"type": "response.completed", "response": full}


def normalized_request(body, messages):
    body = json.loads(json.dumps(body))
    if messages:
        for msg in body["messages"]:
            if isinstance(msg["content"], str):
                msg["content"] = [{"type": "text", "text": msg["content"]}]
    else:
        for item in body["input"]:
            if item.get("type") == "function_call":
                item["arguments"] = json.loads(item["arguments"])
    return body


@pytest.mark.parametrize("family", ["anthropic", "openai_compatible"])
async def test_conversion_matches_native(http, admin, tls, relay_binary, family):  # noqa: F811
    messages = family == "anthropic"
    full = output(messages)
    seen = []
    mode = "success"
    gate = asyncio.Event()

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            headers = {
                k.lower(): v.strip() for line in head[1:] if line for k, v in [line.split(":", 1)]
            }
            data = json.loads(await reader.readexactly(int(headers["content-length"])))
            seen.append((head[0], headers, data))
            if mode == "error":
                err = (
                    {"type": "error", "error": {"type": "overloaded_error", "message": "synthetic"}}
                    if messages
                    else {
                        "error": {
                            "type": "server_error",
                            "code": "server_error",
                            "message": "synthetic",
                        }
                    }
                )
                packet = json.dumps(err).encode()
                writer.write(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(packet)}\r\nConnection: close\r\n\r\n".encode()
                    + packet
                )
            elif data.get("stream"):
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Connection: close\r\n\r\n"
                )
                for n, event in enumerate(stream_events(messages, full)):
                    if n == 1:
                        if mode == "truncated":
                            return
                        if mode == "stream_error":
                            event = {
                                "type": "error",
                                "error": {"type": "server_error", "message": "in-band failure"},
                            }
                        await gate.wait()
                    wire = (
                        f"event: {event['type']}\r\ndata: "
                        + json.dumps(event, ensure_ascii=False)
                        + "\r\n\r\n"
                    ).encode()
                    for start in range(0, len(wire), 7):
                        writer.write(wire[start : start + 7])
                        await writer.drain()
                    if mode == "stream_error" and n == 1:
                        break
            else:
                payload = full.copy()
                if mode == "missing":
                    payload.pop("usage")
                elif mode == "private_output":
                    if messages:
                        payload["content"] = [{"type": "unmapped_vendor_block", "value": "private"}]
                    else:
                        payload["output"] = [
                            {"type": "reasoning", "id": "private", "encrypted_content": "fixture"}
                        ]
                elif mode == "limit":
                    if messages:
                        payload["stop_reason"] = "max_tokens"
                    else:
                        payload["status"] = "incomplete"
                        payload["incomplete_details"] = {"reason": "max_output_tokens"}
                packet = json.dumps(payload).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(packet)}\r\nConnection: close\r\n\r\n".encode()
                    + packet
                )
            await writer.drain()
        finally:
            writer.close()

    provider_server = await asyncio.start_server(provider, "127.0.0.1", 0, ssl=tls[0])
    client_endpoint = "openai_responses" if messages else "anthropic_messages"
    async with (
        provider_server,
        stream_plane(
            http,
            admin,
            relay_binary,
            tls[1],
            provider_server.sockets[0].getsockname()[1],
            family,
            convert_from=client_endpoint,
        ) as (public, model, key, dropped, plane),
    ):
        paths = ["/v1/messages", "/v1/responses"]
        hdrs = [{"x-api-key": key}, {"authorization": f"Bearer {key}"}]
        native = int(not messages)
        cross = 1 - native
        body = scenarios(model)
        # Unknown billing-affecting fields/private history must fail before reserving or sending.
        unsupported = (
            {"reasoning": {"effort": "minimal"}} if messages else {"stop_sequences": ["END"]}
        )
        r = await public.post(
            paths[cross], headers=hdrs[cross], json={**body[cross], **unsupported}
        )
        assert r.status_code == 400, r.text
        assert not seen and admin.execute(
            "SELECT count(*) FROM billing.request_runs"
        ).fetchone() == (0,)
        converted = None
        for i in (cross, native):
            r = await public.post(paths[i], headers=hdrs[i], json=body[i])
            assert r.status_code == 200, r.text
            answer = r.json()
            if i == cross:
                converted = answer
            if i == native:
                assert unsealed(answer) == full
            elif messages:
                assert answer["status"] == "completed" and answer["model"] == model
                assert answer["output"][0]["content"][0]["text"] == "京都"
                assert [
                    (x["call_id"], json.loads(x["arguments"])) for x in answer["output"][1:]
                ] == [("first", {"x": 1}), ("second", {"y": 2})]
                assert answer["usage"]["total_tokens"] == 5
            else:
                assert answer["stop_reason"] == "tool_use" and answer["model"] == model
                assert answer["content"] == output(True)["content"]
            await wait_events(admin, 2 if i == cross else 4)
        _, _, org, _, dep, _, _, _ = plane
        for line, h, req in seen:
            assert (
                line == f"POST {paths[native]} HTTP/1.1" and req["model"] == dep["upstream_model"]
            )
            assert h["x-api-key" if messages else "authorization"] == (
                "provider-secret-do-not-echo" if messages else "Bearer provider-secret-do-not-echo"
            )
        assert normalized_request(seen[0][2], messages) == normalized_request(seen[1][2], messages)
        async with public.stream(
            "POST", paths[cross], headers=hdrs[cross], json={**body[cross], "stream": True}
        ) as r:
            assert r.status_code == 200
            lines = r.aiter_lines()
            first = await asyncio.wait_for(anext(lines), 3)
            assert first == "event: " + ("response.created" if messages else "message_start")
            gate.set()
            frames = [json.loads(line[6:]) async for line in lines if line.startswith("data: ")]
        if messages:
            assert [f["sequence_number"] for f in frames] == list(range(len(frames)))
            assert (
                frames[-1]["type"] == "response.completed"
                and frames[-1]["response"]["output"] == converted["output"]
            )
        else:
            assert [f["index"] for f in frames if f["type"] == "content_block_stop"] == [0, 1, 2]
            assert [
                f["content_block"]["id"]
                for f in frames
                if f["type"] == "content_block_start" and f["content_block"]["type"] == "tool_use"
            ] == ["first", "second"]
            for idx, expected in [(1, {"x": 1}), (2, {"y": 2})]:
                assert (
                    json.loads(
                        "".join(
                            f["delta"]["partial_json"]
                            for f in frames
                            if f["type"] == "content_block_delta" and f["index"] == idx
                        )
                    )
                    == expected
                )
            assert (
                frames[-2]["delta"]["stop_reason"] == "tool_use"
                and frames[-1]["type"] == "message_stop"
            )
        await wait_events(admin, 6)
        r = await public.post(
            paths[native], headers=hdrs[native], json={**body[native], "stream": True}
        )
        assert r.status_code == 200
        await wait_events(admin, 8)
        for (envelope,) in admin.execute("SELECT envelope FROM billing.usage_events"):
            assert (await plane[0].call("append_usage_event", json.dumps(envelope)))[
                "status"
            ] == "accepted"
        assert await http[2].state.usage.process() == 4
        assert (
            dropped == ["reserve_request", "start_attempt", "append_usage_event"] and len(seen) == 4
        )
        fmt = "anthropic.messages.v1" if messages else "openai.responses.v1"
        assert (
            admin.execute(
                "SELECT expected_usage_format,cost_amount FROM billing.request_attempts"
            ).fetchall()
            == [(fmt, 2)] * 4
        )
        assert (
            admin.execute("SELECT quantities FROM billing.normalized_usage").fetchall()
            == [({"input_uncached_tokens": 2, "output_tokens": 3},)] * 4
        )
        assert (
            admin.execute("SELECT charged_amount FROM billing.request_runs").fetchall()
            == [(5,)] * 4
        )
        assert admin.execute("SELECT spent,reserved FROM billing.budget_periods").fetchone() == (
            20,
            0,
        )
        assert account_amounts(admin, org) == (
            80,
            0,
        )
        mode = "error"
        r = await public.post(
            paths[cross], headers=hdrs[cross], json={**body[cross], "stream": True}
        )
        assert r.status_code == 503 and r.json()["error"]["message"] == "synthetic"
        assert ("type" in r.json()) == bool(not messages)
        await wait_events(admin, 10)
        await http[2].state.usage.process()
        # A Messages overloaded_error confirms rejection. A Responses server_error
        # from an intermediary does not establish whether generation already ran.
        rejection_hold = 0 if messages else 10
        assert admin.execute(
            "SELECT held_amount FROM billing.accounts WHERE organization_id=%s", (org["id"],)
        ).fetchone() == (rejection_hold,)
        mode = "truncated"
        with pytest.raises(httpx.RemoteProtocolError):
            await public.post(
                paths[cross], headers=hdrs[cross], json={**body[cross], "stream": True}
            )
        await wait_events(admin, 12)
        await http[2].state.usage.process()
        assert account_amounts(admin, org) == (
            80,
            rejection_hold + 10,
        )
        mode = "limit"
        r = await public.post(paths[cross], headers=hdrs[cross], json=body[cross])
        assert r.status_code == 200
        if messages:
            assert (
                r.json()["status"] == "incomplete"
                and r.json()["incomplete_details"]["reason"] == "max_output_tokens"
            )
        else:
            assert r.json()["stop_reason"] == "max_tokens"
        await wait_events(admin, 14)
        await http[2].state.usage.process()
        mode = "missing"
        r = await public.post(paths[cross], headers=hdrs[cross], json=body[cross])
        assert r.status_code == 200
        assert r.json()["usage"] == (None if messages else {"input_tokens": 0, "output_tokens": 0})
        await wait_events(admin, 16)
        assert admin.execute(
            "SELECT count(*) FROM billing.usage_events WHERE kind='attempt.finished' "
            "AND envelope->'payload'->>'outcome'='succeeded' "
            "AND envelope->'payload'->'raw_usage'='null'::jsonb"
        ).fetchone() == (1,)
        assert await http[2].state.usage.process() == 1
        assert account_amounts(admin, org) == (
            75,
            rejection_hold + 20,
        )
        assert admin.execute("SELECT spent,reserved FROM billing.budget_periods").fetchone() == (
            25,
            rejection_hold + 20,
        )
        mode = "private_output"
        r = await public.post(paths[cross], headers=hdrs[cross], json=body[cross])
        assert r.status_code == 502
        await wait_events(admin, 18)
        assert await http[2].state.usage.process() == 1
        assert account_amounts(admin, org) == (
            75,
            rejection_hold + 20,
        )
        assert admin.execute(
            "SELECT count(*) FROM billing.request_attempts WHERE cost_amount=2"
        ).fetchone() == (6,)
        assert admin.execute(
            "SELECT count(*) FROM billing.ledger_entries WHERE kind='usage_debit'"
        ).fetchone() == (5,)
        assert len(seen) == 9
        mode = "stream_error"
        r = await public.post(
            paths[cross], headers=hdrs[cross], json={**body[cross], "stream": True}
        )
        assert r.status_code == 200
        frames = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
        assert frames[-1]["type"] == "error"
        assert (
            frames[-1]["message"] if messages else frames[-1]["error"]["message"]
        ) == "in-band failure"
        assert not any(f["type"] in {"message_stop", "response.completed"} for f in frames)
        await wait_events(admin, 20)
        await http[2].state.usage.process()
        assert account_amounts(admin, org) == (
            75,
            rejection_hold + 20,
        )


@pytest.mark.parametrize("family", ["anthropic", "openai_compatible"])
async def test_cache_usage(http, admin, tls, relay_binary, family):  # noqa: F811
    from test_identity_api import auth

    messages = family == "anthropic"
    client_is_messages = not messages
    upstream_bodies = []

    # A deterministic local provider, with an explicit cold write then a read on append.
    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            fields = {
                k.lower(): v.strip() for line in head[1:] if line for k, v in [line.split(":", 1)]
            }
            req = json.loads(await reader.readexactly(int(fields["content-length"])))
            assert "x-nexus-cache-key" not in fields
            answer = output(messages)
            history = "messages" if messages else "input"
            previous = upstream_bodies[-1] if upstream_bodies else None
            enabled = (
                req.get("cache_control") == {"type": "ephemeral"}
                if messages
                else (
                    req.get("prompt_cache_options") == {"mode": "explicit"}
                    and req["input"][0]["content"][0].get("prompt_cache_breakpoint")
                    == {"mode": "explicit"}
                )
            )
            hit = bool(
                previous
                and enabled
                and req[history][: len(previous[history])] == previous[history]
                and req.get("prompt_cache_key") == previous.get("prompt_cache_key")
                and req.get("tools") == previous.get("tools")
            )
            upstream_bodies.append(req)
            reads, writes = (2, 0) if hit else (0, 2)
            answer["usage"] = (
                {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cache_read_input_tokens": reads,
                    "cache_creation_input_tokens": writes,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": writes,
                        "ephemeral_1h_input_tokens": 0,
                    },
                }
                if messages
                else {
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "input_tokens_details": {
                        "cached_tokens": reads,
                        "cache_write_tokens": writes,
                        "cache_write_tokens_details": {
                            "ttl_30m_tokens": writes,
                            "ttl_12h_tokens": 0,
                        },
                    },
                }
            )
            packet = json.dumps(answer).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(packet)}\r\nConnection: close\r\n\r\n".encode()
                + packet
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(provider, "127.0.0.1", 0, ssl=tls[0])
    async with (
        server,
        stream_plane(
            http,
            admin,
            relay_binary,
            tls[1],
            server.sockets[0].getsockname()[1],
            family,
            convert_from="anthropic_messages" if client_is_messages else "openai_responses",
            auto_cache=True,
        ) as (relay, model, key, _, plane),
    ):
        body = scenarios(model)[0 if client_is_messages else 1]
        if client_is_messages:
            body["system"] = [
                {"type": "text", "text": "stable rules", "cache_control": {"type": "ephemeral"}}
            ]
        else:
            body["prompt_cache_key"] = "shared-workflow"
        # An enabled deployment keeps its price dimensions even after later model edits.
        model_path = "/api/v1/models/" + plane[3]["id"]
        before = (await http[0].get(model_path, headers=auth(http[1]))).json()
        rejected = await http[0].patch(
            model_path,
            headers=auth(http[1], before["version"]),
            json={"token_dimensions": ["input_uncached_tokens", "output_tokens"]},
        )
        assert (
            rejected.status_code == 422
            and rejected.json()["error"]["code"] == "price_dimensions_mismatch"
        )
        history = body["messages" if client_is_messages else "input"]
        hdr = {"x-api-key": key} if client_is_messages else {"Authorization": "Bearer " + key}
        hdr["X-Nexus-Cache-Key"] = "private-workflow"
        for i in range(2):
            if i:
                history.extend(
                    [
                        {"role": "assistant", "content": "answer"},
                        {"role": "user", "content": "continue"},
                    ]
                )
            reply = await relay.post(
                "/v1/messages" if client_is_messages else "/v1/responses", headers=hdr, json=body
            )
            assert reply.status_code == 200, reply.text
        if client_is_messages:
            assert all("ttl" not in b["prompt_cache_options"] for b in upstream_bodies)
            assert all(len(b["prompt_cache_key"]) == 64 for b in upstream_bodies)
        assert len(upstream_bodies) == 2
        org = plane[2]["id"]
        pending = await http[0].get(
            f"/api/v1/organizations/{org}/cache-usage", headers=auth(http[1])
        )
        assert pending.json()[0]["attempts"] == 2 and pending.json()[0]["metered_attempts"] == 0
        await wait_events(admin, 4)
        assert await http[2].state.usage.process() == 2
        charges = admin.execute(
            "SELECT charged_amount FROM billing.request_runs ORDER BY charged_amount"
        ).fetchall()
        costs = admin.execute(
            "SELECT cost_amount FROM billing.request_attempts ORDER BY cost_amount"
        ).fetchall()
        assert charges == [(Decimal("3.2"),), (Decimal("5.5"),)]
        assert costs == [(Decimal("1.28"),), (Decimal("2.20"),)]
        report = await http[0].get(
            f"/api/v1/organizations/{plane[2]['id']}/cache-usage", headers=auth(http[1])
        )
        assert report.status_code == 200, report.text
        assert report.json() == [
            {
                "deployment_id": plane[4]["id"],
                "model_id": plane[3]["id"],
                "attempts": 2,
                "metered_attempts": 2,
                "cache_hit_attempts": 1,
                "input_tokens": 6,
                "cache_read_tokens": 2,
                "cache_write_tokens": 2,
            }
        ]
