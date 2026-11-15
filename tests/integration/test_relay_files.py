import asyncio
import json

import pytest
from test_relay import relay_binary, tls, wait_events  # noqa: F401
from test_relay_streams import stream_plane

pytestmark = pytest.mark.integration


async def test_files_upload_use_list_download_and_delete(http, admin, tls, relay_binary):  # noqa: F811
    file_id = "file_provider_one"
    metadata = {
        "id": file_id,
        "filename": "small.txt",
        "mime_type": "text/plain",
        "size_bytes": 5,
        "downloadable": True,
        "created_at": "2026-11-14T00:00:00Z",
        "type": "file",
    }
    sent = []

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = head[0].split()
            hdr = {k.lower(): v.strip() for row in head[1:] if row for k, v in [row.split(":", 1)]}
            data = await reader.readexactly(int(hdr.get("content-length", "0")))
            sent.append((path, method))
            mime = "application/json"
            if path == "/v1/files":
                assert method == "POST" and b"hello" in data and b"small.txt" in data
                assert "files-api-" in hdr["anthropic-beta"]
                resp = json.dumps(metadata).encode()
            elif path == f"/v1/files/{file_id}/content":
                resp = b"hello"
                mime = "text/plain"
            elif path == f"/v1/files/{file_id}":
                resp = json.dumps(
                    metadata if method == "GET" else {"id": file_id, "type": "file_deleted"}
                ).encode()
            else:
                assert path == "/v1/messages"
                inp = json.loads(data)
                part = inp["messages"][-1]["content"][0]
                if part["type"] == "tool_result":
                    part = part["content"][0]
                assert part["source"]["file_id"] == file_id
                resp = json.dumps(
                    {
                        "id": "r",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}],
                        "stop_reason": "end_turn",
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                    }
                ).encode()
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\n"
                    + f"Content-Length: {len(resp)}\r\nConnection: close\r\n\r\n"
                ).encode()
                + resp
            )
            await writer.drain()
        finally:
            writer.close()

    srv = await asyncio.start_server(provider, "127.0.0.1", 0, ssl=tls[0])
    async with (
        srv,
        stream_plane(
            http, admin, relay_binary, tls[1], srv.sockets[0].getsockname()[1], "anthropic"
        ) as (relay, model, key, _, _),
    ):
        h = {"x-api-key": key, "anthropic-beta": "files-api-2025-04-14"}
        made = await relay.post(
            "/v1/files", files={"file": ("small.txt", b"hello", "text/plain")}, headers=h
        )
        assert made.status_code == 200, made.text
        handle = made.json()["id"]
        assert handle.startswith("file_nx_")
        assert (await relay.get("/v1/files", headers=h)).json()["data"] == [
            metadata | {"id": handle}
        ]
        path = "/v1/files/" + handle
        assert (await relay.get(path, headers=h)).json() == made.json()
        assert (await relay.get(path + "/content", headers=h)).content == b"hello"
        assert admin.execute("SELECT count(*) FROM billing.request_runs").fetchone() == (0,)
        body = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "document", "source": {"type": "file", "file_id": handle}}
                    ],
                }
            ],
            "max_tokens": 4,
        }
        res = await relay.post("/v1/messages", headers=h, json=body)
        assert res.status_code == 200, res.text
        nested = body | {
            "messages": [
                {"role": "user", "content": "read the document"},
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "use1", "name": "lookup", "input": {}}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "use1",
                            "content": body["messages"][0]["content"],
                        }
                    ],
                },
            ]
        }
        second = await relay.post("/v1/messages", headers=h, json=nested)
        assert second.status_code == 200, second.text
        await wait_events(admin, 4)
        assert await http[2].state.usage.process() == 2
        assert (await relay.delete(path, headers=h)).json() == {
            "id": handle,
            "type": "file_deleted",
        }
        n = len(sent)
        assert (await relay.get(path, headers=h)).status_code == 404
        assert (await relay.post("/v1/messages", headers=h, json=body)).status_code == 404
        assert (await relay.get("/v1/files", headers=h)).json()["data"] == []
        assert n == len(sent)


async def test_openai_file_scope_pagination_and_responses(http, admin, tls, relay_binary):  # noqa: F811
    from test_billing import headers
    from test_project_credentials import key as issue_key

    api, token, _, _ = http
    uploads = {}
    history = []
    calls = []

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = head[0].split()
            hdr = {k.lower(): v.strip() for row in head[1:] if row for k, v in [row.split(":", 1)]}
            wire = await reader.readexactly(int(hdr.get("content-length", 0)))
            assert "authorization" in hdr and "x-api-key" not in hdr
            calls.append((method, path))
            mime = "application/json"
            if path == "/v1/files":
                assert method == "POST" and b"data1" in wire and b"user_data" in wire
                pid = "file-p" + str(len(uploads))
                obj = {
                    "object": "file",
                    "id": pid,
                    "bytes": 5,
                    "purpose": "user_data",
                    "created_at": 1700000000 + len(uploads),
                    "filename": "notes.txt",
                }
                uploads[pid] = obj
            elif path.startswith("/v1/files/"):
                pid = path.split("/")[3]
                if path.endswith("/content"):
                    mime = "text/plain"
                    payload = b"data1"
                elif method == "DELETE":
                    del uploads[pid]
                    obj = {"object": "file", "id": pid, "deleted": True}
                else:
                    obj = uploads[pid]
            elif path == "/v1/responses":
                req = json.loads(wire)
                assert req["input"][0]["content"][0]["file_id"] == "file-p0"
                obj = {
                    "object": "response",
                    "id": "resp_from_file",
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 0},
                    },
                }
                history[:] = req["input"]
            elif path == "/v1/conversations":
                assert json.loads(wire)["items"][0]["content"][0]["file_id"] == "file-p0"
                obj = {"id": "conv_p", "object": "conversation"}
            else:
                assert path.startswith("/v1/conversations/conv_p/items") or path.startswith(
                    "/v1/responses/resp_from_file/input_items"
                )
                obj = {"object": "list", "data": history, "has_more": False}
            if mime == "application/json":
                payload = json.dumps(obj).encode()
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\n"
                    f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
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
        ) as (relay, model, key, _, plane),
    ):
        h = {"Authorization": "Bearer " + key}
        files = []
        for _ in range(2):
            r = await relay.post(
                "/v1/files",
                headers=h,
                data={"purpose": "user_data"},
                files={"file": ("notes.txt", b"data1", "text/plain")},
            )
            assert r.status_code == 200, r.text
            files.append(r.json()["id"])
        path = "/v1/files/" + files[0]
        assert (await relay.get(path + "/content", headers=h)).content == b"data1"
        assert (await relay.get(path, headers=h)).json()["id"] == files[0]
        page = (await relay.get("/v1/files", headers=h, params={"order": "asc", "limit": 1})).json()
        assert page["object"] == "list" and page["has_more"] and page["last_id"] == files[0]
        page = (
            await relay.get(
                "/v1/files",
                headers=h,
                params={"order": "asc", "after": files[0], "purpose": "user_data"},
            )
        ).json()
        assert [f["id"] for f in page["data"]] == [files[1]] and not page["has_more"]
        assert (await relay.get("/v1/files", headers={"x-api-key": key})).json()["data"] == []
        assert (await relay.get(path, headers={"x-api-key": key})).status_code == 404
        assert admin.execute("SELECT count(*) FROM billing.request_runs").fetchone() == (0,)
        items = [{"role": "user", "content": [{"type": "input_file", "file_id": files[0]}]}]
        req = {"model": model, "input": items, "store": True}
        response = await relay.post("/v1/responses", headers=h, json=req)
        assert response.status_code == 200, response.text
        data = await relay.get(
            "/v1/responses/" + response.json()["id"] + "/input_items?limit=1", headers=h
        )
        assert data.json()["data"] == items, data.text
        conv = await relay.post("/v1/conversations", headers=h, json={"items": items})
        assert conv.status_code == 200, conv.text
        data = await relay.get("/v1/conversations/" + conv.json()["id"] + "/items", headers=h)
        assert data.json()["data"] == items, data.text
        chat = await relay.post(
            "/v1/chat/completions",
            headers=h,
            json={
                "model": model,
                "messages": [
                    {"role": "user", "content": [{"type": "file", "file": {"file_id": files[0]}}]}
                ],
            },
        )
        assert chat.status_code == 200 and chat.json()["object"] == "chat.completion", chat.text
        await wait_events(admin, 4)
        assert await http[2].state.usage.process() == 2
        foreign = (
            await issue_key(
                api, token, plane[2], str(plane[5].service_account_id), name="other-file-owner"
            )
        ).json()["secret"]
        publish = await api.post(
            f"/api/v1/gateway-clusters/{plane[7]}/releases",
            headers=headers(token, "file-owner-change"),
            json={},
        )
        assert publish.status_code == 202
        other = {"Authorization": "Bearer " + foreign}
        async with asyncio.timeout(10):
            while (await relay.get("/v1/models", headers=other)).status_code != 200:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
        before = len(calls)
        assert (await relay.get("/v1/files", headers=other)).json()["data"] == []
        assert (await relay.get(path + "/content", headers=other)).status_code == 404
        assert (await relay.post("/v1/responses", headers=other, json=req)).status_code == 404
        assert before == len(calls)
        assert (await relay.delete(path, headers=h)).json() == {
            "id": files[0],
            "object": "file",
            "deleted": True,
        }
        assert (await relay.post("/v1/responses", headers=h, json=req)).status_code == 404
        admin.execute(
            "UPDATE publication.tenant_files SET expires_at=clock_timestamp() "
            "WHERE deleted_at IS NULL"
        )
        assert (await relay.get("/v1/files", headers=h)).json()["data"] == []
