"""The built-in administrator, the locked API before setup, the wizard and ``nexus setup reset``."""

import asyncio
import json
import os
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from mail_stub import PASSWORD, install_mail
from setup_wizard import ADMIN_EMAIL, API, SITE, sign_in
from test_identity_api import auth, create_org, create_user, login

from nexus.api import SETUP_OPEN_ROUTES, create_app, setup_open
from nexus.commands import import_policies
from nexus.modules.identity.domain.setup import (
    BUILTIN_ADMIN_ID,
    DEFAULT_ADMIN_EMAIL,
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_PASSWORD_HASH,
)
from nexus.shared.policy import PolicyRejected

pytestmark = pytest.mark.integration
NEW_PASSWORD = "a-fresh-administrator-password"


@pytest.fixture
async def fresh(runtime_settings):
    """An API process on a database where setup has not been completed."""
    settings = runtime_settings.model_copy(
        update={"redis_namespace": "nexus:setup-test:" + uuid4().hex}
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client,
    ):
        yield client, app, settings


def code(response):
    try:
        return response.json()["error"]["code"]
    except ValueError, KeyError, TypeError:
        return None


def completion(**changes):
    return {"site": SITE, "email": ADMIN_EMAIL, "password": PASSWORD, **changes}


def concrete(template):
    """A request path for a route template: every parameter gets a well-formed value."""
    parts = []
    for part in template.split("/"):
        if part.startswith("{"):
            part = "x" if ":path}" in part else str(uuid4())
        parts.append(part)
    return "/".join(parts)


def stored_admin(admin):
    return admin.execute(
        "SELECT login,password_hash,status,verified_email,unverified_email "
        "FROM identity.users WHERE id=%s",
        (BUILTIN_ADMIN_ID,),
    ).fetchone()


async def test_reinstall_state_signs_in_with_the_default_account(fresh, admin):
    client, _, _ = fresh
    assert stored_admin(admin) == (
        "admin",
        DEFAULT_ADMIN_PASSWORD_HASH,
        "active",
        None,
        DEFAULT_ADMIN_EMAIL,
    )
    assert admin.execute(
        "SELECT role FROM identity.platform_role_bindings WHERE user_id=%s", (BUILTIN_ADMIN_ID,)
    ).fetchall() == [("platform_admin",)]
    # The default address, any spelling of it, and the login name all sign in.
    for login_name in (DEFAULT_ADMIN_EMAIL, "Admin@Example.COM", "admin"):
        token = await sign_in(client, login_name)
    me = (await client.get(API + "/me", headers=auth(token))).json()
    assert me["user"]["id"] == str(BUILTIN_ADMIN_ID) and me["platform_roles"] == ["platform_admin"]
    assert me["verified_email"] is None and me["unverified_email"] == DEFAULT_ADMIN_EMAIL
    status = await client.get(API + "/setup", headers=auth(token))
    assert status.status_code == 200, status.text
    assert status.json() == {
        "required": True,
        "builtin_admin": True,
        "email_delivery_available": False,
        "site": {
            "site_name": "Nexus",
            "console_url": None,
            "contact_email": None,
            "setup_completed_at": None,
            "managed": None,
            "version": 1,
        },
    }
    assert (await client.get(API + "/setup")).status_code == 401


async def test_locked_api_answers_only_sign_in_and_the_wizard(fresh, admin):
    client, app, _ = fresh
    token = await sign_in(client)
    # Self-service entry points are closed as well, even with registration switched on.
    admin.execute(
        "UPDATE identity.auth_policy SET registration=true,email_verification=true,"
        "version=version+1"
    )
    assert (await client.get(API + "/auth/options")).json()["registration"] is False
    walked = set()
    for context in iter_route_contexts(app.routes):
        if not isinstance(context.original_route, APIRoute):
            continue
        for method in sorted(context.methods - {"HEAD", "OPTIONS"}):
            walked.add((method, context.path))
            if setup_open(method, context.path):
                continue
            response = await client.request(
                method,
                concrete(context.path),
                headers=auth(token),
                json={} if method in {"POST", "PUT", "PATCH"} else None,
            )
            assert response.status_code == 409, (method, context.path, response.text)
            assert code(response) == "setup_required", (method, context.path)
            assert response.headers["cache-control"] == "no-store"
    # Every listed route exists, and the walk covered the whole management API.
    assert SETUP_OPEN_ROUTES <= walked
    assert len(walked) > 150
    for method, path in [
        ("POST", "/auth/registrations"),
        ("POST", "/auth/password/forgot"),
        ("POST", "/auth/password/reset"),
        ("POST", "/auth/password"),
        ("POST", "/invitations/accept"),
    ]:
        response = await client.request(method, API + path, json={})
        assert code(response) == "setup_required", path
    # Unauthenticated callers learn nothing more than signed-in ones.
    assert code(await client.get(API + "/organizations")) == "setup_required"
    # The open routes work.
    assert (await client.get(API + "/error-codes")).json()["setup_required"] == [
        {"status": 409, "action": "complete_setup", "retry": "after_correction"}
    ]
    assert (await client.get("/health/live")).status_code == 200
    assert (await client.get(API + "/me", headers=auth(token))).status_code == 200
    assert (
        await client.delete(API + "/auth/sessions/current", headers=auth(token))
    ).status_code == 204
    assert (await client.get(API + "/me", headers=auth(token))).status_code == 401
    # Nothing ran behind the lock.
    assert admin.execute("SELECT count(*) FROM identity.organizations").fetchone() == (1,)


async def test_wizard_without_mail_replaces_the_default_credentials(fresh, admin):
    client, _, _ = fresh
    other = await sign_in(client)
    token = await sign_in(client)
    for body, status, error in [
        (completion(email=DEFAULT_ADMIN_EMAIL), 422, "email_reserved"),
        (completion(email="ops@sub.example.net"), 422, "email_reserved"),
        (completion(email="not-an-address"), 422, "invalid_email"),
        (completion(password="short"), 422, "validation_error"),
        (completion(site={**SITE, "site_name": "  "}), 422, "validation_error"),
        (completion(site={**SITE, "console_url": "http://console.corp"}), 422, "validation_error"),
        (completion(site={**SITE, "contact_email": "nobody"}), 422, "validation_error"),
    ]:
        response = await client.post(API + "/setup", headers=auth(token), json=body)
        assert (response.status_code, code(response)) == (status, error), body
    # Without mail, the email step needs no token and the address is stored unverified.
    assert (
        await client.post(
            API + "/setup/email-verification", headers=auth(token), json={"email": ADMIN_EMAIL}
        )
    ).status_code == 503
    done = await client.post(API + "/setup", headers=auth(token), json=completion())
    assert done.status_code == 200, done.text
    result = done.json()
    assert result["required"] is False and result["site"]["version"] == 2
    assert result["site"]["setup_completed_at"] is not None
    assert {k: result["site"][k] for k in SITE} == SITE
    login_name, password_hash, status, verified, unverified = stored_admin(admin)
    assert (login_name, status, verified, unverified) == ("admin", "active", None, ADMIN_EMAIL)
    assert password_hash != DEFAULT_ADMIN_PASSWORD_HASH
    # The completing session stays; every other one, and the default password, are gone.
    assert (await client.get(API + "/organizations", headers=auth(token))).status_code == 200
    assert (await client.get(API + "/me", headers=auth(other))).status_code == 401
    for login_name in (DEFAULT_ADMIN_EMAIL, "admin"):
        refused = await client.post(
            API + "/auth/sessions",
            json={"login": login_name, "password": DEFAULT_ADMIN_PASSWORD},
        )
        assert refused.status_code == 401
    me = (await client.get(API + "/me", headers=auth(await login(client, ADMIN_EMAIL)))).json()
    assert me["unverified_email"] == ADMIN_EMAIL and me["verified_email"] is None
    await login(client, "admin")
    # Submitting again, or asking for another mail, is a conflict.
    again = await client.post(API + "/setup", headers=auth(token), json=completion())
    assert (again.status_code, code(again)) == (409, "setup_completed")
    status = (await client.get(API + "/setup", headers=auth(token))).json()
    assert status["required"] is False
    (audit,) = admin.execute(
        "SELECT actor_id,details FROM identity.audit_events WHERE action='setup.completed'"
    ).fetchall()
    assert audit[0] == BUILTIN_ADMIN_ID
    assert audit[1] == {
        "email_verified": False,
        "site": {
            "console_url": [None, SITE["console_url"]],
            "contact_email": [None, SITE["contact_email"]],
        },
    }


async def test_concurrent_submissions_complete_setup_once(fresh, admin):
    client, _, _ = fresh
    # One session submitting twice: the second answer is a conflict.
    token = await sign_in(client)
    results = await asyncio.gather(
        *(
            client.post(API + "/setup", headers=auth(token), json=completion(password=password))
            for password in ("first-password-with-entropy", "second-password-with-entropy")
        )
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    assert code(next(r for r in results if r.status_code == 409)) == "setup_completed"
    winner = next(i for i, r in enumerate(results) if r.status_code == 200)
    passwords = ["first-password-with-entropy", "second-password-with-entropy"]
    await login(client, "admin", passwords[winner])
    loser = await client.post(
        API + "/auth/sessions", json={"login": "admin", "password": passwords[1 - winner]}
    )
    assert loser.status_code == 401
    assert admin.execute(
        "SELECT count(*) FROM identity.audit_events WHERE action='setup.completed'"
    ).fetchone() == (1,)


async def test_racing_sessions_leave_exactly_one_administrator(fresh, admin):
    client, _, _ = fresh
    first, second = await sign_in(client), await sign_in(client)
    results = await asyncio.gather(
        client.post(API + "/setup", headers=auth(first), json=completion(password="a" * 16)),
        client.post(API + "/setup", headers=auth(second), json=completion(password="b" * 16)),
    )
    statuses = [r.status_code for r in results]
    assert statuses.count(200) == 1, [r.text for r in results]
    loser = results[statuses.index(200) ^ 1]
    # The loser's session was revoked by the winner, or found the wizard already finished.
    assert (loser.status_code, code(loser)) in {(401, "unauthorized"), (409, "setup_completed")}
    winner = (first, second)[statuses.index(200)]
    assert (await client.get(API + "/me", headers=auth(winner))).status_code == 200
    assert admin.execute(
        "SELECT count(*) FROM identity.admin_sessions WHERE user_id=%s AND revoked_at IS NULL",
        (BUILTIN_ADMIN_ID,),
    ).fetchone() == (1,)


async def test_wizard_with_mail_requires_a_verified_address(fresh, admin):
    client, app, settings = fresh
    service = app.state.identity_service
    messages, _ = install_mail(service, settings)
    token = await sign_in(client)
    assert (await client.get(API + "/setup", headers=auth(token))).json()[
        "email_delivery_available"
    ] is True
    missing = await client.post(API + "/setup", headers=auth(token), json=completion())
    assert (missing.status_code, code(missing)) == (422, "email_verification_required")
    for address in (DEFAULT_ADMIN_EMAIL, "ops@example.org", "ops@team.example"):
        refused = await client.post(
            API + "/setup/email-verification", headers=auth(token), json={"email": address}
        )
        assert (refused.status_code, code(refused)) == (422, "email_reserved"), address

    async def mailed(address):
        sent = await client.post(
            API + "/setup/email-verification", headers=auth(token), json={"email": address}
        )
        assert sent.status_code == 202, sent.text
        assert await service.dispatch_email() == 1
        message = messages.pop()
        assert message.to == address and message.subject == "Verify your Nexus email"
        return message.token

    first = await mailed(ADMIN_EMAIL)
    other = await mailed("other-admin@corp.internal")
    # A newer request replaces the older token, and a token proves only its own address.
    for token_value, address in [
        (first, ADMIN_EMAIL),
        (other, ADMIN_EMAIL),
        ("nxb_x", ADMIN_EMAIL),
    ]:
        wrong = await client.post(
            API + "/setup",
            headers=auth(token),
            json=completion(email=address, email_token=token_value),
        )
        assert (wrong.status_code, code(wrong)) == (422, "invalid_verification_token")
    # Failed submissions wrote nothing.
    assert stored_admin(admin)[1] == DEFAULT_ADMIN_PASSWORD_HASH
    done = await client.post(
        API + "/setup",
        headers=auth(token),
        json=completion(email="other-admin@corp.internal", email_token=other),
    )
    assert done.status_code == 200, done.text
    assert stored_admin(admin)[3:] == ("other-admin@corp.internal", None)
    assert (await client.get(API + "/me", headers=auth(token))).json()[
        "verified_email"
    ] == "other-admin@corp.internal"
    await login(client, "other-admin@corp.internal")
    (audit,) = admin.execute(
        "SELECT details FROM identity.audit_events WHERE action='setup.completed'"
    ).fetchall()
    assert audit[0]["email_verified"] is True
    # Identity mail carries the site name.
    renamed = await client.put(
        API + "/settings/site",
        headers=auth(token, 2),
        json={**SITE, "site_name": "Acme AI Gateway"},
    )
    assert renamed.status_code == 200, renamed.text
    await create_user(client, token, "mail-member")
    session = await login(client, "mail-member")
    bound = await client.post(
        API + "/me/email",
        headers=auth(session),
        json={"email": "member@corp.internal", "password": PASSWORD},
    )
    assert bound.status_code == 202, bound.text
    assert await service.dispatch_email() == 1
    assert messages.pop().subject == "Verify your Acme AI Gateway email"


async def test_only_the_builtin_administrator_completes_setup(http, admin, service_directory):
    client, root, app, _ = http
    await create_user(client, root, "plain-member")
    member = await login(client, "plain-member")
    reset = await run_reset(service_directory, "--yes")
    assert reset.returncode == 0, reset.stderr
    app.state.identity_service._site.invalidate()
    status = (await client.get(API + "/setup", headers=auth(member))).json()
    assert status["required"] is True and status["builtin_admin"] is False
    refused = await client.post(API + "/setup", headers=auth(member), json=completion())
    assert (refused.status_code, code(refused)) == (403, "forbidden")
    mailed = await client.post(
        API + "/setup/email-verification", headers=auth(member), json={"email": ADMIN_EMAIL}
    )
    assert mailed.status_code in {403, 503}
    assert code(await client.get(API + "/organizations", headers=auth(member))) == (
        "setup_required"
    )


async def run_reset(service_directory, *extra):
    return await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "nexus.cli",
            "--env-file",
            str(service_directory / ".env.api"),
            "setup",
            "reset",
            *extra,
        ],
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


async def test_reset_command_restores_the_default_account_and_locks_the_api(
    http, admin, service_directory
):
    client, root, app, _ = http
    org = await create_org(client, root, "Kept through a reset")
    await create_user(client, root, "staff-member")
    staff = await login(client, "staff-member")
    second_admin = await login(client, "admin")
    # Without a terminal the command insists on --yes and changes nothing.
    refused = await run_reset(service_directory)
    assert refused.returncode == 1 and "--yes" in refused.stderr
    assert (await client.get(API + "/me", headers=auth(root))).status_code == 200
    done = await run_reset(service_directory, "--yes")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {
        "user_id": str(BUILTIN_ADMIN_ID),
        "login": "admin",
        "email": DEFAULT_ADMIN_EMAIL,
        "setup_required": True,
    }
    # Every session of the built-in administrator ends and its defaults are back.
    for session in (root, second_admin):
        assert (await client.get(API + "/me", headers=auth(session))).status_code == 401
    assert stored_admin(admin) == (
        "admin",
        DEFAULT_ADMIN_PASSWORD_HASH,
        "active",
        None,
        DEFAULT_ADMIN_EMAIL,
    )
    stale = await client.post(API + "/auth/sessions", json={"login": "admin", "password": PASSWORD})
    assert stale.status_code == 401
    # Other processes see the lock within the policy cache lifetime.
    app.state.identity_service._site.invalidate()
    assert code(await client.get(API + "/organizations", headers=auth(staff))) == ("setup_required")
    token = await sign_in(client)
    assert (await client.get(API + "/setup", headers=auth(token))).json()["required"] is True
    (detail,) = admin.execute(
        "SELECT details FROM identity.audit_events WHERE action='setup.reset'"
    ).fetchone()
    assert detail == {"actor": "setup-reset", "was_completed": True}
    # The wizard runs again; data outside the administrator account is untouched.
    done = await client.post(
        API + "/setup", headers=auth(token), json=completion(password=NEW_PASSWORD)
    )
    assert done.status_code == 200, done.text
    assert (
        await client.get(API + f"/organizations/{org['id']}", headers=auth(token))
    ).status_code == 200
    assert (await client.get(API + "/me", headers=auth(staff))).status_code == 200
    await login(client, ADMIN_EMAIL, NEW_PASSWORD)


async def test_site_policy_is_an_administrator_setting(http, admin):
    client, root, _, _ = http
    path = API + "/settings/site"
    current = await client.get(path, headers=auth(root))
    assert current.status_code == 200 and current.headers["ETag"] == '"2"'
    view = current.json()
    assert view["setup_completed_at"] is not None
    body = {k: view[k] for k in SITE}
    assert (await client.put(path, headers=auth(root), json=body)).status_code == 428
    for invalid in (
        {"console_url": "http://console.corp"},
        {"console_url": "https://console.corp/?next=x"},
        {"console_url": "https://user:secret@console.corp"},
        {"site_name": ""},
        {"site_name": "x" * 101},
        {"contact_email": "no-at-sign"},
        {"setup_completed_at": None},
    ):
        response = await client.put(path, headers=auth(root, 2), json={**body, **invalid})
        assert response.status_code == 422, invalid
    loopback = await client.put(
        path,
        headers=auth(root, 2),
        json={**body, "console_url": "http://localhost:3000/", "site_name": " Acme "},
    )
    assert loopback.status_code == 200, loopback.text
    assert loopback.json()["console_url"] == "http://localhost:3000"
    assert loopback.json()["site_name"] == "Acme"
    assert loopback.json()["setup_completed_at"] == view["setup_completed_at"]
    stale = await client.put(path, headers=auth(root, 2), json=body)
    assert (stale.status_code, code(stale)) == (412, "version_conflict")
    await create_user(client, root, "site-member")
    member = await login(client, "site-member")
    assert (await client.get(path, headers=auth(member))).status_code == 403
    (details,) = admin.execute(
        "SELECT details FROM identity.audit_events WHERE action='site_policy.updated'"
    ).fetchone()
    assert details == {
        "site_name": ["Nexus", "Acme"],
        "console_url": ["https://console.nexus.test", "http://localhost:3000"],
    }


async def test_policy_file_sets_site_details_but_never_setup_state(fresh, admin, tmp_path):
    client, _, settings = fresh

    def policy_file(name, groups):
        path = tmp_path / name
        path.write_text(json.dumps({"version": 1, "groups": groups}))
        return str(path)

    for values, field in [
        ({"setup_completed_at": "2026-01-01T00:00:00Z"}, "setup_completed_at"),
        ({"setup_completed": True}, "setup_completed"),
        ({"console_url": "ftp://console"}, "console_url"),
    ]:
        groups = {"identity.site_policy": {"mode": "managed", "values": values}}
        with pytest.raises(PolicyRejected, match=f"identity.site_policy: invalid {field}"):
            await import_policies(settings, policy_file("bad.json", groups))
    # Initial values reach a pristine policy, so the wizard starts from them.
    initial = {"site_name": "Acme AI", "console_url": "https://console.acme.test"}
    result = await import_policies(
        settings,
        policy_file(
            "initial.json", {"identity.site_policy": {"mode": "initial", "values": initial}}
        ),
    )
    assert result["groups"]["identity.site_policy"]["changes"] == {
        "site_name": ["Nexus", "Acme AI"],
        "console_url": [None, "https://console.acme.test"],
    }
    token = await sign_in(client)
    site = (await client.get(API + "/setup", headers=auth(token))).json()["site"]
    assert (site["site_name"], site["version"], site["setup_completed_at"]) == ("Acme AI", 2, None)
    # A managed group is fixed: the wizard must submit it unchanged.
    managed = {"site_name": "Managed Name", "console_url": "https://console.managed.test"}
    result = await import_policies(
        settings,
        policy_file(
            "managed.json", {"identity.site_policy": {"mode": "managed", "values": managed}}
        ),
    )
    assert result["groups"]["identity.site_policy"]["status"] == "applied"
    status = (await client.get(API + "/setup", headers=auth(token))).json()
    assert status["required"] is True and status["site"]["managed"] is not None
    locked_site = {k: status["site"][k] for k in SITE}
    changed = await client.post(
        API + "/setup", headers=auth(token), json=completion(site={**locked_site, "site_name": "X"})
    )
    assert (changed.status_code, code(changed)) == (409, "policy_managed_externally")
    done = await client.post(API + "/setup", headers=auth(token), json=completion(site=locked_site))
    assert done.status_code == 200, done.text
    assert done.json()["site"]["managed"] is not None
    view = (await client.get(API + "/settings/site", headers=auth(token))).json()
    refused = await client.put(
        API + "/settings/site",
        headers=auth(token, view["version"]),
        json={k: view[k] for k in SITE},
    )
    assert (refused.status_code, code(refused)) == (409, "policy_managed_externally")
    # Importing the file again leaves the finished setup alone.
    again = await import_policies(
        settings,
        policy_file(
            "managed.json", {"identity.site_policy": {"mode": "managed", "values": managed}}
        ),
    )
    assert again["groups"]["identity.site_policy"]["status"] == "unchanged"
    assert admin.execute(
        "SELECT setup_completed_at IS NOT NULL FROM identity.site_policy"
    ).fetchone() == (True,)


async def test_sign_in_by_address_prefers_a_login_with_that_spelling(http, admin):
    client, root, _, _ = http
    # The administrator's unverified address signs in, in any letter case.
    await login(client, ADMIN_EMAIL.upper())
    # A login spelled like an address wins over the account that uses the address.
    created = await create_user(client, root, ADMIN_EMAIL)
    token = await login(client, ADMIN_EMAIL)
    me = (await client.get(API + "/me", headers=auth(token))).json()
    assert me["user"]["id"] == created["user"]["id"]
    # Proving an address takes it from an account that only claimed it.
    admin.execute(
        "UPDATE identity.users SET unverified_email='shared@corp.internal' WHERE id=%s",
        (BUILTIN_ADMIN_ID,),
    )
    member = await create_user(client, root, "address-owner")
    admin.execute(
        "UPDATE identity.users SET verified_email='shared@corp.internal' WHERE id=%s",
        (member["user"]["id"],),
    )
    token = await login(client, "shared@corp.internal")
    assert (await client.get(API + "/me", headers=auth(token))).json()["user"]["login"] == (
        "address-owner"
    )
