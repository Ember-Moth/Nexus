"""Provider file references are looked up again when opening converted history."""

import asyncio
import json

import pytest
from test_interpreter import configure
from test_relay import relay_binary, tls, wait_events  # noqa: F401
from test_relay_private_history import messages_events, read_messages, responses_events
from test_relay_streams import stream_plane

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("family", ["anthropic", "openai_compatible"])
async def test_file_citations_recheck_the_owned_file(http, admin, tls, relay_binary, family):  # noqa: F811
    claude = family == "anthropic"
    direction = "openai_responses" if claude else "anthropic_messages"
    pid = "private-provider-file"
    raw_cite = (
        {
            "type": "char_location",
            "file_id": pid,
            "cited_text": "sky",
            "document_title": "note",
            "document_index": 0,
            "start_char_index": 0,
            "end_char_index": 3,
        }
        if claude
        else {"type": "file_citation", "file_id": pid, "filename": "note.txt", "index": 0}
    )
    container = "private-provider-container"
    artifact = "private-provider-artifact"
    sent = []
    negotiated = []

    async def provider(reader, writer):
        try:
            lines = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = lines[0].split()
            hdr = {k.lower(): v.strip() for x in lines[1:] if x for k, v in [x.split(":", 1)]}
            payload = await reader.readexactly(int(hdr.get("content-length", 0)))
            if path == "/v1/files" and method == "POST":
                result = (
                    {
                        "id": pid,
                        "filename": "note.txt",
                        "mime_type": "text/plain",
                        "size_bytes": 3,
                        "downloadable": True,
                        "created_at": "2026-11-14T00:00:00Z",
                        "type": "file",
                    }
                    if claude
                    else {
                        "id": pid,
                        "filename": "note.txt",
                        "object": "file",
                        "bytes": 3,
                        "purpose": "user_data",
                        "created_at": 1,
                    }
                )
            elif path == f"/v1/files/{pid}" and method == "DELETE":
                result = (
                    {"id": pid, "type": "file_deleted"}
                    if claude
                    else {"id": pid, "object": "file", "deleted": True}
                )
            elif method == "POST" and path == "/v1/containers":
                result = {
                    "id": container,
                    "object": "container",
                    "name": "owned",
                    "memory_limit": "1g",
                    "status": "running",
                    "created_at": 1781234567,
                    "last_active_at": 1781234567,
                    "expires_after": {"anchor": "last_active_at", "minutes": 20},
                }
            elif method == "POST" and path == f"/v1/containers/{container}/files":
                result = {
                    "object": "container.file",
                    "id": artifact,
                    "container_id": container,
                    "path": "result.txt",
                    "bytes": 3,
                    "created_at": 1781234567,
                    "source": "user",
                }
            elif method == "DELETE" and path == f"/v1/containers/{container}/files/{artifact}":
                result = {
                    "object": "container.file.deleted",
                    "id": artifact,
                    "container_id": container,
                    "deleted": True,
                }
            elif method == "DELETE" and path == f"/v1/containers/{container}":
                result = {"object": "container.deleted", "id": container, "deleted": True}
            else:
                q = json.loads(payload)
                sent.append(q)
                negotiated.append(hdr.get("anthropic-beta"))
                result = (
                    {
                        "id": "m",
                        "type": "message",
                        "role": "assistant",
                        "model": q["model"],
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "sky", "citations": [raw_cite]}],
                        "usage": {
                            "input_tokens": 2,
                            "output_tokens": 1,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    }
                    if claude
                    else {
                        "id": "r",
                        "object": "response",
                        "status": "completed",
                        "model": q["model"],
                        "output": [
                            {
                                "type": "message",
                                "id": "m",
                                "role": "assistant",
                                "status": "completed",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "sky",
                                        "annotations": [raw_cite],
                                    }
                                ],
                            }
                        ],
                        "usage": {
                            "input_tokens": 2,
                            "output_tokens": 1,
                            "input_tokens_details": {"cached_tokens": 0},
                        },
                    }
                )
                if q.get("stream"):
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                        b"Connection: close\r\n\r\n"
                    )
                    for e in messages_events(result) if claude else responses_events(result):
                        writer.write(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())
                    await writer.drain()
                    return
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Connection: close\r\n\r\n" + json.dumps(result).encode()
            )
            await writer.drain()
        finally:
            writer.close()

    peer = await asyncio.start_server(provider, "127.0.0.1", 0, ssl=tls[0])
    async with (
        peer,
        stream_plane(
            http,
            admin,
            relay_binary,
            tls[1],
            peer.sockets[0].getsockname()[1],
            family,
            convert_from=direction,
            features=["files", "code_execution"],
        ) as (relay, model, key, _, plane),
    ):
        vendor_headers = {"x-api-key": key} if claude else {"Authorization": "Bearer " + key}
        r = await relay.post(
            "/v1/files",
            headers=vendor_headers,
            files={"file": ("note.txt", b"sky", "text/plain")},
            data={} if claude else {"purpose": "user_data"},
        )
        assert r.status_code == 200, r.text
        handle = r.json()["id"]
        source = (
            {"type": "input_file", "file_id": handle, "citations": {"enabled": True}}
            if claude
            else {"type": "document", "source": {"type": "file", "file_id": handle}}
        )
        content = [source, {"type": "input_text" if claude else "text", "text": "cite"}]
        auth = (
            {"Authorization": "Bearer " + key}
            if claude
            else {"x-api-key": key, "anthropic-beta": "files-api-2025-04-14"}
        )
        path = "/v1/responses" if claude else "/v1/messages"
        hist = "input" if claude else "messages"
        query = {
            "model": model,
            hist: [{"role": "user", "content": content}],
            ("max_output_tokens" if claude else "max_tokens"): 6,
        }
        for stream in [False, True]:
            first = await relay.post(path, headers=auth, json=query | {"stream": stream})
            assert first.status_code == 200, first.text
            assert pid not in first.text
            if claude:
                assert sent[-1]["messages"][0]["content"][0]["source"]["file_id"] == pid
                assert negotiated[-1] == "files-api-2025-04-14"
            else:
                assert sent[-1]["input"][0]["content"][0]["file_id"] == pid
            if stream:
                frames = [
                    json.loads(line[6:])
                    for line in first.text.splitlines()
                    if line.startswith("data: ")
                ]
                items = frames[-1]["response"]["output"] if claude else read_messages(frames)
            else:
                items = first.json()["output" if claude else "content"]
            if claude:
                assert items[0]["nexus_citations"][0]["file_id"] == handle
            else:
                assert items[-1]["type"] == "redacted_thinking"
                assert items[-1]["nexus_annotations"][0]["annotation"]["file_id"] == handle
            follow = query | {
                hist: [
                    {"role": "user", "content": "go"},
                    *(items if claude else [{"role": "assistant", "content": items}]),
                    {"role": "user", "content": "again"},
                ]
            }
            replay = await relay.post(path, headers=auth, json=follow)
            # A replay without the original document still checks its action-scoped file handle.
            assert replay.status_code == 200, replay.text
            if claude:
                assert sent[-1]["messages"][1]["content"][0]["citations"] == [
                    {k: v for k, v in raw_cite.items() if k != "file_id"}
                ]
            else:
                assert sent[-1]["input"][1]["content"][0]["annotations"] == [raw_cite]
        await wait_events(admin, 8)
        assert await http[2].state.usage.process() == 4
        deleted = await relay.delete(f"/v1/files/{handle}", headers=vendor_headers)
        assert deleted.status_code == 200, deleted.text
        denied = await relay.post(path, headers=auth, json=follow)
        assert denied.status_code == 404, denied.text
        assert len(sent) == 4 and admin.execute(
            "SELECT count(*) FROM billing.request_runs"
        ).fetchone() == (4,)

        if not claude:
            await configure(http, admin, plane, "session_20m")
            created = await relay.post(
                "/v1/containers", headers=vendor_headers, json={"name": "owned"}
            )
            assert created.status_code == 200, created.text
            cid = created.json()["id"]
            root = f"/v1/containers/{cid}"
            uploaded = await relay.post(
                root + "/files",
                headers=vendor_headers,
                files={"file": ("result.txt", b"sky", "text/plain")},
            )
            assert uploaded.status_code == 200, uploaded.text
            fid = uploaded.json()["id"]
            raw_cite = {
                "type": "container_file_citation",
                "container_id": container,
                "file_id": artifact,
                "filename": "result.txt",
                "start_index": 0,
                "end_index": 3,
            }
            query = {
                "model": model,
                "max_tokens": 6,
                "messages": [{"role": "user", "content": "go"}],
            }
            h = {"x-api-key": key}
            answer = await relay.post(path, headers=h, json=query | {"stream": True})
            frames = [json.loads(s[6:]) for s in answer.text.splitlines() if s.startswith("data: ")]
            items = read_messages(frames)
            exposed = items[-1]["nexus_annotations"][0]["annotation"]
            assert exposed["container_id"] == cid and exposed["file_id"] == fid
            follow = query | {
                "messages": [
                    *query["messages"],
                    {"role": "assistant", "content": items},
                    {"role": "user", "content": "next"},
                ]
            }
            again = await relay.post(path, headers=h, json=follow)
            assert again.status_code == 200, again.text
            assert sent[-1]["input"][1]["content"][0]["annotations"] == [raw_cite]
            assert (
                await relay.delete(root + "/files/" + fid, headers=vendor_headers)
            ).status_code == 200
            before = len(sent)
            assert (await relay.post(path, headers=h, json=follow)).status_code == 404
            assert len(sent) == before
            assert (await relay.delete(root, headers=vendor_headers)).status_code == 200
