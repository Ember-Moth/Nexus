import asyncio
import hashlib
import json
from uuid import uuid4

import httpx
import pytest

from nexus.api import create_app

pytestmark = pytest.mark.integration
PASSWORD = "integration-password-with-entropy"


def auth(token, version=None):
    headers = {"Authorization": f"Bearer {token}"}
    if version is not None:
        headers["If-Match"] = f'"{version}"'
    return headers


async def login(client, login_name="root-admin", password=PASSWORD):
    response = await client.post(
        "/api/v1/auth/sessions", json={"login": login_name, "password": password}
    )
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()["access_token"]


async def create_user(client, root_token, name, activate=True):
    response = await client.post("/api/v1/users", headers=auth(root_token), json={"login": name})
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["user"]["status"] == "pending"
    if activate:
        response = await client.post(
            "/api/v1/auth/password", json={"token": result["setup_token"], "password": PASSWORD}
        )
        assert response.status_code == 204, response.text
    return result


async def create_org(client, root_token, name, owner_id=None):
    payload = {"name": name}
    if owner_id:
        payload["owner_user_id"] = owner_id
    response = await client.post("/api/v1/organizations", headers=auth(root_token), json=payload)
    assert response.status_code == 201, response.text
    assert response.headers["etag"] == '"1"'
    assert response.json()["status"] == "provisioning"
    return response.json()


async def test_full_admin_organization_member_flow(http, admin):
    client, root, _, _ = http
    owner = await create_user(client, root, "enterprise-owner")
    developer = await create_user(client, root, "enterprise-developer")
    org = await create_org(client, root, "Acme", owner["user"]["id"])
    owner_token = await login(client, "enterprise-owner")
    me = await client.get("/api/v1/me", headers=auth(owner_token))
    assert me.status_code == 200
    assert me.json()["platform_roles"] == []
    assert me.json()["memberships"][0]["role"] == "org_owner"
    membership = await client.post(
        f"/api/v1/organizations/{org['id']}/members",
        headers=auth(owner_token),
        json={"user_id": developer["user"]["id"], "role": "org_developer"},
    )
    assert membership.status_code == 201, membership.text
    developer_token = await login(client, "enterprise-developer")
    assert (
        await client.get(f"/api/v1/organizations/{org['id']}", headers=auth(developer_token))
    ).status_code == 200
    assert (await client.get("/api/v1/users", headers=auth(developer_token))).status_code == 403
    assert (
        await client.get(
            f"/api/v1/organizations/{org['id']}/members", headers=auth(developer_token)
        )
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/organizations", headers=auth(owner_token), json={"name": "unauthorized"}
        )
    ).status_code == 403
    event = admin.execute("SELECT topic, payload FROM identity.outbox").fetchone()
    assert event[0] == "identity.organization_created"
    assert event[1]["organization_id"] == org["id"]
    assert admin.execute("SELECT count(*) FROM identity.organizations").fetchone()[0] == 1
    assert admin.execute("SELECT to_regclass('billing.billing_accounts')").fetchone()[0] is None
    assert "password_hash" not in me.text and "token_digest" not in me.text
    stored = admin.execute("SELECT token_digest FROM identity.admin_sessions").fetchall()
    assert (hashlib.sha256(owner_token.encode()).hexdigest(),) in stored
    assert (owner_token,) not in stored
    evidence = json.dumps(admin.execute("SELECT details FROM identity.audit_events").fetchall())
    assert PASSWORD not in evidence and owner["setup_token"] not in evidence
    assert owner_token not in evidence


async def test_tenant_scoping_and_mass_assignment(http, admin):
    client, root, _, _ = http
    owner_a = await create_user(client, root, "owner-a")
    owner_b = await create_user(client, root, "owner-b")
    a = await create_org(client, root, "Tenant A", owner_a["user"]["id"])
    b = await create_org(client, root, "Tenant B", owner_b["user"]["id"])
    a_token = await login(client, "owner-a")
    listing = await client.get("/api/v1/organizations", headers=auth(a_token))
    assert [item["id"] for item in listing.json()["items"]] == [a["id"]]
    b_members = await client.get(f"/api/v1/organizations/{b['id']}/members", headers=auth(root))
    foreign = b_members.json()["items"][0]["id"]
    for path in [
        f"/api/v1/organizations/{b['id']}",
        f"/api/v1/organizations/{b['id']}/members",
        f"/api/v1/organizations/{a['id']}/members/{foreign}",
    ]:
        response = await client.get(path, headers=auth(a_token))
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "not_found"
    response = await client.patch(
        f"/api/v1/organizations/{a['id']}/members/{foreign}",
        headers=auth(a_token, 1),
        json={"role": "org_owner"},
    )
    assert response.status_code == 404
    response = await client.patch(
        f"/api/v1/organizations/{b['id']}", headers=auth(a_token, 1), json={"name": "stolen"}
    )
    assert response.status_code == 404
    response = await client.post(
        f"/api/v1/organizations/{b['id']}/members",
        headers=auth(a_token),
        json={"user_id": owner_a["user"]["id"], "role": "org_owner"},
    )
    assert response.status_code == 404
    response = await client.post(
        f"/api/v1/users/{owner_b['user']['id']}/password-reset-tokens", headers=auth(a_token)
    )
    assert response.status_code == 403
    for payload in [
        {"user_id": owner_b["user"]["id"], "role": "platform_admin"},
        {"user_id": owner_b["user"]["id"], "role": "org_owner", "organization_id": b["id"]},
    ]:
        response = await client.post(
            f"/api/v1/organizations/{a['id']}/members", headers=auth(a_token), json=payload
        )
        assert response.status_code == 422
    assert admin.execute("SELECT count(*) FROM identity.memberships").fetchone()[0] == 2
    assert (
        admin.execute(
            "SELECT count(*) FROM identity.audit_events WHERE action = 'access.denied'"
        ).fetchone()[0]
        >= 5
    )


async def test_logout_expiration_and_disabled_user(http, admin):
    client, root, _, _ = http
    assert (
        await client.delete("/api/v1/auth/sessions/current", headers=auth(root))
    ).status_code == 204
    assert (
        await client.delete("/api/v1/auth/sessions/current", headers=auth(root))
    ).status_code == 204
    assert (await client.get("/api/v1/me", headers=auth(root))).status_code == 401
    root = await login(client)
    admin.execute(
        "UPDATE identity.admin_sessions SET created_at = now() - interval '2 days', "
        "expires_at = now() - interval '1 day'"
    )
    assert (await client.get("/api/v1/me", headers=auth(root))).status_code == 401
    root = await login(client)
    admin.execute("UPDATE identity.users SET status='disabled' WHERE login='root-admin'")
    assert (await client.get("/api/v1/me", headers=auth(root))).status_code == 401
    for name, password in [
        ("root-admin", PASSWORD),
        ("unknown-user", PASSWORD),
        ("root-admin", "wrong"),
    ]:
        response = await client.post(
            "/api/v1/auth/sessions", json={"login": name, "password": password}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
    assert (await client.get("/api/v1/me", params={"access_token": root})).status_code == 401
    assert (await client.get("/api/v1/me", headers=auth("sk_" + "a" * 43))).status_code == 401
    assert (
        await client.get(
            "/api/v1/me",
            headers=[("Authorization", f"Bearer {root}"), ("Authorization", f"Bearer {root}")],
        )
    ).status_code == 401


async def test_password_setup_is_single_use_and_reset_revokes_sessions(http, admin):
    client, root, _, _ = http
    user = await create_user(client, root, "setup-user", activate=False)
    pending = await client.post(
        "/api/v1/auth/sessions", json={"login": "setup-user", "password": PASSWORD}
    )
    assert pending.status_code == 401
    fresh = await client.post(
        f"/api/v1/users/{user['user']['id']}/password-reset-tokens", headers=auth(root)
    )
    assert fresh.status_code == 201
    assert (
        await client.post(
            "/api/v1/auth/password", json={"token": user["setup_token"], "password": PASSWORD}
        )
    ).status_code == 401
    token = fresh.json()["setup_token"]
    assert (
        await client.post("/api/v1/auth/password", json={"token": token, "password": PASSWORD})
    ).status_code == 204
    assert (
        await client.post("/api/v1/auth/password", json={"token": token, "password": PASSWORD})
    ).status_code == 401
    session = await login(client, "setup-user")
    reset = await client.post(
        f"/api/v1/users/{user['user']['id']}/password-reset-tokens", headers=auth(root)
    )
    new_password = "new-integration-password-long-enough"
    assert (
        await client.post(
            "/api/v1/auth/password",
            json={"token": reset.json()["setup_token"], "password": new_password},
        )
    ).status_code == 204
    assert (await client.get("/api/v1/me", headers=auth(session))).status_code == 401
    assert (
        await client.post(
            "/api/v1/auth/sessions", json={"login": "setup-user", "password": PASSWORD}
        )
    ).status_code == 401
    await login(client, "setup-user", new_password)
    expired = await client.post(
        f"/api/v1/users/{user['user']['id']}/password-reset-tokens", headers=auth(root)
    )
    admin.execute(
        "UPDATE identity.password_setup_tokens SET created_at=now()-interval '2 days', "
        "expires_at=now()-interval '1 day'"
    )
    assert (
        await client.post(
            "/api/v1/auth/password",
            json={"token": expired.json()["setup_token"], "password": PASSWORD},
        )
    ).status_code == 401


async def test_last_owner_is_preserved_under_concurrent_updates(http, admin):
    client, root, _, _ = http
    user = await create_user(client, root, "other-owner")
    org = await create_org(client, root, "Concurrent Owners")
    path = f"/api/v1/organizations/{org['id']}/members"
    response = await client.post(
        path, headers=auth(root), json={"user_id": user["user"]["id"], "role": "org_owner"}
    )
    assert response.status_code == 201
    members = (await client.get(path, headers=auth(root))).json()["items"]
    results = await asyncio.gather(
        *(
            client.patch(
                f"{path}/{member['id']}",
                headers=auth(root, member["version"]),
                json={"status": "disabled"},
            )
            for member in members
        )
    )
    assert sorted(result.status_code for result in results) == [200, 409]
    assert (
        admin.execute(
            "SELECT count(*) FROM identity.memberships WHERE role='org_owner' AND status='active'"
        ).fetchone()[0]
        == 1
    )
    active = next(
        m
        for m in (await client.get(path, headers=auth(root))).json()["items"]
        if m["status"] == "active"
    )
    assert (
        await client.delete(f"{path}/{active['id']}", headers=auth(root, active["version"]))
    ).status_code == 409
    assert (
        await client.patch(
            f"{path}/{active['id']}", headers=auth(root), json={"role": "org_developer"}
        )
    ).status_code == 428
    assert (
        await client.patch(
            f"{path}/{active['id']}", headers=auth(root, 999), json={"role": "org_developer"}
        )
    ).status_code == 412
    duplicate = await client.post(
        path, headers=auth(root), json={"user_id": active["user_id"], "role": "org_developer"}
    )
    assert duplicate.status_code == 409


async def test_live_membership_permissions_and_organization_lifecycle(http):
    client, root, _, _ = http
    user = await create_user(client, root, "changing-owner")
    org = await create_org(client, root, "Lifecycle")
    base = f"/api/v1/organizations/{org['id']}"
    member = (
        await client.post(
            base + "/members",
            headers=auth(root),
            json={"user_id": user["user"]["id"], "role": "org_owner"},
        )
    ).json()
    token = await login(client, "changing-owner")
    assert (await client.get(base + "/members", headers=auth(token))).status_code == 200
    assert (
        await client.patch(base, headers=auth(token, 1), json={"status": "active"})
    ).status_code == 403
    assert (
        await client.patch(base, headers=auth(root, 1), json={"status": "active"})
    ).status_code == 409
    suspended = await client.patch(base, headers=auth(root, 1), json={"status": "suspended"})
    assert suspended.status_code == 200
    assert (await client.get(base + "/members", headers=auth(token))).status_code == 403
    assert (await client.get(base, headers=auth(token))).json()["status"] == "suspended"
    resumed = await client.patch(base, headers=auth(root, 2), json={"status": "provisioning"})
    assert resumed.status_code == 200
    assert (await client.get(base + "/members", headers=auth(token))).status_code == 200
    changed = await client.patch(
        base + f"/members/{member['id']}", headers=auth(root, 1), json={"role": "org_developer"}
    )
    assert changed.status_code == 200
    assert (await client.get(base + "/members", headers=auth(token))).status_code == 403
    assert (
        await client.delete(base + f"/members/{member['id']}", headers=auth(root, 2))
    ).status_code == 204
    assert (await client.get(base, headers=auth(token))).status_code == 404
    assert (await client.get("/api/v1/organizations", headers=auth(token))).json()["items"] == []
    archived = await client.patch(base, headers=auth(root, 3), json={"status": "archived"})
    assert archived.status_code == 200
    assert (
        await client.patch(base, headers=auth(root, 4), json={"name": "reopen"})
    ).status_code == 409


async def test_pagination_validation_and_secret_redaction(http):
    client, root, _, _ = http
    for index in range(3):
        await create_user(client, root, f"page-user-{index}", activate=False)
    first = await client.get("/api/v1/users", headers=auth(root), params={"limit": 2})
    second = await client.get(
        "/api/v1/users",
        headers=auth(root),
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
    )
    one, two = first.json()["items"], second.json()["items"]
    assert len(one) == len(two) == 2
    assert {x["id"] for x in one}.isdisjoint({x["id"] for x in two})
    assert "password_hash" not in first.text
    for query in [{"limit": 0}, {"limit": 101}, {"cursor": "invalid"}]:
        assert (
            await client.get("/api/v1/users", headers=auth(root), params=query)
        ).status_code == 422
    secret = "should-not-appear-in-validation-output"
    response = await client.post(
        "/api/v1/auth/sessions", json={"login": "root-admin", "password": {"value": secret}}
    )
    assert response.status_code == 422
    assert secret not in response.text and "input" not in response.json()["error"]
    assert response.json()["error"]["trace_id"] == response.headers["x-request-id"]
    assert response.headers["cache-control"] == "no-store"
    duplicate = await client.post(
        "/api/v1/users", headers=auth(root), json={"login": "page-user-0"}
    )
    assert duplicate.status_code == 409
    schema = (await client.get("/openapi.json")).json()
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    assert "/api/v1/organizations/{organization_id}/members/{member_id}" in schema["paths"]


async def test_finance_role_is_not_a_platform_administrator(http, admin):
    client, root, _, _ = http
    user = await create_user(client, root, "staff-finance")
    admin.execute(
        "INSERT INTO identity.platform_role_bindings(user_id,role) VALUES (%s,'platform_finance')",
        (user["user"]["id"],),
    )
    await create_org(client, root, "Finance-visible")
    token = await login(client, "staff-finance")
    assert (
        len((await client.get("/api/v1/organizations", headers=auth(token))).json()["items"]) == 1
    )
    assert (
        await client.post("/api/v1/organizations", headers=auth(token), json={"name": "forbidden"})
    ).status_code == 403
    assert (
        await client.post("/api/v1/users", headers=auth(token), json={"login": "forbidden"})
    ).status_code == 403
    assert (await client.get("/api/v1/users", headers=auth(token))).status_code == 403


async def test_authentication_rate_limits_and_fail_closed(http):
    _, _, _, settings = http
    settings = settings.model_copy(
        update={
            "redis_namespace": "nexus:rate:" + uuid4().hex,
            "login_per_account": 2,
            "login_per_ip": 3,
        }
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        await login(client)
        response = await client.post(
            "/api/v1/auth/sessions", json={"login": "root-admin", "password": "bad"}
        )
        assert response.status_code == 401
        assert (
            await client.post(
                "/api/v1/auth/sessions", json={"login": "root-admin", "password": PASSWORD}
            )
        ).status_code == 429
        assert (
            await client.post(
                "/api/v1/auth/sessions", json={"login": "other-login", "password": "bad"}
            )
        ).status_code == 401
        assert (
            await client.post(
                "/api/v1/auth/sessions",
                headers={"X-Forwarded-For": "192.168.55.55"},
                json={"login": "third-login", "password": "bad"},
            )
        ).status_code == 429

        class Unavailable:
            async def allow(self, subject, peer):
                raise RuntimeError("private dependency detail")

        app.state.identity_service._auth_limits = Unavailable()
        response = await client.post(
            "/api/v1/auth/sessions", json={"login": "root-admin", "password": PASSWORD}
        )
        assert response.status_code == 503
        assert "private dependency detail" not in response.text


async def test_concurrent_password_redemption_and_live_platform_role(http, admin):
    client, root, _, _ = http
    user = await create_user(client, root, "single-use-race", activate=False)
    results = await asyncio.gather(
        *(
            client.post(
                "/api/v1/auth/password", json={"token": user["setup_token"], "password": PASSWORD}
            )
            for _ in range(2)
        )
    )
    assert sorted(response.status_code for response in results) == [204, 401]
    assert (
        admin.execute(
            "SELECT count(*) FROM identity.audit_events "
            "WHERE action='user.password_set' AND resource_id=%s",
            (user["user"]["id"],),
        ).fetchone()[0]
        == 1
    )
    await login(client, "single-use-race")
    admin.execute("DELETE FROM identity.platform_role_bindings WHERE role='platform_admin'")
    assert (await client.get("/api/v1/users", headers=auth(root))).status_code == 403
    assert (await client.get("/api/v1/me", headers=auth(root))).json()["platform_roles"] == []
