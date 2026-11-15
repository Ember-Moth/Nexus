import asyncio
import hashlib
from uuid import uuid4

import psycopg
import pytest
from test_identity_api import PASSWORD, auth

pytestmark = pytest.mark.integration
API = "/api/v1"


async def issue(client, owner, name, role):
    base = f"{API}/organizations/{owner['personal_organization_id']}/invitations"
    response = await client.post(
        base,
        headers=auth(owner["access_token"]),
        json={
            "login": name,
            "role": role,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def accept(client, session, invitation):
    return await client.post(
        API + "/invitations/accept",
        headers=auth(session["access_token"]),
        json={"token": invitation["token"]},
    )


async def test_registration_creates_one_owned_tenant_and_one_account(http, admin, delivery, signup):
    client, root, app, _ = http
    messages, mail = delivery[3], delivery[2]
    # Both spellings start a registration; the login is decided when a mailbox proves itself.
    for name, address in [
        ("New-Customer", "first@example.com"),
        ("new-customer", "second@example.com"),
    ]:
        started = await client.post(
            API + "/auth/registrations",
            json={"login": name, "password": PASSWORD, "email": address},
        )
        assert started.status_code == 202
    assert await mail.dispatch_email() == 1 and await mail.dispatch_email() == 1
    attempts = await asyncio.gather(
        *(
            client.post(
                API + "/auth/registrations/verify",
                json={"token": message.token, "password": PASSWORD},
            )
            for message in messages
        )
    )
    assert sorted(response.status_code for response in attempts) == [201, 409]
    assert (
        next(r.json()["error"]["code"] for r in attempts if r.status_code == 409) == "login_in_use"
    )
    created = next(response.json() for response in attempts if response.status_code == 201)
    session, org = created["access_token"], created["personal_organization_id"]
    me = (await client.get(API + "/me", headers=auth(session))).json()
    user = me["user"]["id"]
    assert me["user"]["login"] == "new-customer" and me["user"]["status"] == "active"
    assert me["platform_roles"] == [] and me["personal_organization_id"] == org
    assert [(m["organization_id"], m["role"]) for m in me["memberships"]] == [(org, "org_owner")]
    path = f"{API}/organizations/{org}"
    record = (await client.get(path, headers=auth(session))).json()
    assert (record["name"], record["personal_owner_id"]) == ("Personal's Organisation", user)
    renamed = await client.patch(
        path, json={"name": "My team"}, headers=auth(session, record["version"])
    )
    assert renamed.status_code == 200 and renamed.json()["name"] == "My team"
    archived = await client.patch(
        path, json={"status": "archived"}, headers=auth(root, renamed.json()["version"])
    )
    assert (
        archived.status_code == 409
        and archived.json()["error"]["code"] == "personal_organization_required"
    )
    for statement in [
        "DELETE FROM identity.organizations WHERE id=%s",
        "UPDATE identity.organizations SET personal_owner_id=NULL WHERE id=%s",
        "DELETE FROM identity.memberships WHERE organization_id=%s",
    ]:
        with pytest.raises(psycopg.errors.RaiseException):
            admin.execute(statement, (org,))
    await app.state.process_billing()
    await app.state.process_billing()
    assert admin.execute(
        "SELECT o.status,o.name,a.balance FROM identity.organizations o "
        "JOIN billing.accounts a ON a.id=o.billing_account_id AND a.organization_id=o.id "
        "WHERE o.personal_owner_id=%s",
        (user,),
    ).fetchall() == [("active", "My team", 0)]
    denied = await client.post(
        API + "/auth/registrations",
        json={
            "login": "extra-field",
            "password": PASSWORD,
            "roles": ["platform_admin"],
        },
    )
    assert denied.status_code == 422


async def test_invitation_join_role_change_and_owner_binding(http, admin, signup):
    client = http[0]
    owner = await signup("solo-owner")
    invitation = await issue(client, owner, "Guest-User", "org_owner")
    other = await signup("guest-user")
    assert (await accept(client, owner, invitation)).status_code == 404
    joined = await asyncio.gather(
        accept(client, other, invitation), accept(client, other, invitation)
    )
    assert [r.status_code for r in joined] == [200, 200]
    member = joined[0].json()
    assert member == joined[1].json() and member["role"] == "org_owner"
    assert admin.execute(
        "SELECT count(*) FROM identity.audit_events WHERE action='invitation.accepted' "
        "AND organization_id=%s",
        (owner["personal_organization_id"],),
    ).fetchone() == (1,)
    assert (
        await client.post(
            f"{API}/organizations/{owner['personal_organization_id']}/invitations",
            headers=auth(owner["access_token"]),
            json={"login": "guest-user", "role": "org_developer"},
        )
    ).status_code == 409
    guest_me = (await client.get(API + "/me", headers=auth(other["access_token"]))).json()
    assert {m["organization_id"] for m in guest_me["memberships"]} == {
        owner["personal_organization_id"],
        other["personal_organization_id"],
    }
    path = f"{API}/organizations/{owner['personal_organization_id']}/members"
    rows = (await client.get(path, headers=auth(other["access_token"]))).json()["items"]
    bound = next(m for m in rows if m["login"] == "solo-owner")
    for method, body in [("PATCH", {"role": "org_finance"}), ("DELETE", None)]:
        rejected = await client.request(
            method,
            f"{path}/{bound['id']}",
            headers=auth(other["access_token"], bound["version"]),
            json=body,
        )
        assert (
            rejected.status_code == 409
            and rejected.json()["error"]["code"] == "personal_owner_required"
        )
    target = f"{path}/{member['id']}"
    changed = await client.patch(
        target, headers=auth(owner["access_token"], member["version"]), json={"role": "org_finance"}
    )
    assert changed.status_code == 200
    assert (await accept(client, other, invitation)).json()["role"] == "org_finance"
    assert (await client.get(path, headers=auth(other["access_token"]))).status_code == 403
    removed = await client.delete(
        target, headers=auth(owner["access_token"], changed.json()["version"])
    )
    assert removed.status_code == 204
    assert (await accept(client, other, invitation)).status_code == 409
    fresh = await issue(client, owner, "guest-user", "org_developer")
    assert (await accept(client, other, fresh)).json()["role"] == "org_developer"


async def test_invitation_rotation_expiry_revocation_and_scope(http, admin, signup):
    client = http[0]
    owner, guest = await signup("inviter"), await signup("invited")
    path = f"{API}/organizations/{owner['personal_organization_id']}/invitations"
    stale = await issue(client, owner, "invited", "org_owner")
    current = await issue(client, owner, "invited", "org_developer")
    assert (await accept(client, guest, stale)).status_code == 404
    listing = await client.get(path, headers=auth(owner["access_token"]))
    assert [r["id"] for r in listing.json()["items"]] == [current["id"]]
    assert "token" not in listing.text
    assert admin.execute(
        "SELECT token_digest FROM identity.organization_invitations WHERE id=%s",
        (current["id"],),
    ).fetchone() == (hashlib.sha256(current["token"].encode()).hexdigest(),)
    assert (await client.get(path, headers=auth(guest["access_token"]))).status_code == 404
    wrong_org = f"{API}/organizations/{guest['personal_organization_id']}/invitations"
    assert (
        await client.delete(wrong_org + "/" + current["id"], headers=auth(guest["access_token"]))
    ).status_code == 404
    assert (
        await client.delete(path + "/" + current["id"], headers=auth(owner["access_token"]))
    ).status_code == 204
    assert (await accept(client, guest, current)).status_code == 404
    expired = await issue(client, owner, "invited", "org_finance")
    admin.execute(
        "UPDATE identity.organization_invitations SET created_at=now()-interval '8 days', "
        "expires_at=now()-interval '1 day' WHERE id=%s",
        (expired["id"],),
    )
    assert (await accept(client, guest, expired)).status_code == 410
    assert (await client.get(path, headers=auth(owner["access_token"]))).json()["items"] == []


async def test_direct_membership_changes_invalidate_pending_invites(http, signup):
    client = http[0]
    owner, guest = await signup("direct-owner"), await signup("direct-guest")
    pending = await issue(client, owner, "direct-guest", "org_owner")
    me = (await client.get(API + "/me", headers=auth(guest["access_token"]))).json()
    path = f"{API}/organizations/{owner['personal_organization_id']}/members"
    direct = await client.post(
        path,
        headers=auth(owner["access_token"]),
        json={"user_id": me["user"]["id"], "role": "org_developer"},
    )
    assert direct.status_code == 201
    member = direct.json()
    assert (
        await client.delete(
            f"{path}/{member['id']}", headers=auth(owner["access_token"], member["version"])
        )
    ).status_code == 204
    assert (await accept(client, guest, pending)).status_code == 404
    renewed = await issue(client, owner, "direct-guest", "org_owner")
    current = (
        await client.get(f"{path}/{member['id']}", headers=auth(owner["access_token"]))
    ).json()
    updated = await client.patch(
        f"{path}/{member['id']}",
        headers=auth(owner["access_token"], current["version"]),
        json={"role": "org_finance"},
    )
    assert updated.status_code == 200
    assert (await accept(client, guest, renewed)).status_code == 404


def test_personal_organization_migration_backfills_once_and_preserves_ids(
    migration_settings, admin
):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["settings"] = migration_settings
    user = uuid4()
    command.downgrade(config, "0014_bedrock_converse")
    try:
        admin.execute(
            "INSERT INTO identity.users(id, login, password_hash) "
            "VALUES(%s,'legacy-account','hash')",
            (user,),
        )
    finally:
        command.upgrade(config, "head")
    query = (
        "SELECT o.id,m.id,e.id FROM identity.organizations o "
        "JOIN identity.memberships m ON m.organization_id=o.id AND m.user_id=o.personal_owner_id "
        "JOIN identity.outbox e ON e.aggregate_id=o.id WHERE o.personal_owner_id=%s"
    )
    original = admin.execute(query, (user,)).fetchall()
    assert len(original) == 1
    admin.execute("UPDATE identity.organizations SET name='Renamed' WHERE id=%s", (original[0][0],))
    try:
        command.downgrade(config, "0014_bedrock_converse")
    finally:
        command.upgrade(config, "head")
    assert admin.execute(query, (user,)).fetchall() == original
    assert admin.execute(
        "SELECT name FROM identity.organizations WHERE personal_owner_id=%s", (user,)
    ).fetchone() == ("Renamed",)
