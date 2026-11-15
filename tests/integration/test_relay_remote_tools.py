import asyncio
import json

import pytest
from test_relay import relay_binary, tls, wait_events  # noqa: F401
from test_relay_streams import stream_plane

pytestmark = pytest.mark.integration


async def test_messages_remote_tool_deltas_and_settlement(http, admin, tls, relay_binary):  # noqa: F811
    seen = []
    toolset = {"type": "mcp_toolset", "mcp_server_name": "docs"}
    fetch = {"type": "web_fetch_20250910", "name": "web_fetch"}
    server_def = {"type": "url", "name": "docs", "url": "https://example.org/mcp"}

    async def provider(reader, writer):
        try:
            lines = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            hdr = {
                k.lower(): v.strip() for line in lines[1:] if line for k, v in [line.split(":", 1)]
            }
            request = json.loads(await reader.readexactly(int(hdr["content-length"])))
            seen.append(request)
            assert request["tools"] == [toolset, fetch] and request["mcp_servers"] == [server_def]
            assert hdr["anthropic-beta"] == "mcp-client-2025-11-20"
            usage = {
                "input_tokens": 2,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "server_tool_use": {"web_fetch_requests": 0},
            }
            events = [
                {
                    "type": "message_start",
                    "message": {"id": "msg_tools", "type": "message", "usage": usage},
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "server_tool_use",
                        "name": "web_fetch",
                        "id": "fetch1",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": "{}"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "web_fetch_tool_result",
                        "tool_use_id": "fetch1",
                        "content": {
                            "type": "web_fetch_tool_result_error",
                            "error_code": "url_not_accessible",
                        },
                    },
                },
                {"type": "content_block_stop", "index": 1},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 1, "server_tool_use": {"web_fetch_requests": 1}},
                },
                {"type": "message_stop"},
            ]
            wire = "".join(
                f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                + f"Content-Length: {len(wire)}\r\n\r\n".encode()
                + wire
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
            "anthropic",
            features=["remote_mcp", "web_fetch"],
        ) as (relay, model, key, _, _),
    ):
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "read"}],
            "max_tokens": 4,
            "stream": True,
            "tools": [toolset, fetch],
            "mcp_servers": [server_def],
        }
        reply = await relay.post(
            "/v1/messages",
            headers={"x-api-key": key, "anthropic-beta": "mcp-client-2025-11-20"},
            json=body,
        )
        assert reply.status_code == 200 and "event: message_stop" in reply.text
        assert "web_fetch_tool_result" in reply.text and len(seen) == 1
        await wait_events(admin, 2)
        assert await http[2].state.usage.process() == 1
        assert admin.execute(
            "SELECT charged_amount,reservation_state FROM billing.request_runs"
        ).fetchone() == (3, "captured")


@pytest.mark.parametrize("family", ["anthropic", "openai_compatible"])
async def test_disabled_provider_features_are_refused_before_reservation(
    http,
    admin,
    tls,  # noqa: F811
    relay_binary,  # noqa: F811
    family,
):
    contacted = []

    async def provider(reader, writer):
        contacted.append(True)
        writer.close()

    server = await asyncio.start_server(provider, "127.0.0.1", 0, ssl=tls[0])
    async with (
        server,
        stream_plane(
            http, admin, relay_binary, tls[1], server.sockets[0].getsockname()[1], family
        ) as (relay, model, key, _, _),
    ):
        if family == "anthropic":
            path, hdr = "/v1/messages", {"x-api-key": key}
            base = {"model": model, "max_tokens": 4, "messages": [{"role": "user", "content": "x"}]}
            refused = [
                {"mcp_servers": [{"type": "url", "name": "d", "url": "https://example.org/m"}]},
                {"tools": [{"type": "web_fetch_20250910", "name": "web_fetch"}]},
            ]
            resources = [
                ("/v1/messages/batches", {"requests": [{"custom_id": "a", "params": base}]})
            ]
        else:
            path, hdr = "/v1/responses", {"authorization": f"Bearer {key}"}
            base = {"model": model, "input": "x"}
            refused = [
                {"store": True},
                {"store": True, "background": True},
                {
                    "tools": [
                        {
                            "type": "mcp",
                            "server_label": "d",
                            "server_url": "https://example.org/m",
                            "require_approval": "never",
                        }
                    ]
                },
            ]
            resources = [("/v1/conversations", {}), ("/v1/vector_stores", {"name": "v"})]
        for extra in refused:
            r = await relay.post(path, headers=hdr, json={**base, **extra})
            assert r.status_code == 400, r.text
            code = r.json()["error"]["type" if family == "anthropic" else "code"]
            assert code == "feature_disabled"
        for resource, body in resources:
            r = await relay.post(resource, headers=hdr, json=body)
            assert r.status_code == 400, (resource, r.text)
        upload = await relay.post(
            "/v1/files", headers=hdr, files={"file": ("a.txt", b"a")}, data={"purpose": "user_data"}
        )
        assert upload.status_code == 400 and "feature_disabled" in upload.text, upload.text
        assert not contacted
        assert admin.execute("SELECT count(*) FROM billing.request_runs").fetchone() == (0,)
