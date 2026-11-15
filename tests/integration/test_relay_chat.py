"""Chat clients use existing native usage, reservation and cost ownership."""

import asyncio
import json

import pytest
from test_billing import account_amounts
from test_relay import relay_binary, tls, wait_events  # noqa: F401
from test_relay_conversion import output, stream_events
from test_relay_streams import stream_plane

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("family", ["anthropic", "openai_compatible"])
async def test_chat(http, admin, tls, relay_binary, family):  # noqa: F811
    messages = family == "anthropic"
    seen = []

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            headers = {
                k.lower(): v.strip() for line in head[1:] if line for k, v in [line.split(":", 1)]
            }
            body = json.loads(await reader.readexactly(int(headers["content-length"])))
            seen.append((head[0], body))
            result = output(messages)
            if body.get("stop_sequences"):
                result.update(
                    content=[{"type": "text", "text": "before marker"}],
                    stop_reason="stop_sequence",
                    stop_sequence="END",
                )
            if body.get("stream"):
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Connection: close\r\n\r\n"
                )
                for frame in stream_events(messages, result):
                    if body.get("stop_sequences") and frame["type"] == "message_delta":
                        frame["delta"] = {"stop_reason": "stop_sequence", "stop_sequence": "END"}
                    writer.write(f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n".encode())
            else:
                data = json.dumps(result).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode()
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
            family,
            convert_from="openai_responses" if messages else None,
        ) as (relay, model, key, dropped, plane),
    ):
        hdr = {"Authorization": f"Bearer {key}"}
        path = "/v1/chat/completions"
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": "policy"},
                {"role": "user", "content": "hello"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "strict": False,
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "max_completion_tokens": 6,
        }
        reply = await relay.post(path, headers=hdr, json=body)
        assert reply.status_code == 200, reply.text
        data = reply.json()
        assert data["object"] == "chat.completion" and data["model"] == model
        choice = data["choices"][0]
        assert choice["finish_reason"] == "tool_calls" and choice["message"]["content"] == "京都"
        assert [c["id"] for c in choice["message"]["tool_calls"]] == ["first", "second"]
        assert data["usage"]["total_tokens"] == 5
        async with relay.stream(
            "POST",
            path,
            headers=hdr,
            json=body | {"stream": True, "stream_options": {"include_usage": True}},
        ) as r:
            assert r.status_code == 200
            frames = [line[6:] async for line in r.aiter_lines() if line.startswith("data: ")]
        assert frames[-1] == "[DONE]"
        chunks = list(map(json.loads, frames[:-1]))
        assert chunks[-1]["choices"] == [] and chunks[-1]["usage"]["total_tokens"] == 5
        assert chunks[-2]["choices"][0]["finish_reason"] == "tool_calls"
        deltas = [c["choices"][0]["delta"] for c in chunks[:-1]]
        assert "".join(d.get("content", "") for d in deltas) == "京都"
        tools = [f for d in deltas for f in d.get("tool_calls", [])]
        assert [f["id"] for f in tools if "id" in f] == ["first", "second"]
        for i, expected in enumerate([{"x": 1}, {"y": 2}]):
            assert (
                json.loads("".join(f["function"]["arguments"] for f in tools if f["index"] == i))
                == expected
            )
        assert len(seen) == 2 and all(
            path == ("POST /v1/messages HTTP/1.1" if messages else "POST /v1/responses HTTP/1.1")
            for path, _ in seen
        )
        await wait_events(admin, 4)
        assert await http[2].state.usage.process() == 2
        assert account_amounts(admin, plane[2]) == (90, 0)

        stop_body = {
            "model": model,
            "messages": [{"role": "user", "content": "go"}],
            "stop": "END",
            "max_tokens": 6,
        }
        if messages:
            for stream in [False, True]:
                reply = await relay.post(path, headers=hdr, json={**stop_body, "stream": stream})
                assert reply.status_code == 200
                if stream:
                    frames = [
                        json.loads(line[5:])
                        for line in reply.text.splitlines()
                        if line.startswith("data:") and "[DONE]" not in line
                    ]
                    assert frames[-1]["choices"][0]["finish_reason"] == "stop"
                else:
                    assert reply.json()["choices"][0]["message"]["content"] == "before marker"
                    assert reply.json()["choices"][0]["finish_reason"] == "stop"
                assert seen[-1][1]["stop_sequences"] == ["END"]
            await wait_events(admin, 8)
            assert await http[2].state.usage.process() == 2
            assert account_amounts(admin, plane[2]) == (80, 0)
        else:
            assert (await relay.post(path, headers=hdr, json=stop_body)).status_code == 400
            assert len(seen) == 2
            assert admin.execute("SELECT count(*) FROM billing.request_runs").fetchone() == (2,)
