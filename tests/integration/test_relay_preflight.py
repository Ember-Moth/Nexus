import asyncio
import json

import pytest
from test_relay import relay_binary, tls, wait_events  # noqa: F401
from test_relay_streams import stream_plane

pytestmark = pytest.mark.integration


async def test_model_discovery_and_native_count(http, admin, tls, relay_binary):  # noqa: F811
    received = []

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            headers = {
                k.lower(): v.strip() for line in head[1:] if line for k, v in [line.split(":", 1)]
            }
            body = json.loads(await reader.readexactly(int(headers["content-length"])))
            received.append((head[0], headers, body))
            payload = (
                {"input_tokens": 17}
                if "count_tokens" in head[0]
                else {
                    "id": "msg-version",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                }
            )
            data = json.dumps(payload).encode()
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
            http, admin, relay_binary, tls[1], server.sockets[0].getsockname()[1], "anthropic"
        ) as (client, name, token, _, plane),
    ):
        _, _, _, _, dep, admission, *_ = plane
        listing = await client.get("/v1/models", headers={"Authorization": "Bearer " + token})
        assert listing.status_code == 200, listing.text
        assert listing.json()["object"] == "list" and [m["id"] for m in listing.json()["data"]] == [
            name
        ]
        assert listing.json()["data"][0]["created"] > 0
        assert (await client.get("/v1/models")).status_code == 401
        body = {"model": name, "messages": [{"role": "user", "content": "count this"}]}
        headers = {
            "x-api-key": token,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "first-flag,second-flag",
            "authorization": "unrelated-private-header",
        }
        count = await client.post("/v1/messages/count_tokens", json=body, headers=headers)
        assert count.status_code == 200 and count.json() == {"input_tokens": 17}, count.text
        assert received[0][0] == "POST /v1/messages/count_tokens HTTP/1.1"
        assert received[0][2] == body | {"model": dep["upstream_model"]}
        assert received[0][1]["anthropic-beta"] == "first-flag,second-flag"
        assert "authorization" not in received[0][1] and received[0][1]["x-api-key"] != token
        assert admin.execute("SELECT count(*) FROM billing.request_runs").fetchone() == (0,)
        sent = await client.post("/v1/messages", json=body | {"max_tokens": 3}, headers=headers)
        assert sent.status_code == 200, sent.text
        assert received[-1][1]["anthropic-beta"] == "first-flag,second-flag"
        await wait_events(admin, 2)
        admin.execute(
            "UPDATE identity.api_keys SET revoked_at=now() WHERE id=%s", (admission.api_key_id,)
        )
        for verb, path in [("GET", "/v1/models"), ("POST", "/v1/messages/count_tokens")]:
            revoked = await client.request(
                verb,
                path,
                headers={"Authorization": "Bearer " + token, "x-api-key": token},
                json=body if verb == "POST" else None,
            )
            assert revoked.status_code == 401, revoked.text
        assert len(received) == 2
