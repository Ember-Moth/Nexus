"""Only the Chat endpoint is available at the upstream."""

import asyncio
import json

import httpx
import pytest
from test_billing import account_amounts
from test_relay import relay_binary, tls, wait_events  # noqa: F401
from test_relay_streams import stream_plane

pytestmark = pytest.mark.integration


async def test_responses_over_chat(http, admin, tls, relay_binary):  # noqa: F811
    seen = []
    usage = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    head = {"id": "chatcmpl-abc", "created": 100, "model": "private"}
    functions = [
        {"id": name, "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
        for name in ["call-a", "call-b"]
    ]
    unblock = asyncio.Event()
    truncated = False

    async def provider(reader, writer):
        try:
            headers = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            fields = dict(
                (k.lower(), v.strip()) for k, v in (s.split(":", 1) for s in headers[1:] if s)
            )
            b = json.loads(await reader.readexactly(int(fields["content-length"])))
            seen.append((headers[0], fields["authorization"].startswith("Bearer "), b))
            if headers[0] != "POST /v1/chat/completions HTTP/1.1":
                writer.write(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n")
            elif b.get("stream"):
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Connection: close\r\n\r\n"
                )

                def emit(delta, reason=None):
                    v = head | {
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
                    }
                    writer.write(f"data: {json.dumps(v)}\n\n".encode())

                emit({"role": "assistant", "content": "京"})
                await writer.drain()
                await asyncio.wait_for(unblock.wait(), 5)
                emit({"content": "都"})
                emit(
                    {
                        "tool_calls": [
                            (c | {"index": i, "function": {"name": "lookup", "arguments": "{"}})
                            for i, c in enumerate(functions)
                        ]
                    }
                )
                emit(
                    {"tool_calls": [{"index": i, "function": {"arguments": "}"}} for i in (1, 0)]},
                    "tool_calls",
                )
                if truncated:
                    await writer.drain()
                    return
                frame = head | {
                    "choices": [],
                    "usage": usage | {"prompt_tokens_details": {"cached_tokens": 0}},
                    "object": "chat.completion.chunk",
                }
                writer.write(f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n".encode())
            else:
                msg = {"role": "assistant", "content": "京都", "tool_calls": functions}
                data = json.dumps(
                    head
                    | {
                        "object": "chat.completion",
                        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls"}],
                        "usage": usage,
                    }
                ).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                    + f"Content-Length: {len(data)}\r\n\r\n".encode()
                    + data
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
            "openai_compatible",
            chat_upstream=True,
        ) as (relay, model, key, _, plane),
    ):
        hdr = {"Authorization": f"Bearer {key}"}
        body = {
            "model": model,
            "instructions": "policy",
            "input": "hi",
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            "max_output_tokens": 6,
        }
        response = await relay.post("/v1/responses", headers=hdr, json=body)
        assert response.status_code == 200, response.text
        normal = response.json()
        assert (
            normal["object"] == "response"
            and normal["status"] == "completed"
            and normal["model"] == model
        )
        assert normal["output"][0]["content"][0]["text"] == "京都"
        assert [v["call_id"] for v in normal["output"][1:]] == ["call-a", "call-b"]
        assert normal["usage"]["total_tokens"] == 5
        assert seen[0][1]
        wire = seen[0][2]
        assert wire["model"] == plane[4]["upstream_model"] and wire["max_completion_tokens"] == 6
        assert wire["messages"] == [
            {"role": "system", "content": "policy"},
            {"role": "user", "content": "hi"},
        ]
        assert wire["tools"][0]["function"]["name"] == "lookup"
        history = [
            {"role": "user", "content": "hi"},
            *normal["output"],
            *[
                {"type": "function_call_output", "call_id": c["id"], "output": "ok"}
                for c in functions
            ],
        ]
        events = []
        async with relay.stream(
            "POST", "/v1/responses", headers=hdr, json=body | {"input": history, "stream": True}
        ) as streamed:
            assert streamed.status_code == 200
            async for line in streamed.aiter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    events.append(event)
                    if event["type"] == "response.output_text.delta" and event["delta"] == "京":
                        unblock.set()
        assert seen[1][2]["stream_options"] == {"include_usage": True}
        assert [c["id"] for c in seen[1][2]["messages"][-3]["tool_calls"]] == ["call-a", "call-b"]
        assert [m["tool_call_id"] for m in seen[1][2]["messages"][-2:]] == ["call-a", "call-b"]
        assert [e["sequence_number"] for e in events] == list(range(len(events)))
        assert (
            events[0]["type"] == "response.created" and events[-1]["type"] == "response.completed"
        )
        final = events[-1]["response"]
        assert final["output"][0]["content"] == normal["output"][0]["content"]
        assert [c["arguments"] for c in final["output"][1:]] == ["{}", "{}"]
        assert final["usage"]["total_tokens"] == 5
        for i, c in enumerate(final["output"][1:], 1):
            deltas = [
                e
                for e in events
                if e["type"] == "response.function_call_arguments.delta" and e["output_index"] == i
            ]
            assert "".join(e["delta"] for e in deltas) == c["arguments"] and all(
                e["item_id"] == c["id"] for e in deltas
            )
        declined = await relay.post(
            "/v1/responses",
            headers=hdr,
            json=body | {"context_management": [{"type": "compaction"}]},
        )
        assert declined.status_code == 400 and len(seen) == 2
        await wait_events(admin, 4)
        assert await http[2].state.usage.process() == 2
        assert account_amounts(admin, plane[2]) == (90, 0)
        assert admin.execute(
            "SELECT DISTINCT expected_usage_format FROM billing.request_attempts"
        ).fetchall() == [("openai.chat.v1",)]

        truncated = True
        received = []
        async with relay.stream(
            "POST", "/v1/responses", headers=hdr, json=body | {"stream": True}
        ) as lost:
            assert lost.status_code == 200
            with pytest.raises(httpx.RemoteProtocolError):
                async for line in lost.aiter_lines():
                    if line.startswith("data: "):
                        received.append(json.loads(line[6:])["type"])
        assert "response.completed" not in received and "response.incomplete" not in received
        await wait_events(admin, 6)
        await http[2].state.usage.process()
        assert admin.execute(
            "SELECT state,reservation_state,charged_amount FROM billing.request_runs WHERE id=%s",
            (lost.headers["x-request-id"],),
        ).fetchone() == ("review", "held", None)
