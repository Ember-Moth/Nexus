import asyncio
import json
from decimal import Decimal

import pytest
from test_billing import headers
from test_project_credentials import key as issue_key
from test_relay import relay_binary, tls, wait_events  # noqa: F401
from test_relay_streams import stream_plane, unsealed

pytestmark = pytest.mark.integration


async def test_stored_response_key_ownership(http, admin, tls, relay_binary):  # noqa: F811
    stored = {}
    sent = []

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            verb, path, _ = head[0].split()
            hdrs = {k.lower(): v.strip() for row in head[1:] if row for k, v in [row.split(":", 1)]}
            raw = await reader.readexactly(int(hdrs.get("content-length", "0")))
            sent.append((verb, path, json.loads(raw) if raw else {}))
            if verb == "POST":
                item = {
                    "object": "response",
                    "id": "resp_upstream_" + str(len(sent)),
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "msg_in_parent",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "private-body"}],
                        }
                    ],
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 0},
                    },
                }
                if sent[-1][2].get("tools"):
                    assert sent[-1][2]["tools"][0]["type"] == "mcp"
                    item["output"].append(
                        {
                            "id": "approval_in_parent",
                            "type": "mcp_approval_request",
                            "server_label": "calendar",
                            "name": "read",
                            "arguments": "{}",
                        }
                    )
                if sent[-1][2]["store"]:
                    stored[item["id"]] = item
            elif verb == "GET":
                item = stored[path.rsplit("/", 1)[1]]
            else:
                rid = path.rsplit("/", 1)[1]
                del stored[rid]
                item = {"id": rid, "object": "response.deleted", "deleted": True}
            p = json.dumps(item).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(p)}\r\nConnection: close\r\n\r\n".encode()
                + p
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
            features=["stored_responses", "background", "remote_mcp"],
        ) as (client, model, key, _, plane),
    ):
        token = {"Authorization": "Bearer " + key}
        body = {"model": model, "input": "first"}
        created = await client.post(
            "/v1/responses",
            headers=token,
            json=body
            | {
                "store": True,
                "tools": [
                    {
                        "type": "mcp",
                        "server_label": "calendar",
                        "server_url": "https://example.org/mcp",
                        "require_approval": "always",
                    }
                ],
            },
        )
        assert created.status_code == 200, created.text
        handle = created.json()["id"]
        assert handle.startswith("resp_nx_") and handle != "resp_upstream_1"
        path = "/v1/responses/" + handle
        get = await client.get(path, headers=token)
        assert get.status_code == 200 and get.json() == created.json()
        # Item references carry the IDs this key received, bound to it by the node.
        sealed = {unsealed(o)["id"]: o["id"] for o in created.json()["output"]}
        assert all(v.startswith("nxo_") for v in sealed.values())
        for invalid_item, status in [
            ({"id": "absent"}, 400),
            ({"type": "item_reference", "id": "msg_in_parent"}, 400),
            (
                {
                    "type": "mcp_approval_response",
                    "approval_request_id": sealed["msg_in_parent"],
                    "approve": True,
                },
                404,
            ),
        ]:
            denied = await client.post(
                "/v1/responses",
                headers=token,
                json=body | {"previous_response_id": handle, "input": [invalid_item]},
            )
            assert denied.status_code == status, denied.text
        assert admin.execute("SELECT count(*) FROM billing.request_runs").fetchone() == (1,)
        continuation = await client.post(
            "/v1/responses",
            headers=token,
            json=body
            | {
                "previous_response_id": handle,
                "input": [
                    {"type": "item_reference", "id": sealed["msg_in_parent"]},
                    {
                        "type": "mcp_approval_response",
                        "approval_request_id": sealed["approval_in_parent"],
                        "approve": True,
                    },
                ],
            },
        )
        assert continuation.status_code == 200, continuation.text
        assert sent[-1][2]["previous_response_id"] == "resp_upstream_1"
        await wait_events(admin, 4)
        assert await http[2].state.usage.process() == 2
        # Publish the second key so this tests a live authorized key, not a stale-snapshot denial.
        foreign = (
            await issue_key(
                http[0], http[1], plane[2], str(plane[5].service_account_id), name="other-owner"
            )
        ).json()["secret"]
        release = await http[0].post(
            f"/api/v1/gateway-clusters/{plane[7]}/releases",
            headers=headers(http[1], "owned-revision"),
            json={},
        )
        assert release.status_code == 202
        async with asyncio.timeout(8):
            while (  # noqa: ASYNC110 - probe installed relay revision
                await client.get("/v1/models", headers={"Authorization": "Bearer " + foreign})
            ).status_code != 200:
                await asyncio.sleep(0.05)
        size = len(sent)
        for method in ["GET", "DELETE"]:
            denied = await client.request(
                method, path, headers={"Authorization": "Bearer " + foreign}
            )
            assert denied.status_code == 404
        denied = await client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer " + foreign},
            json=body | {"previous_response_id": handle},
        )
        assert denied.status_code == 404 and len(sent) == size
        deleted = await client.delete(path, headers=token)
        assert deleted.status_code == 200 and deleted.json()["id"] == handle, deleted.text
        assert (await client.get(path, headers=token)).status_code == 404
        assert admin.execute("SELECT count(*) FROM billing.request_runs").fetchone() == (2,)


async def test_stored_websocket_continues_on_new_connection(http, admin, tls, relay_binary):  # noqa: F811
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve

    seen = []

    async def provider(ws):
        async for raw in ws:
            req = json.loads(raw)
            seen.append(req)
            rid = "private_r" + str(len(seen))
            await ws.send(
                json.dumps(
                    {"type": "response.created", "response": {"id": rid, "status": "in_progress"}}
                )
            )
            await ws.send(
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": rid,
                            "object": "response",
                            "status": "completed",
                            "output": [],
                            "usage": {
                                "input_tokens": 1,
                                "output_tokens": 1,
                                "input_tokens_details": {"cached_tokens": 0},
                            },
                        },
                    }
                )
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
            features=["stored_responses", "background", "remote_mcp"],
        ) as (client, model, key, _, plane),
    ):
        url = str(client.base_url).replace("http://", "ws://").rstrip("/") + "/v1/responses"
        parent = None
        for _ in range(2):
            async with connect(
                url, additional_headers={"Authorization": "Bearer " + key}, proxy=None
            ) as ws:
                create = {"type": "response.create", "model": model, "input": "hi", "store": True}
                if parent:
                    create["previous_response_id"] = parent
                await ws.send(json.dumps(create))
                created = json.loads(await asyncio.wait_for(ws.recv(), 4))
                assert created["type"] == "response.created", created
                parent = created["response"]["id"]
                assert parent.startswith("resp_nx_")
                ended = json.loads(await asyncio.wait_for(ws.recv(), 4))
                assert ended["type"] == "response.completed" and ended["response"]["id"] == parent
        assert seen[1]["previous_response_id"] == "private_r1" and seen[0]["store"]
        await wait_events(admin, 4)
        assert await http[2].state.usage.process() == 2


@pytest.mark.parametrize("cancel", [False, True])
async def test_background_recovery_and_cancel(http, admin, tls, relay_binary, cancel):  # noqa: F811
    from test_relay import running_relay

    finished = asyncio.Event()
    posts = 0

    async def provider(reader, writer):
        nonlocal posts
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            verb, path, _ = head[0].split()
            hdr = {
                k.lower(): v.strip() for line in head[1:] if line for k, v in [line.split(":", 1)]
            }
            body = await reader.readexactly(int(hdr.get("content-length", 0)))
            if path.endswith("/cancel"):
                assert verb == "POST"
                finished.set()
            elif verb == "POST":
                assert json.loads(body)["background"] is True
                posts += 1
            result = {"id": "resp_pending", "object": "response", "status": "queued", "output": []}
            if finished.is_set():
                result.update(
                    status="cancelled" if cancel else "completed",
                    usage={
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 0},
                    },
                )
            payload = json.dumps(result).encode()
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\n"
                    + "Content-Type: application/json\r\nConnection: close\r\n\r\n"
                ).encode()
                + payload
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
            features=["stored_responses", "background", "remote_mcp"],
        ) as (client, model, key, _, plane),
    ):
        h = {"Authorization": "Bearer " + key}
        submit = await client.post(
            "/v1/responses", json={"model": model, "input": "hi", "background": True}, headers=h
        )
        assert submit.status_code == 200, submit.text
        handle = submit.json()["id"]
        assert handle.startswith("resp_nx_")
        assert admin.execute("SELECT count(*) FROM billing.usage_events").fetchone() == (0,)
        path = "/v1/responses/" + handle
        assert (await client.delete(path, headers=h)).status_code == 409
        first = client.relay_process
        first.terminate()
        await asyncio.wait_for(first.wait(), 15)
        # A new process with the same gateway identity resumes durable work without re-submission.
        peer, private, _, _, _, _, _, cluster = plane
        async with running_relay(
            relay_binary, peer, private, cluster, http[3], tls[1], peer.connection.info.port
        ) as replacement:
            if cancel:
                result = await replacement.post(path + "/cancel", headers=h)
                assert result.status_code == 200 and result.json()["id"] == handle
            else:
                finished.set()
            async with asyncio.timeout(16):
                while admin.execute("SELECT count(*) FROM billing.usage_events").fetchone() != (2,):  # noqa: ASYNC110
                    await asyncio.sleep(0.05)
            assert posts == 1
            assert await http[2].state.usage.process() == 1
            assert admin.execute("SELECT cost_amount FROM billing.request_attempts").fetchone() == (
                Decimal("1.2"),
            )
            assert admin.execute(
                "SELECT charged_amount,reservation_state FROM billing.request_runs"
            ).fetchone() == (3, "captured")
            assert admin.execute("SELECT done FROM publication.response_jobs").fetchone() == (True,)


@pytest.mark.parametrize(
    "disconnect", [True, False], ids=["resume_after_disconnect", "complete_initial_stream"]
)
async def test_background_stream_single_settlement(http, admin, tls, relay_binary, disconnect):  # noqa: F811
    pid = "resp_background_stream"
    released = asyncio.Event()
    submitted = []
    cursors = []

    def response(completed):
        return {
            "object": "response",
            "id": pid,
            "status": "completed" if completed else "in_progress",
            "output": [],
            "usage": {
                "input_tokens": 2,
                "output_tokens": 1,
                "input_tokens_details": {"cached_tokens": 0},
            }
            if completed
            else None,
        }

    def frame(t, seq, **fields):
        data = json.dumps({"type": t, "sequence_number": seq, **fields})
        return f"event: {t}\ndata: {data}\n\n".encode()

    terminal = frame("response.completed", 2, response=response(True))
    delta = frame(
        "response.output_text.delta",
        1,
        response_id=pid,
        delta="OK",
        output_index=0,
        content_index=0,
    )

    async def provider(reader, writer):
        try:
            lines = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = lines[0].split()
            hdr = {
                k.lower(): v.strip() for line in lines[1:] if line for k, v in [line.split(":", 1)]
            }
            if method == "POST":
                assert path == "/v1/responses"
                req = json.loads(await reader.readexactly(int(hdr["content-length"])))
                submitted.append(req)
                assert req["background"] is True and req["stream"] is True and req["store"] is True
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Connection: close\r\n\r\n"
                    + frame("response.created", 0, response=response(False))
                )
                await writer.drain()
                await released.wait()
                writer.write(delta + terminal)
            elif "?" in path:
                from urllib.parse import parse_qs, urlsplit

                url = urlsplit(path)
                assert url.path == "/v1/responses/" + pid
                query = parse_qs(url.query)
                assert query["stream"] == ["true"]
                cursor = int(query["starting_after"][0])
                cursors.append(cursor)
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Connection: close\r\n\r\n"
                )
                if cursor < 1:
                    writer.write(delta)
                if cursor < 2:
                    writer.write(terminal)
            else:
                assert path == "/v1/responses/" + pid
                payload = json.dumps(response(released.is_set())).encode()
                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\n".encode()
                    + b"Connection: close\r\n\r\n"
                    + payload
                )
            await writer.drain()
        except BrokenPipeError, ConnectionResetError:
            pass
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
            features=["stored_responses", "background", "remote_mcp"],
        ) as (relay, model, key, _, _),
    ):
        auth = {"Authorization": "Bearer " + key}
        async with relay.stream(
            "POST",
            "/v1/responses",
            headers=auth,
            json={"model": model, "background": True, "stream": True, "input": "ok"},
        ) as r:
            assert r.status_code == 200
            lines = r.aiter_lines()
            assert await anext(lines) == "event: response.created"
            first = json.loads((await anext(lines))[6:])
            assert first["type"] == "response.created"
            handle = first["response"]["id"]
            assert handle.startswith("resp_nx_")
            assert admin.execute(
                "SELECT count(*) FROM publication.response_jobs WHERE NOT done"
            ).fetchone() == (1,)
            assert admin.execute("SELECT count(*) FROM billing.usage_events").fetchone() == (0,)
            if not disconnect:
                released.set()
                rest = [json.loads(line[6:]) async for line in lines if line.startswith("data: ")]
                assert rest[-1]["response"]["id"] == handle
        released.set()
        for cursor in [0, 1, 2]:
            read = await relay.get(
                "/v1/responses/" + handle,
                headers=auth,
                params={"stream": "true", "starting_after": cursor},
            )
            assert read.status_code == 200
            events = [
                json.loads(line[6:]) for line in read.text.splitlines() if line.startswith("data: ")
            ]
            assert [e["sequence_number"] for e in events] == list(range(cursor + 1, 3))
            assert all(
                e.get("response_id", handle) == handle
                and e.get("response", {"id": handle})["id"] == handle
                for e in events
            )
        assert len(submitted) == 1 and cursors == [0, 1, 2]
        async with asyncio.timeout(12):
            while admin.execute("SELECT count(*) FROM billing.usage_events").fetchone() != (2,):  # noqa: ASYNC110
                await asyncio.sleep(0.05)
        assert await http[2].state.usage.process() == 1
        assert admin.execute(
            "SELECT charged_amount,reservation_state FROM billing.request_runs"
        ).fetchall() == [(3, "captured")]
        assert admin.execute("SELECT cost_amount FROM billing.request_attempts").fetchall() == [
            (Decimal("1.2"),)
        ]
