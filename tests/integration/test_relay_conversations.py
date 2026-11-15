import asyncio
import json

import pytest
from test_billing import headers
from test_project_credentials import key as issue_key
from test_relay import relay_binary, tls, wait_events  # noqa: F401
from test_relay_streams import stream_plane

pytestmark = pytest.mark.integration


async def test_owned_conversation_items_generation_and_deletion(http, admin, tls, relay_binary):  # noqa: F811
    pid = "conv_provider"
    base = "/v1/conversations"
    content = []
    metadata = {}
    calls = []
    stored = None

    async def provider(reader, writer):
        nonlocal metadata, stored
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = head[0].split()
            hdr = {k.lower(): v.strip() for row in head[1:] if row for k, v in [row.split(":", 1)]}
            body = await reader.readexactly(int(hdr.get("content-length", "0")))
            params = json.loads(body) if body else {}
            calls.append((method, path))
            mime = "application/json"
            if path == base:
                content.extend(params["items"])
                metadata = params["metadata"]
                result = {"object": "conversation", "id": pid, "metadata": metadata}
            elif path == base + "/" + pid:
                if method == "DELETE":
                    result = {"object": "conversation.deleted", "id": pid, "deleted": True}
                else:
                    metadata = params.get("metadata", metadata)
                    result = {"object": "conversation", "id": pid, "metadata": metadata}
            elif path.startswith(base + "/" + pid + "/items"):
                if method == "POST":
                    content.extend(params["items"])
                if path.endswith("msg1"):
                    if method == "DELETE":
                        content.pop()
                        result = {"object": "conversation", "id": pid}
                    else:
                        result = {
                            "id": "msg1",
                            "type": "message",
                            "role": "user",
                            "content": content[-1]["content"],
                        }
                else:
                    result = {
                        "object": "list",
                        "data": content,
                        "first_id": "msg0",
                        "last_id": "msg1",
                        "has_more": False,
                    }
            elif path == "/v1/responses" and method == "POST":
                assert params["conversation"] in (pid, {"id": pid})
                result = {
                    "id": "resp_conversation",
                    "object": "response",
                    "status": "completed",
                    "conversation": {"id": pid},
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }
                    ],
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 0},
                    },
                }
                if params["store"]:
                    stored = result
                if params.get("stream"):
                    mime = "text/event-stream"
                    events = [
                        {
                            "type": "response.created",
                            "response": {"id": "stream_conv", "conversation": {"id": pid}},
                        },
                        {"type": "response.completed", "response": result | {"id": "stream_conv"}},
                    ]
                    wire = "".join(
                        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
                    ).encode()
            else:
                assert path == "/v1/responses/resp_conversation" and method == "GET"
                result = stored
            if mime == "application/json":
                wire = json.dumps(result).encode()
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\n"
                    f"Content-Length: {len(wire)}\r\nConnection: close\r\n\r\n"
                ).encode()
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
            "openai_compatible",
            features=["conversations", "stored_responses", "files"],
        ) as (relay, model, key, _, plane),
    ):
        h = {"Authorization": "Bearer " + key}
        first = {"role": "user", "content": "hello"}
        source = {"metadata": {"topic": "one"}, "items": [first]}
        created = await relay.post(base, headers=h, json=source)
        assert created.status_code == 200, created.text
        handle = created.json()["id"]
        assert handle.startswith("conv_nx_")
        path = f"{base}/{handle}"
        assert (await relay.get(path, headers=h)).json() == created.json()
        assert (await relay.post(path, headers=h, json={"metadata": {"topic": "new"}})).json()[
            "metadata"
        ] == {"topic": "new"}
        invalid = await relay.post(
            path + "/items", headers=h, json={"items": [{"type": "item_reference", "id": "any"}]}
        )
        assert invalid.status_code == 400
        later = {"role": "user", "content": "next"}
        assert (await relay.post(path + "/items", headers=h, json={"items": [later]})).json()[
            "data"
        ] == [first, later]
        assert (await relay.get(path + "/items?limit=2", headers=h)).json()["last_id"] == "msg1"
        assert (await relay.get(path + "/items/msg1", headers=h)).json()["content"] == "next"
        assert (await relay.delete(path + "/items/msg1", headers=h)).json()["id"] == handle
        assert admin.execute("SELECT count(*) FROM billing.request_runs").fetchone() == (0,)
        generation = {"model": model, "input": "hi", "conversation": handle}
        resp = await relay.post("/v1/responses", headers=h, json=generation | {"store": True})
        assert resp.status_code == 200, resp.text
        assert resp.json()["conversation"] == {"id": handle}
        get = await relay.get("/v1/responses/" + resp.json()["id"], headers=h)
        assert get.json() == resp.json()
        stream = await relay.post(
            "/v1/responses",
            headers=h,
            json=generation | {"stream": True, "conversation": {"id": handle}},
        )
        events = [
            json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")
        ]
        assert events[-1]["type"] == "response.completed"
        assert all(e["response"]["conversation"] == {"id": handle} for e in events)
        await wait_events(admin, 4)
        assert await http[2].state.usage.process() == 2
        foreign = (
            await issue_key(
                http[0],
                http[1],
                plane[2],
                str(plane[5].service_account_id),
                name="conversation-other",
            )
        ).json()["secret"]
        release = await http[0].post(
            f"/api/v1/gateway-clusters/{plane[7]}/releases",
            headers=headers(http[1], "conversation-other"),
            json={},
        )
        assert release.status_code == 202
        other = {"Authorization": "Bearer " + foreign}
        async with asyncio.timeout(10):
            while (await relay.get("/v1/models", headers=other)).status_code != 200:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
        n = len(calls)
        assert (await relay.get(path + "/items", headers=other)).status_code == 404
        assert (
            await relay.post("/v1/responses", headers=other, json=generation)
        ).status_code == 404
        assert len(calls) == n
        assert (await relay.delete(path, headers=h)).json()["id"] == handle
        assert (await relay.post("/v1/responses", headers=h, json=generation)).status_code == 404


async def test_conversation_handle_on_websocket(http, admin, tls, relay_binary):  # noqa: F811
    from uuid import uuid4

    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve

    pid = "conv_at_ws_provider"
    observed = []

    async def provider(socket):
        async for wire in socket:
            req = json.loads(wire)
            observed.append(req.get("conversation"))
            result = {
                "id": "resp_ws",
                "object": "response",
                "conversation": {"id": pid},
                "output": [],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "input_tokens_details": {"cached_tokens": 0},
                },
            }
            for typ, status in [
                ("response.created", "in_progress"),
                ("response.completed", "completed"),
            ]:
                await socket.send(
                    json.dumps({"type": typ, "response": result | {"status": status}})
                )

    async with (
        serve(provider, "127.0.0.1", 0, ssl=tls[0]) as upstream,
        stream_plane(
            http,
            admin,
            relay_binary,
            tls[1],
            upstream.sockets[0].getsockname()[1],
            "openai_compatible",
            ws=True,
            features=["conversations", "stored_responses", "files"],
        ) as (relay, model, token, _, plane),
    ):
        peer, _, _, m, dep, admission, _, cluster = plane
        revision = admin.execute(
            "SELECT desired_revision FROM publication.gateway_clusters WHERE id=%s", (cluster,)
        ).fetchone()[0]
        handle = uuid4()
        await peer.connection.execute(
            "SELECT gateway_v1.conversation_save(%s,%s,%s,%s,%s,%s)",
            (handle, admission.api_key_id, revision, m["id"], dep["id"], pid),
        )
        url = f"ws://127.0.0.1:{relay.base_url.port}/v1/responses"
        key = {"Authorization": "Bearer " + token}
        public_id = f"conv_nx_{handle}"
        async with connect(url, additional_headers=key, proxy=None) as socket:
            await socket.send(
                json.dumps(
                    {
                        "type": "response.create",
                        "model": model,
                        "input": "ok",
                        "conversation": public_id,
                    }
                )
            )
            for typ in ["response.created", "response.completed"]:
                event = json.loads(await asyncio.wait_for(socket.recv(), 4))
                assert event["type"] == typ and event["response"]["conversation"]["id"] == public_id
        assert observed == [pid]
        await wait_events(admin, 2)
        assert await http[2].state.usage.process() == 1
