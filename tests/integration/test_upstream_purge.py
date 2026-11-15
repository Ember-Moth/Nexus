"""Tenant resource caps and deletion of upstream copies after a key becomes unusable."""

import asyncio
import json

import pytest
from test_identity_api import auth
from test_relay import relay_binary, tls  # noqa: F401
from test_relay_streams import stream_plane

pytestmark = pytest.mark.integration


async def test_revoked_key_resources_are_purged_upstream(
    http,
    admin,
    tls,  # noqa: F811
    relay_binary,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setenv("NEXUS_RELAY_PURGE_GRACE_SECONDS", "0")
    monkeypatch.setenv("NEXUS_RELAY_PURGE_INTERVAL_MS", "200")
    monkeypatch.setenv("NEXUS_RELAY_RESOURCES_PER_KEY", "1")
    calls = []

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = head[0].split()
            hdr = {k.lower(): v.strip() for row in head[1:] if row for k, v in [row.split(":", 1)]}
            await reader.readexactly(int(hdr.get("content-length", 0)))
            calls.append((method, path))
            if (method, path) == ("POST", "/v1/files"):
                obj = {
                    "object": "file",
                    "id": "file-owned",
                    "bytes": 1,
                    "purpose": "user_data",
                    "created_at": 1700000000,
                    "filename": "a.txt",
                }
            elif (method, path) == ("POST", "/v1/conversations"):
                obj = {"id": "conv_owned", "object": "conversation", "created_at": 1700000000}
            elif method == "DELETE" and path == "/v1/files/file-owned":
                obj = {"object": "file", "id": "file-owned", "deleted": True}
            elif method == "DELETE" and path == "/v1/conversations/conv_owned":
                obj = {"id": "conv_owned", "object": "conversation.deleted", "deleted": True}
            else:
                raise AssertionError((method, path))
            payload = json.dumps(obj).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
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
            features=["files", "conversations"],
        ) as (relay, model, key, _, plane),
    ):
        hdr = {"authorization": f"Bearer {key}"}

        async def upload():
            return await relay.post(
                "/v1/files",
                headers=hdr,
                files={"file": ("a.txt", b"a")},
                data={"purpose": "user_data"},
            )

        first = await upload()
        assert first.status_code == 200 and first.json()["id"].startswith("file_nx_"), first.text
        # Each key may hold one live resource of a kind in this test.
        over = await upload()
        assert over.status_code == 429, over.text
        assert over.json()["error"]["code"] == "resource_limit_exceeded"
        conversation = await relay.post("/v1/conversations", headers=hdr, json={})
        assert conversation.status_code == 200, conversation.text
        assert not any(method == "DELETE" for method, _ in calls)

        client, root, _, _ = http
        org, kid = plane[2], plane[5].api_key_id
        revoked = await client.post(
            f"/api/v1/organizations/{org['id']}/api-keys/{kid}/revoke", headers=auth(root, 1)
        )
        assert revoked.status_code == 200, revoked.text
        async with asyncio.timeout(20):
            while admin.execute(  # noqa: ASYNC110 - wait for the node's purge round
                "SELECT count(*) FROM publication.upstream_purges WHERE state='done'"
            ).fetchone() != (2,):
                await asyncio.sleep(0.1)
        assert ("DELETE", "/v1/files/file-owned") in calls
        assert ("DELETE", "/v1/conversations/conv_owned") in calls
        assert admin.execute(
            "SELECT kind,reason,attempts FROM publication.upstream_purges ORDER BY kind"
        ).fetchall() == [("conversation", "key_inactive", 1), ("file", "key_inactive", 1)]
        assert admin.execute(
            "SELECT count(*) FROM publication.tenant_files WHERE deleted_at IS NULL"
        ).fetchone() == (0,)
        assert admin.execute(
            "SELECT count(*) FROM publication.conversations WHERE deleted_at IS NULL"
        ).fetchone() == (0,)
        assert admin.execute(
            "SELECT action,count(*) FROM publication.audit_events "
            "WHERE action LIKE 'upstream_resource.%' GROUP BY action"
        ).fetchall() == [("upstream_resource.purged", 2)]
        # A second round neither re-enqueues nor re-deletes.
        deletes = sum(method == "DELETE" for method, _ in calls)
        await asyncio.sleep(0.6)
        assert sum(method == "DELETE" for method, _ in calls) == deletes


async def test_unsaved_upstream_resource_is_deleted_or_queued(
    http,
    admin,
    tls,  # noqa: F811
    relay_binary,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setenv("NEXUS_RELAY_PURGE_INTERVAL_MS", "200")
    calls = []
    created = asyncio.Event()
    release = asyncio.Event()

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = head[0].split()
            hdr = {k.lower(): v.strip() for row in head[1:] if row for k, v in [row.split(":", 1)]}
            await reader.readexactly(int(hdr.get("content-length", 0)))
            calls.append((method, path))
            status = "200 OK"
            if (method, path) == ("POST", "/v1/conversations"):
                created.set()
                await release.wait()
                obj = {"id": "conv_lost", "object": "conversation", "created_at": 1700000000}
            elif (method, path) == ("DELETE", "/v1/conversations/conv_lost"):
                # The immediate cleanup fails; the queued purge succeeds.
                first = calls.count((method, path)) == 1
                status = "500 Internal Server Error" if first else status
                obj = {"id": "conv_lost", "object": "conversation.deleted", "deleted": True}
            else:
                raise AssertionError((method, path))
            payload = json.dumps(obj).encode()
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n".encode()
                + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
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
            features=["conversations"],
        ) as (relay, model, key, _, plane),
    ):
        pending = asyncio.create_task(
            relay.post("/v1/conversations", headers={"authorization": f"Bearer {key}"}, json={})
        )
        await asyncio.wait_for(created.wait(), 10)
        client, root, _, _ = http
        org, kid = plane[2], plane[5].api_key_id
        revoked = await client.post(
            f"/api/v1/organizations/{org['id']}/api-keys/{kid}/revoke", headers=auth(root, 1)
        )
        assert revoked.status_code == 200, revoked.text
        release.set()
        # The provider created the conversation, but the binding cannot be saved for this key.
        assert (await pending).status_code >= 400
        async with asyncio.timeout(20):
            while admin.execute(  # noqa: ASYNC110 - wait for the queued purge
                "SELECT state FROM publication.upstream_purges"
            ).fetchall() != [("done",)]:
                await asyncio.sleep(0.1)
        assert calls.count(("DELETE", "/v1/conversations/conv_lost")) == 2
        assert admin.execute(
            "SELECT kind,reason,resource_id FROM publication.upstream_purges"
        ).fetchall() == [("conversation", "orphan", None)]
        assert admin.execute("SELECT count(*) FROM publication.conversations").fetchone() == (0,)
