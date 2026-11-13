import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from test_identity_api import auth, create_org, create_user, login

from nexus.modules.identity.errors import IdentityError

pytestmark = pytest.mark.integration


def headers(token, *, version=None, idem=None):
    result = auth(token, version)
    if idem:
        result["Idempotency-Key"] = idem
    return result


async def project(client, token, org, name="Project"):
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/projects", headers=auth(token), json={"name": name}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def account(client, token, org, project_id, name="Application"):
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/service-accounts",
        headers=auth(token),
        json={"project_id": project_id, "name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def key(client, token, org, account_id, *, idem=None, **values):
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/api-keys",
        headers=headers(token, idem=idem or str(uuid4())),
        json={"service_account_id": account_id, "name": "Application Key", **values},
    )
    assert response.status_code in {200, 201}, response.text
    return response


async def developer(client, root, org, project_id):
    user = await create_user(client, root, "developer-" + uuid4().hex[:8])
    base = f"/api/v1/organizations/{org['id']}"
    member = await client.post(
        base + "/members",
        headers=auth(root),
        json={"user_id": user["user"]["id"], "role": "org_developer"},
    )
    assert member.status_code == 201
    grant = await client.post(
        base + "/project-grants",
        headers=auth(root),
        json={"project_id": project_id, "membership_id": member.json()["id"]},
    )
    assert grant.status_code == 201, grant.text
    return await login(client, user["user"]["login"]), member.json(), grant.json()


async def test_project_scopes_limit_developer_and_finance_access(http):
    client, root, _, _ = http
    org = await create_org(client, root, "Scoped organization")
    first = await project(client, root, org, "Allowed")
    second = await project(client, root, org, "Hidden")
    a = await account(client, root, org, first["id"], "Allowed app")
    b = await account(client, root, org, second["id"], "Hidden app")
    await key(client, root, org, a["id"])
    hidden_key = (await key(client, root, org, b["id"])).json()["key"]
    token, _, _ = await developer(client, root, org, first["id"])
    base = f"/api/v1/organizations/{org['id']}"
    assert [
        x["id"] for x in (await client.get(base + "/projects", headers=auth(token))).json()["items"]
    ] == [first["id"]]
    assert [
        x["id"]
        for x in (await client.get(base + "/service-accounts", headers=auth(token))).json()["items"]
    ] == [a["id"]]
    assert len((await client.get(base + "/api-keys", headers=auth(token))).json()["items"]) == 1
    for path in [
        f"/projects/{second['id']}",
        f"/service-accounts/{b['id']}",
        f"/api-keys/{hidden_key['id']}",
    ]:
        assert (await client.get(base + path, headers=auth(token))).status_code == 404
    assert (
        await client.get(
            base + "/service-accounts", headers=auth(token), params={"project_id": second["id"]}
        )
    ).status_code == 404
    assert (
        await client.post(
            base + "/service-accounts",
            headers=auth(token),
            json={"name": "unauthorized", "project_id": second["id"]},
        )
    ).status_code == 404
    assert (
        await client.post(base + "/projects", headers=auth(token), json={"name": "forbidden"})
    ).status_code == 403
    assert (await client.get(base + "/project-grants", headers=auth(token))).status_code == 403
    await account(client, token, org, first["id"], "Developer app")
    finance = await create_user(client, root, "org-finance")
    finance_member = await client.post(
        base + "/members",
        headers=auth(root),
        json={"user_id": finance["user"]["id"], "role": "org_finance"},
    )
    assert (
        await client.post(
            base + "/project-grants",
            headers=auth(root),
            json={"project_id": first["id"], "membership_id": finance_member.json()["id"]},
        )
    ).status_code == 409
    finance_token = await login(client, "org-finance")
    assert (
        len((await client.get(base + "/projects", headers=auth(finance_token))).json()["items"])
        == 2
    )
    assert (await client.get(base + "/api-keys", headers=auth(finance_token))).status_code == 403
    assert (
        await client.get(base + "/service-accounts", headers=auth(finance_token))
    ).status_code == 403


async def test_cross_tenant_foreign_keys_and_resource_lookup(http, admin):
    client, root, _, _ = http
    a = await create_org(client, root, "A")
    b = await create_org(client, root, "B")
    pa = await project(client, root, a)
    pb = await project(client, root, b)
    sb = await account(client, root, b, pb["id"])
    _, member_b, _ = await developer(client, root, b, pb["id"])
    base = f"/api/v1/organizations/{a['id']}"
    assert (
        await client.post(
            base + "/project-grants",
            headers=auth(root),
            json={"project_id": pa["id"], "membership_id": member_b["id"]},
        )
    ).status_code == 404
    assert (
        await client.post(
            base + "/service-accounts",
            headers=auth(root),
            json={"project_id": pb["id"], "name": "wrong"},
        )
    ).status_code == 404
    assert (
        await client.post(
            base + "/api-keys",
            headers=headers(root, idem="cross-org"),
            json={"service_account_id": sb["id"], "name": "wrong"},
        )
    ).status_code == 404
    assert (
        await client.get(base + f"/service-accounts/{sb['id']}", headers=auth(root))
    ).status_code == 404
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            "INSERT INTO identity.project_grants(id, organization_id, project_id, membership_id) "
            "VALUES (%s,%s,%s,%s)",
            (uuid4(), a["id"], pa["id"], member_b["id"]),
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            "INSERT INTO identity.api_keys(id, organization_id, project_id, service_account_id, "
            "name, digest, prefix) VALUES (%s,%s,%s,%s,'bad','fake-digest','nxk_fake')",
            (uuid4(), a["id"], pa["id"], sb["id"]),
        )


async def test_key_secret_once_idempotency_and_credential_types(http, admin):
    client, root, app, _ = http
    org = await create_org(client, root, "Keys")
    p = await project(client, root, org)
    sa = await account(client, root, org, p["id"])
    base = f"/api/v1/organizations/{org['id']}/api-keys"
    payload = {"name": "Concurrent key", "service_account_id": sa["id"]}
    results = await asyncio.gather(
        *(
            client.post(base, headers=headers(root, idem="same-intent"), json=payload)
            for _ in range(2)
        )
    )
    assert sorted(r.status_code for r in results) == [200, 201]
    original = next(r.json() for r in results if r.status_code == 201)
    replay = next(r.json() for r in results if r.status_code == 200)
    secret = original["secret"]
    assert secret.startswith("nxk_") and replay["secret"] is None
    assert replay["secret_status"] == "secret_unavailable"
    assert replay["key"]["id"] == original["key"]["id"]
    assert admin.execute("SELECT count(*) FROM identity.api_keys").fetchone()[0] == 1
    assert (
        admin.execute("SELECT digest FROM identity.api_keys").fetchone()[0]
        == hashlib.sha256(secret.encode()).hexdigest()
    )
    for path in [base, base + "/" + original["key"]["id"]]:
        response = await client.get(path, headers=auth(root))
        assert secret not in response.text and "digest" not in response.text
    evidence = json.dumps(admin.execute("SELECT details FROM identity.audit_events").fetchall())
    assert secret not in evidence
    assert secret not in json.dumps(
        admin.execute(
            "SELECT request_digest,key_digest FROM identity.idempotency_records"
        ).fetchall()
    )
    assert (
        await client.post(
            base, headers=headers(root, idem="same-intent"), json=payload | {"name": "changed"}
        )
    ).status_code == 409
    assert (await client.post(base, headers=auth(root), json=payload)).status_code == 428
    assert (await client.get("/api/v1/me", headers=auth(secret))).status_code == 401
    with pytest.raises(IdentityError):
        await app.state.project_service.authenticate_api_key(secret)
    admin.execute("UPDATE identity.organizations SET status='active' WHERE id=%s", (org["id"],))
    principal = await app.state.project_service.authenticate_api_key(secret)
    assert str(principal.project_id) == p["id"]
    with pytest.raises(IdentityError):
        await app.state.project_service.authenticate_api_key(root)
    assert (
        await client.post(
            base, headers=headers(root, idem="mass-assignment"), json=payload | {"digest": "chosen"}
        )
    ).status_code == 422


async def test_rotation_overlap_immediate_revocation_and_replay(http, admin):
    client, root, app, _ = http
    org = await create_org(client, root, "Rotation")
    p = await project(client, root, org)
    sa = await account(client, root, org, p["id"])
    original = (await key(client, root, org, sa["id"])).json()
    admin.execute("UPDATE identity.organizations SET status='active' WHERE id=%s", (org["id"],))
    base = f"/api/v1/organizations/{org['id']}/api-keys"
    path = base + "/" + original["key"]["id"] + "/rotate"
    responses = await asyncio.gather(
        *(
            client.post(
                path,
                headers=headers(root, version=1, idem="rotate-once"),
                json={"grace_seconds": 300},
            )
            for _ in range(2)
        )
    )
    assert sorted(item.status_code for item in responses) == [200, 201]
    response = next(item for item in responses if item.status_code == 201)
    assert next(item for item in responses if item.status_code == 200).json()["secret"] is None
    replacement = response.json()
    await app.state.project_service.authenticate_api_key(original["secret"])
    await app.state.project_service.authenticate_api_key(replacement["secret"])
    old = (await client.get(base + "/" + original["key"]["id"], headers=auth(root))).json()
    assert old["version"] == 2 and old["rotated_at"] is not None
    assert (
        0 < (datetime.fromisoformat(old["expires_at"]) - datetime.now(UTC)).total_seconds() <= 300
    )
    assert replacement["key"]["expires_at"] is None
    retry = await client.post(
        path, headers=headers(root, version=1, idem="rotate-once"), json={"grace_seconds": 300}
    )
    assert retry.status_code == 200 and retry.json()["secret"] is None
    assert retry.json()["key"]["id"] == replacement["key"]["id"]
    assert (
        await client.post(path, headers=headers(root, version=2, idem="rotate-again"), json={})
    ).status_code == 409
    admin.execute(
        "UPDATE identity.api_keys SET expires_at=now()-interval '1 second' WHERE id=%s",
        (old["id"],),
    )
    with pytest.raises(IdentityError):
        await app.state.project_service.authenticate_api_key(original["secret"])
    immediate = await client.post(
        base + "/" + replacement["key"]["id"] + "/rotate",
        headers=headers(root, version=1, idem="rotate-immediate"),
        json={},
    )
    assert immediate.status_code == 201, immediate.text
    with pytest.raises(IdentityError):
        await app.state.project_service.authenticate_api_key(replacement["secret"])
    newest = immediate.json()
    await app.state.project_service.authenticate_api_key(newest["secret"])
    revoked = await client.post(
        base + "/" + newest["key"]["id"] + "/revoke", headers=headers(root, version=1)
    )
    assert revoked.status_code == 200
    with pytest.raises(IdentityError):
        await app.state.project_service.authenticate_api_key(newest["secret"])


async def test_grant_and_member_revocation_affect_management_not_service_identity(http, admin):
    client, root, app, _ = http
    org = await create_org(client, root, "Independent identity")
    p = await project(client, root, org)
    token, member, grant = await developer(client, root, org, p["id"])
    sa = await account(client, token, org, p["id"])
    credential = (await key(client, token, org, sa["id"], idem="developer-key")).json()
    admin.execute("UPDATE identity.organizations SET status='active' WHERE id=%s", (org["id"],))
    base = f"/api/v1/organizations/{org['id']}"
    assert (
        await client.delete(
            base + "/project-grants/" + grant["id"], headers=headers(root, version=1)
        )
    ).status_code == 204
    assert (await client.get(base + "/projects", headers=auth(token))).json()["items"] == []
    retry = await client.post(
        base + "/api-keys",
        headers=headers(token, idem="developer-key"),
        json={"service_account_id": sa["id"], "name": "Application Key"},
    )
    assert retry.status_code == 404
    await app.state.project_service.authenticate_api_key(credential["secret"])
    assert (
        await client.patch(
            base + "/project-grants/" + grant["id"],
            headers=headers(root, version=2),
            json={"status": "active"},
        )
    ).status_code == 200
    assert (await client.get(base + "/projects", headers=auth(token))).json()["items"][0][
        "id"
    ] == p["id"]
    assert (
        await client.delete(base + "/members/" + member["id"], headers=headers(root, version=1))
    ).status_code == 204
    assert (await client.get(base + "/projects", headers=auth(token))).status_code == 404
    await app.state.project_service.authenticate_api_key(credential["secret"])


async def test_parent_states_expiry_and_versions_are_enforced(http, admin):
    client, root, app, _ = http
    org = await create_org(client, root, "Parent state")
    p = await project(client, root, org)
    sa = await account(client, root, org, p["id"])
    raw = (await key(client, root, org, sa["id"])).json()
    base = f"/api/v1/organizations/{org['id']}"
    admin.execute("UPDATE identity.organizations SET status='active' WHERE id=%s", (org["id"],))

    async def denied():
        with pytest.raises(IdentityError):
            await app.state.project_service.authenticate_api_key(raw["secret"])

    sa_path = base + "/service-accounts/" + sa["id"]
    assert (
        await client.patch(sa_path, headers=headers(root, version=1), json={"status": "disabled"})
    ).status_code == 200
    await denied()
    request = {"name": "blocked", "service_account_id": sa["id"]}
    assert (
        await client.post(
            base + "/api-keys", headers=headers(root, idem="disabled-sa"), json=request
        )
    ).status_code == 409
    assert (
        await client.patch(sa_path, headers=headers(root, version=2), json={"status": "active"})
    ).status_code == 200
    await app.state.project_service.authenticate_api_key(raw["secret"])
    project_path = base + "/projects/" + p["id"]
    assert (
        await client.patch(project_path, headers=auth(root), json={"status": "disabled"})
    ).status_code == 428
    assert (
        await client.patch(
            project_path, headers=headers(root, version=99), json={"status": "disabled"}
        )
    ).status_code == 412
    assert (
        await client.patch(
            project_path, headers=headers(root, version=1), json={"status": "disabled"}
        )
    ).status_code == 200
    await denied()
    assert (
        await client.patch(
            project_path, headers=headers(root, version=2), json={"status": "active"}
        )
    ).status_code == 200
    await app.state.project_service.authenticate_api_key(raw["secret"])
    assert (
        await client.patch(base, headers=headers(root, version=1), json={"status": "suspended"})
    ).status_code == 200
    await denied()
    assert (
        await client.patch(base, headers=headers(root, version=2), json={"status": "active"})
    ).status_code == 200
    assert (
        await client.patch(
            project_path, headers=headers(root, version=3), json={"status": "archived"}
        )
    ).status_code == 200
    await denied()
    assert (
        await client.patch(
            project_path, headers=headers(root, version=4), json={"status": "active"}
        )
    ).status_code == 409
    assert (
        await client.post(
            base + "/api-keys/" + raw["key"]["id"] + "/revoke", headers=headers(root, version=1)
        )
    ).status_code == 200


async def test_key_validation_and_pagination(http):
    client, root, _, _ = http
    org = await create_org(client, root, "Validation")
    p = await project(client, root, org)
    sa = await account(client, root, org, p["id"])
    base = f"/api/v1/organizations/{org['id']}/api-keys"
    request = {"name": "validation", "service_account_id": sa["id"]}
    for expires in [(datetime.now(UTC) - timedelta(seconds=1)).isoformat(), "2040-01-01T00:00:00"]:
        response = await client.post(
            base, headers=headers(root, idem=str(uuid4())), json=request | {"expires_at": expires}
        )
        assert response.status_code == 422
    response = await client.post(base, headers=headers(root, idem="invalid key"), json=request)
    assert response.status_code == 422
    for index in range(3):
        await key(client, root, org, sa["id"], name=f"key-{index}")
    first = await client.get(base, headers=auth(root), params={"limit": 2})
    second = await client.get(
        base, headers=auth(root), params={"limit": 2, "cursor": first.json()["next_cursor"]}
    )
    assert len(first.json()["items"]) == 2 and len(second.json()["items"]) == 1
    assert {k["id"] for k in first.json()["items"]}.isdisjoint(
        {k["id"] for k in second.json()["items"]}
    )
    key_id = second.json()["items"][0]["id"]
    assert (
        await client.post(
            base + "/" + key_id + "/rotate",
            headers=headers(root, version=1, idem="too-long"),
            json={"grace_seconds": 3601},
        )
    ).status_code == 422
    assert (await client.get("/openapi.json")).status_code == 200


async def test_rotation_cannot_cross_projects_or_extend_old_expiry(http, admin):
    client, root, _, _ = http
    org = await create_org(client, root, "Rotation scope")
    first = await project(client, root, org, "First")
    second = await project(client, root, org, "Second")
    sa1 = await account(client, root, org, first["id"])
    sa2 = await account(client, root, org, second["id"])
    expires = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()
    original = (await key(client, root, org, sa1["id"], expires_at=expires)).json()
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            "INSERT INTO identity.api_keys (id,organization_id,project_id,service_account_id,"
            "name,digest,prefix,rotated_from_id) "
            "VALUES (%s,%s,%s,%s,'wrong','wrong-digest','nxk_bad',%s)",
            (uuid4(), org["id"], second["id"], sa2["id"], original["key"]["id"]),
        )
    base = f"/api/v1/organizations/{org['id']}/api-keys/{original['key']['id']}"
    rotation = await client.post(
        base + "/rotate",
        headers=headers(root, version=1, idem="bounded-overlap"),
        json={"grace_seconds": 300},
    )
    assert rotation.status_code == 201, rotation.text
    old = (await client.get(base, headers=auth(root))).json()
    assert old["expires_at"] == original["key"]["expires_at"]
    assert rotation.json()["key"]["expires_at"] == original["key"]["expires_at"]
