"""Organizations list the upstream copies their keys hold and purge them on request."""

import asyncio
import json
from uuid import uuid4

import pytest
from test_identity_api import auth, create_org, create_user, login
from test_plane import plane  # noqa: F401
from test_project_credentials import developer
from test_relay import relay_binary, tls  # noqa: F401
from test_relay_streams import stream_plane

pytestmark = pytest.mark.integration


async def member(client, root, org, role):
    user = await create_user(client, root, f"{role}-" + uuid4().hex[:8])
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        headers=auth(root),
        json={"user_id": user["user"]["id"], "role": role},
    )
    assert response.status_code == 201, response.text
    return await login(client, user["user"]["login"])


def seed(admin, kind, cid, dep, kid, provider_id):
    """Bind an upstream copy to a key as a node would; returns its handle ID."""
    handle = uuid4()
    values = (handle, kid, cid, dep["model_id"], dep["id"], dep["credential_id"], provider_id)
    if kind == "file":
        admin.execute(
            "INSERT INTO publication.tenant_files(id,api_key_id,cluster_id,model_id,deployment_id,"
            "credential_id,credential_version,upstream_version,provider_id,metadata,endpoint) "
            "VALUES(%s,%s,%s,%s,%s,%s,1,1,%s,'{}','openai_responses')",
            values,
        )
        return handle
    admin.execute(
        "INSERT INTO publication.conversations(id,api_key_id,cluster_id,model_id,deployment_id,"
        "credential_id,credential_version,upstream_version,provider_id) "
        "VALUES(%s,%s,%s,%s,%s,%s,1,1,%s)",
        values,
    )
    return handle


async def resources(client, token, org_id, **query):
    response = await client.get(
        f"/api/v1/organizations/{org_id}/upstream-resources",
        headers=auth(token),
        params={k: str(v) for k, v in query.items()},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def purge(client, token, org_id, body, key=None):
    return await client.post(
        f"/api/v1/organizations/{org_id}/upstream-purges",
        headers={**auth(token), "Idempotency-Key": key or str(uuid4())},
        json={k: str(v) for k, v in body.items()},
    )


async def progress(client, token, org_id, request_id):
    response = await client.get(
        f"/api/v1/organizations/{org_id}/upstream-purges/{request_id}", headers=auth(token)
    )
    assert response.status_code == 200, response.text
    return response.json()


async def node_round(peer, grace=0, finish="done"):
    """One purge round of a node: schedule, claim, and report every job."""
    jobs = (
        await (
            await peer.connection.execute("SELECT gateway_v1.claim_purges(%s,100)", (grace,))
        ).fetchone()
    )[0]
    for job in jobs:
        await peer.connection.execute(
            "SELECT gateway_v1.finish_purge(%s,%s,%s)",
            (job["id"], finish, None if finish == "done" else "upstream_unavailable"),
        )
    return jobs


async def revoke(client, root, org, kid):
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/api-keys/{kid}/revoke", headers=auth(root, 1)
    )
    assert response.status_code == 200, response.text


async def test_resources_and_requests_stay_inside_their_organization(http, admin, plane):  # noqa: F811
    client, root, _, _ = http
    _, _, org, _, dep, admission, _, cid = plane
    kid = admission.api_key_id
    file_id = seed(admin, "file", cid, dep, kid, "file-a")
    owner = await member(client, root, org, "org_owner")
    other = await create_org(client, root, "Other enterprise")
    other_owner = await member(client, root, other, "org_owner")

    listed = await resources(client, owner, org["id"])
    assert [(r["id"], r["handle"], r["state"]) for r in listed["items"]] == [
        (str(file_id), f"file_nx_{file_id}", "retained")
    ]
    item = listed["items"][0]
    assert item["organization_id"] == org["id"] and item["project_id"] == str(admission.project_id)
    assert item["api_key_id"] == str(kid) and item["purge"] is None
    accepted = await purge(client, owner, org["id"], {"api_key_id": kid})
    assert accepted.status_code == 202, accepted.text
    request_id = accepted.json()["id"]

    # Another organization's owner can neither see nor purge the copy, nor the request.
    for path in (
        f"/api/v1/organizations/{org['id']}/upstream-resources",
        f"/api/v1/organizations/{org['id']}/upstream-purges/{request_id}",
        f"/api/v1/organizations/{other['id']}/upstream-purges/{request_id}",
    ):
        response = await client.get(path, headers=auth(other_owner))
        assert response.status_code == 404, (path, response.text)
    response = await client.get(
        f"/api/v1/organizations/{other['id']}/upstream-resources",
        headers=auth(other_owner),
        params={"api_key_id": str(kid)},
    )
    assert response.status_code == 404, response.text
    assert (await resources(client, other_owner, other["id"]))["items"] == []
    for target in (org, other):
        response = await purge(client, other_owner, target["id"], {"api_key_id": kid})
        assert response.status_code == 404, response.text
    # Global listing is for platform administrators only.
    response = await client.get("/api/v1/upstream-resources", headers=auth(other_owner))
    assert response.status_code == 403, response.text

    # Developers see the keys of granted projects and cannot purge; finance sees nothing.
    granted, _, _ = await developer(client, root, org, str(admission.project_id))
    assert [r["id"] for r in (await resources(client, granted, org["id"]))["items"]] == [
        str(file_id)
    ]
    assert (await progress(client, granted, org["id"], request_id))["api_key_id"] == str(kid)
    response = await purge(client, granted, org["id"], {"api_key_id": kid})
    assert response.status_code == 403, response.text
    ungranted = await member(client, root, org, "org_developer")
    assert (await resources(client, ungranted, org["id"]))["items"] == []
    response = await client.get(
        f"/api/v1/organizations/{org['id']}/upstream-purges/{request_id}",
        headers=auth(ungranted),
    )
    assert response.status_code == 404, response.text
    finance = await member(client, root, org, "org_finance")
    response = await client.get(
        f"/api/v1/organizations/{org['id']}/upstream-resources", headers=auth(finance)
    )
    assert response.status_code == 403, response.text

    # A platform administrator lists across organizations, with the owner of each copy.
    response = await client.get("/api/v1/upstream-resources", headers=auth(root))
    assert response.status_code == 200, response.text
    [listed] = response.json()["items"]
    assert (listed["id"], listed["organization_id"], listed["state"]) == (
        str(file_id),
        org["id"],
        "pending",
    )
    response = await client.get(
        "/api/v1/upstream-resources", headers=auth(root), params={"organization_id": other["id"]}
    )
    assert response.json()["items"] == []
    response = await client.get(
        "/api/v1/upstream-resources", headers=auth(root), params={"project_id": str(uuid4())}
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "organization_required"


async def test_revoked_key_resources_are_listed_and_purged_on_request(
    http,
    admin,
    tls,  # noqa: F811
    relay_binary,  # noqa: F811
    monkeypatch,
):
    # The grace period is long: only the owner's request deletes anything here.
    monkeypatch.setenv("NEXUS_RELAY_PURGE_GRACE_SECONDS", "3600")
    monkeypatch.setenv("NEXUS_RELAY_PURGE_INTERVAL_MS", "200")
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
                    "id": "file-held",
                    "bytes": 1,
                    "purpose": "user_data",
                    "created_at": 1700000000,
                    "filename": "a.txt",
                }
            elif (method, path) == ("POST", "/v1/conversations"):
                obj = {"id": "conv_held", "object": "conversation", "created_at": 1700000000}
            elif (method, path) == ("DELETE", "/v1/files/file-held"):
                obj = {"object": "file", "id": "file-held", "deleted": True}
            elif (method, path) == ("DELETE", "/v1/conversations/conv_held"):
                obj = {"id": "conv_held", "object": "conversation.deleted", "deleted": True}
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
        ) as (relay, model, key, _, provisioned),
    ):
        hdr = {"authorization": f"Bearer {key}"}
        uploaded = await relay.post(
            "/v1/files",
            headers=hdr,
            files={"file": ("a.txt", b"a")},
            data={"purpose": "user_data"},
        )
        assert uploaded.status_code == 200, uploaded.text
        conversation = await relay.post("/v1/conversations", headers=hdr, json={})
        assert conversation.status_code == 200, conversation.text
        client, root, _, _ = http
        org, kid = provisioned[2], provisioned[5].api_key_id
        owner = await member(client, root, org, "org_owner")
        await revoke(client, root, org, kid)

        listed = await resources(client, owner, org["id"], api_key_id=kid)
        handles = {r["kind"]: r["handle"] for r in listed["items"]}
        assert handles == {
            "file": uploaded.json()["id"],
            "conversation": conversation.json()["id"],
        }
        assert {r["state"] for r in listed["items"]} == {"retained"}
        file_id = handles["file"].removeprefix("file_nx_")

        accepted = await purge(
            client, owner, org["id"], {"api_key_id": kid, "kind": "file", "resource_id": file_id}
        )
        assert accepted.status_code == 202, accepted.text
        request = accepted.json()
        assert accepted.headers["location"].endswith(f"/upstream-purges/{request['id']}")
        assert (request["state"], request["total"], request["pending"]) == ("pending", 1, 1)
        async with asyncio.timeout(20):
            while request["state"] == "pending":
                await asyncio.sleep(0.1)
                request = await progress(client, owner, org["id"], request["id"])
        assert (request["state"], request["purged"]) == ("purged", 1)
        assert calls.count(("DELETE", "/v1/files/file-held")) == 1
        assert ("DELETE", "/v1/conversations/conv_held") not in calls
        states = {
            r["kind"]: (r["state"], r["purge"] and r["purge"]["reason"])
            for r in (await resources(client, owner, org["id"]))["items"]
        }
        assert states == {"file": ("purged", "owner_request"), "conversation": ("retained", None)}

        # Every copy the revoked key still holds, then a replay of the same request.
        idem = "purge-revoked-key"
        accepted = await purge(client, owner, org["id"], {"api_key_id": kid}, idem)
        assert accepted.status_code == 202, accepted.text
        request = accepted.json()
        assert (request["total"], request["kind"]) == (1, None)
        async with asyncio.timeout(20):
            while request["state"] == "pending":
                await asyncio.sleep(0.1)
                request = await progress(client, owner, org["id"], request["id"])
        assert request["state"] == "purged"
        replay = await purge(client, owner, org["id"], {"api_key_id": kid}, idem)
        assert replay.status_code == 202 and replay.json() == request
        assert calls.count(("DELETE", "/v1/conversations/conv_held")) == 1
        assert admin.execute(
            "SELECT count(*) FROM publication.conversations WHERE deleted_at IS NULL"
        ).fetchone() == (0,)
        assert admin.execute(
            "SELECT action,details->>'reason',count(*) FROM publication.audit_events "
            "WHERE action LIKE 'upstream_resource.%' GROUP BY 1,2 ORDER BY 1"
        ).fetchall() == [
            ("upstream_resource.purge_requested", None, 2),
            ("upstream_resource.purged", "owner_request", 2),
        ]


async def test_purge_requests_are_idempotent_and_retry_failed_purges(http, admin, plane):  # noqa: F811
    client, root, _, _ = http
    peer, _, org, _, dep, admission, _, cid = plane
    kid = admission.api_key_id
    first = seed(admin, "file", cid, dep, kid, "file-one")
    second = seed(admin, "conversation", cid, dep, kid, "conv_two")
    owner = await member(client, root, org, "org_owner")

    accepted = await purge(client, owner, org["id"], {"api_key_id": kid}, "owner-purge")
    assert accepted.status_code == 202, accepted.text
    request = accepted.json()
    assert (request["state"], request["total"], request["pending"]) == ("pending", 2, 2)
    replay = await purge(client, owner, org["id"], {"api_key_id": kid}, "owner-purge")
    assert replay.status_code == 202 and replay.json()["id"] == request["id"]
    conflict = await purge(
        client,
        owner,
        org["id"],
        {"api_key_id": kid, "kind": "file", "resource_id": first},
        "owner-purge",
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    # A new key covers the same purges instead of queueing them twice.
    again = (await purge(client, owner, org["id"], {"api_key_id": kid})).json()
    assert again["id"] != request["id"] and again["total"] == 2
    single = await purge(
        client, owner, org["id"], {"api_key_id": kid, "kind": "conversation", "resource_id": second}
    )
    assert single.status_code == 202 and single.json()["total"] == 1
    missing = await purge(
        client, owner, org["id"], {"api_key_id": kid, "kind": "file", "resource_id": second}
    )
    assert missing.status_code == 404, missing.text
    malformed = await purge(client, owner, org["id"], {"api_key_id": kid, "kind": "file"})
    assert malformed.status_code == 422, malformed.text
    assert admin.execute(
        "SELECT reason,state,count(*) FROM publication.upstream_purges GROUP BY 1,2"
    ).fetchall() == [("owner_request", "pending", 2)]
    assert admin.execute(
        "SELECT count(*) FROM publication.audit_events "
        "WHERE action='upstream_resource.purge_requested'"
    ).fetchone() == (3,)
    no_key = await client.post(
        f"/api/v1/organizations/{org['id']}/upstream-purges",
        headers=auth(owner),
        json={"api_key_id": str(kid)},
    )
    assert no_key.status_code == 428, no_key.text

    # The file's purge gives up on its last attempt; a new request retries it from scratch.
    admin.execute(
        "UPDATE publication.upstream_purges SET attempts=29 WHERE resource_id=%s", (first,)
    )
    jobs = await node_round(peer, grace=3600, finish="failed")
    assert len(jobs) == 2
    assert (await progress(client, owner, org["id"], request["id"]))["state"] == "pending"
    admin.execute(
        "UPDATE publication.upstream_purges SET next_attempt=clock_timestamp() "
        "WHERE state='pending'"
    )
    await node_round(peer, grace=3600, finish="done")
    status = await progress(client, owner, org["id"], request["id"])
    assert (status["state"], status["purged"], status["failed"]) == ("failed", 1, 1)
    states = {r["kind"]: r for r in (await resources(client, owner, org["id"]))["items"]}
    assert states["file"]["state"] == "failed"
    assert states["file"]["purge"]["last_error"] == "upstream_unavailable"
    assert states["conversation"]["state"] == "purged"
    retry = (await purge(client, owner, org["id"], {"api_key_id": kid})).json()
    assert (retry["state"], retry["total"], retry["pending"]) == ("pending", 1, 1)
    assert admin.execute(
        "SELECT state,attempts FROM publication.upstream_purges WHERE resource_id=%s", (first,)
    ).fetchone() == ("pending", 0)
    await node_round(peer, grace=3600)
    assert (await progress(client, owner, org["id"], retry["id"]))["state"] == "purged"
    # Nothing is left: a further request covers no purge and is complete at once.
    empty = (await purge(client, owner, org["id"], {"api_key_id": kid})).json()
    assert (empty["state"], empty["total"]) == ("purged", 0)


async def test_owner_requests_and_the_grace_period_schedule_share_purges(http, admin, plane):  # noqa: F811
    client, root, _, _ = http
    peer, _, org, _, dep, admission, _, cid = plane
    keys = [
        row[0]
        for row in admin.execute(
            "SELECT id FROM identity.api_keys WHERE organization_id=%s ORDER BY created_at",
            (org["id"],),
        ).fetchall()
    ]
    assert len(keys) == 2
    copies = {
        kid: [seed(admin, "file", cid, dep, kid, f"file-{n}-{i}") for i in range(3)]
        for n, kid in enumerate(keys)
    }
    for kid in keys:
        await revoke(client, root, org, kid)
    owner = await member(client, root, org, "org_owner")

    # The schedule queues the first key's copies and holds them uncommitted; the request waits
    # for that transaction, then covers the purges it queued instead of adding its own.
    async with peer.connection.transaction():
        scheduled = (
            await (
                await peer.connection.execute("SELECT gateway_v1.claim_purges(0,100)")
            ).fetchone()
        )[0]
        assert len(scheduled) == 6
        pending = asyncio.create_task(purge(client, owner, org["id"], {"api_key_id": keys[0]}))
        await asyncio.sleep(0.5)
        assert not pending.done()
    first = (await pending).json()
    assert (first["total"], first["pending"]) == (3, 3)
    assert admin.execute(
        "SELECT reason,count(*) FROM publication.upstream_purges GROUP BY 1"
    ).fetchall() == [("key_inactive", 6)]

    # Purged copies of the second key are queued again neither by a request nor the schedule.
    for job in scheduled:
        if job["api_key_id"] == str(keys[1]):
            await peer.connection.execute(
                "SELECT gateway_v1.finish_purge(%s,'done',NULL)", (job["id"],)
            )
    assert admin.execute(
        "SELECT count(*) FROM publication.tenant_files WHERE api_key_id=%s AND deleted_at IS NULL",
        (keys[1],),
    ).fetchone() == (0,)
    extra = seed(admin, "file", cid, dep, keys[1], "file-late")
    second = (await purge(client, owner, org["id"], {"api_key_id": keys[1]})).json()
    assert (second["total"], second["pending"]) == (1, 1)
    late = await node_round(peer, grace=0)
    assert [job["resource_id"] for job in late] == [str(extra)]
    assert admin.execute(
        "SELECT reason,count(*) FROM publication.upstream_purges GROUP BY 1 ORDER BY 1"
    ).fetchall() == [("key_inactive", 6), ("owner_request", 1)]

    # The node reports the rest of the jobs it claimed in the first round.
    for job in scheduled:
        if job["api_key_id"] == str(keys[0]):
            await peer.connection.execute(
                "SELECT gateway_v1.finish_purge(%s,'done',NULL)", (job["id"],)
            )
    assert sorted(job["resource_id"] for job in scheduled) == sorted(
        str(handle) for handle in copies[keys[0]] + copies[keys[1]]
    )
    for request in (first, second):
        assert (await progress(client, owner, org["id"], request["id"]))["state"] == "purged"
    assert admin.execute(
        "SELECT count(*) FROM publication.tenant_files WHERE deleted_at IS NULL"
    ).fetchone() == (0,)


async def test_listing_pages_filters_and_unsaved_copies(http, admin, plane):  # noqa: F811
    client, root, _, _ = http
    _, _, org, _, dep, admission, _, cid = plane
    kid = admission.api_key_id
    conversation = seed(admin, "conversation", cid, dep, kid, "conv_first")
    files = [seed(admin, "file", cid, dep, kid, f"file-{i}") for i in range(3)]
    orphan = admin.execute(
        "INSERT INTO publication.upstream_purges(cluster_id,kind,api_key_id,deployment_id,"
        "credential_id,credential_version,endpoint,provider_id,reason) "
        "VALUES(%s,'conversation',%s,%s,%s,1,'openai_responses','conv_lost','orphan') RETURNING id",
        (cid, kid, dep["id"], dep["credential_id"]),
    ).fetchone()[0]

    seen, cursor = [], None
    while True:
        page = await resources(
            client, root, org["id"], limit=2, **({"cursor": cursor} if cursor else {})
        )
        seen.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    # Newest first across kinds, with the copy whose handle was never saved.
    assert [r["id"] for r in seen] == [str(orphan), *map(str, reversed(files)), str(conversation)]
    assert seen[0]["handle"] is None and seen[0]["state"] == "pending"
    assert seen[0]["purge"]["reason"] == "orphan"
    assert seen[-1]["handle"] == f"conv_nx_{conversation}"
    assert all(r["created_at"] for r in seen)
    only = await resources(client, root, org["id"], kind="conversation", state="retained")
    assert [r["id"] for r in only["items"]] == [str(conversation)]
    response = await client.get(
        f"/api/v1/organizations/{org['id']}/upstream-resources",
        headers=auth(root),
        params={"cursor": "not-a-cursor"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_cursor"
    # An orphan of the key is covered by a request for the key.
    request = (await purge(client, root, org["id"], {"api_key_id": kid})).json()
    assert request["total"] == 5
