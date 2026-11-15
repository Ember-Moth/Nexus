import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import psycopg
import pytest
from mail_stub import install_mail
from test_identity_api import PASSWORD, auth, create_user, login

from nexus.api import create_app
from nexus.infrastructure.passwords import Argon2Passwords
from nexus.worker import run_worker

pytestmark = pytest.mark.integration
API = "/api/v1"
OPEN = {"registration": True, "email_verification": True, "turnstile": False}


def body_without_trace(response):
    error = dict(response.json()["error"])
    error.pop("trace_id")
    return response.status_code, error


async def open_registration(client, root, **changes):
    settings = API + "/settings/authentication"
    current = await client.get(settings, headers=auth(root))
    response = await client.put(
        settings, headers=auth(root, current.json()["version"]), json={**OPEN, **changes}
    )
    assert response.status_code == 200, response.text
    return response.json()


def count_hashes(service):
    """Record each Argon2 hash the service computes."""
    calls = []
    original = service._passwords

    class Recording:
        def hash(self, password):
            calls.append(password)
            return original.hash(password)

        def verify(self, password_hash, password):
            return original.verify(password_hash, password)

    service._passwords = Recording()
    return calls


async def member(client, root, admin, name, email):
    """A regular active user with a verified recovery address and a session."""
    created = await create_user(client, root, name)
    admin.execute(
        "UPDATE identity.users SET verified_email=%s WHERE id=%s", (email, created["user"]["id"])
    )
    return created["user"]["id"], await login(client, name)


@asynccontextmanager
async def peers(app, *addresses):
    clients = [
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(address, 4000)), base_url="http://test"
        )
        for address in addresses
    ]
    try:
        yield clients
    finally:
        for client in clients:
            await client.aclose()


async def test_verified_signup_login_and_recovery(delivery, admin):
    client, root, mail, messages, actions = delivery
    settings = API + "/settings/authentication"
    policy = (await client.get(settings, headers=auth(root))).json()
    assert policy == {
        "registration": False,
        "email_verification": False,
        "turnstile": False,
        "version": 1,
    }
    unverified = await client.put(
        settings,
        headers=auth(root, 1),
        json={"registration": True, "email_verification": False, "turnstile": True},
    )
    assert unverified.status_code == 422
    assert unverified.json()["error"]["code"] == "registration_requires_email_verification"
    payload = {"registration": True, "email_verification": True, "turnstile": True}
    assert (await client.put(settings, headers=auth(root, 1), json=payload)).status_code == 200
    assert (await client.put(settings, headers=auth(root, 1), json=payload)).status_code == 412
    options = (await client.get(API + "/auth/options")).json()
    assert options == {
        **payload,
        "turnstile_site_key": "test-site-key",
        "email_delivery_available": True,
    }

    registration = {"login": "new-member", "email": "Member@example.com", "password": PASSWORD}
    hashes = count_hashes(mail)
    assert (await client.post(API + "/auth/registrations", json=registration)).status_code == 403
    assert (
        await client.post(
            API + "/auth/registrations", json={**registration, "turnstile_token": "login"}
        )
    ).status_code == 403
    assert hashes == []  # no Argon2 work before the challenge passes
    pending = await client.post(
        API + "/auth/registrations", json={**registration, "turnstile_token": "register"}
    )
    assert len(hashes) == 1
    assert pending.status_code == 202 and pending.json() == {"email_verification_required": True}
    login_body = {"login": "new-member", "password": PASSWORD, "turnstile_token": "login"}
    assert (await client.post(API + "/auth/sessions", json=login_body)).status_code == 401
    assert await mail.dispatch_email() == 1
    recipient, token, _, _ = messages.pop()
    assert recipient == "member@example.com"
    assert admin.execute("SELECT ciphertext FROM identity.email_challenges").fetchall() == [(None,)]
    verify = {"token": token, "password": PASSWORD, "turnstile_token": "confirm_registration"}
    bad = await client.post(
        API + "/auth/registrations/verify",
        json={**verify, "password": "wrong-password-long-enough"},
    )
    assert bad.status_code == 401
    result = await client.post(API + "/auth/registrations/verify", json=verify)
    assert result.status_code == 201, result.text
    audits = await client.get(
        API + "/audit-events",
        headers=auth(root),
        params={"action": "user.registered", "trace_id": result.headers["x-request-id"]},
    )
    assert [r["trace_id"] for r in audits.json()["items"]] == [result.headers["x-request-id"]]
    session = result.json()["access_token"]
    assert (await client.post(API + "/auth/registrations/verify", json=verify)).status_code == 401
    absent = await client.get(
        API + "/audit-events",
        headers=auth(root),
        params={"trace_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert absent.json()["items"] == []
    me = (await client.get(API + "/me", headers=auth(session))).json()
    assert me["verified_email"] == recipient and len(me["memberships"]) == 1
    assert (await client.get(settings, headers=auth(session))).status_code == 403
    assert (await client.put(settings, headers=auth(session, 2), json=payload)).status_code == 403
    assert (
        await client.post(API + "/auth/sessions", json={**login_body, "turnstile_token": None})
    ).status_code == 403
    second = (await client.post(API + "/auth/sessions", json=login_body)).json()["access_token"]
    for address in ["absent@example.com", recipient]:
        forgot = await client.post(
            API + "/auth/password/forgot",
            json={"email": address, "turnstile_token": "forgot_password"},
        )
        assert forgot.status_code == 202 and forgot.json() == {"accepted": True}
    assert await mail.dispatch_email() == 1 and await mail.dispatch_email() == 0
    recipient, reset, _, _ = messages.pop()
    new_password = "new-password-with-entropy"
    body = {"password": new_password, "token": reset, "turnstile_token": "reset_password"}
    results = await asyncio.gather(
        *(client.post(API + "/auth/password/reset", json=body) for _ in range(2))
    )
    assert sorted(r.status_code for r in results) == [204, 401]
    for old in [session, second]:
        assert (await client.get(API + "/me", headers=auth(old))).status_code == 401
    assert (await client.post(API + "/auth/sessions", json=login_body)).status_code == 401
    assert (
        await client.post(API + "/auth/sessions", json={**login_body, "password": new_password})
    ).status_code == 201
    assert set(actions) >= {
        "register",
        "confirm_registration",
        "login",
        "forgot_password",
        "reset_password",
    }


async def test_binding_rotation_retry_and_expiry(delivery, admin, caplog):
    caplog.set_level("INFO", logger="nexus.mail")
    client, root, mail, messages, _ = delivery
    await create_user(client, root, "mail-member")
    session = await login(client, "mail-member")
    bind = API + "/me/email"
    args = {"email": "legacy@example.com", "password": PASSWORD}
    assert (
        await client.post(bind, headers=auth(session), json={**args, "password": "incorrect"})
    ).status_code == 401
    queued = await client.post(bind, headers=auth(session), json=args)
    assert queued.status_code == 202
    original = mail.providers.transport
    mail.providers.transport = httpx.MockTransport(lambda r: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await mail.dispatch_email()
    assert await mail.dispatch_email() == 0
    admin.execute("UPDATE identity.email_challenges SET next_attempt=now()")
    mail.providers.transport = original
    assert await mail.dispatch_email() == 1
    entries = [r for r in caplog.records if r.name == "nexus.mail"]
    assert [r.msg for r in entries] == [
        "mail.queued",
        "mail.attempt",
        "mail.failed",
        "mail.attempt",
        "mail.accepted",
    ]
    fields = [r.event_fields for r in entries]
    assert {str(f["request_id"]) for f in fields} == {queued.headers["x-request-id"]}
    assert len({f["job_id"] for f in fields}) == 1
    assert fields[2]["provider_status"] == 503 and fields[4]["provider_status"] == 200
    assert fields[4]["provider"] == "resend" and fields[4]["attempt"] == 2
    assert all(
        secret not in str(fields)
        for secret in [PASSWORD, args["email"], session, messages[-1].token]
    )
    assert (
        await client.post(
            bind + "/verify", headers=auth(session), json={"token": messages.pop().token}
        )
    ).status_code == 204
    assert (
        await client.post(API + "/auth/password/forgot", json={"email": args["email"]})
    ).status_code == 202
    assert await mail.dispatch_email() == 1
    old_reset = messages.pop().token
    assert (
        await client.post(bind, headers=auth(session), json={**args, "email": "new@example.com"})
    ).status_code == 202
    assert await mail.dispatch_email() == 1
    assert (
        await client.post(
            bind + "/verify", headers=auth(session), json={"token": messages.pop().token}
        )
    ).status_code == 204
    assert (
        await client.post(
            API + "/auth/password/reset", json={"token": old_reset, "password": PASSWORD}
        )
    ).status_code == 401
    assert (
        await client.post(API + "/auth/password/forgot", json={"email": "new@example.com"})
    ).status_code == 202
    assert await mail.dispatch_email() == 1
    admin.execute("UPDATE identity.email_challenges SET expires_at=now()")
    assert (
        await client.post(
            API + "/auth/password/reset", json={"token": messages.pop().token, "password": PASSWORD}
        )
    ).status_code == 401
    assert await mail.dispatch_email() == 0
    assert admin.execute("SELECT count(*) FROM identity.email_challenges").fetchone() == (0,)


async def test_registration_is_closed_by_default_and_answers_uniformly(delivery, admin):
    client, root, mail, messages, _ = delivery
    hashes = count_hashes(mail)
    closed = [
        await client.post(API + "/auth/registrations", json=body)
        for body in [
            {"login": "brand-new@example.com", "password": PASSWORD},
            {"login": "root-admin", "email": "root@example.com", "password": PASSWORD},
            {"login": "admin", "email": "x@example.com", "password": PASSWORD},
            {"login": "not-an-email", "password": PASSWORD},
        ]
    ]
    expected = {
        "code": "registration_unavailable",
        "message": "Self-service registration is not available",
    }
    assert [body_without_trace(r) for r in closed] == [(403, expected)] * len(closed)
    # Refused before any password hashing or mail.
    assert hashes == []
    assert admin.execute("SELECT count(*) FROM identity.email_challenges").fetchone() == (0,)
    assert (await client.get(API + "/auth/options")).json()["registration"] is False
    with pytest.raises(psycopg.errors.CheckViolation):
        admin.execute("UPDATE identity.auth_policy SET registration=true")

    # Closing registration also stops pending sign-ups from being confirmed.
    await open_registration(client, root)
    started = await client.post(
        API + "/auth/registrations",
        json={"login": "late-member", "email": "late@example.com", "password": PASSWORD},
    )
    assert started.status_code == 202
    assert await mail.dispatch_email() == 1
    await open_registration(client, root, registration=False)
    confirmed = await client.post(
        API + "/auth/registrations/verify",
        json={"token": messages.pop().token, "password": PASSWORD},
    )
    assert confirmed.status_code == 403
    assert confirmed.json()["error"]["code"] == "registration_unavailable"


async def test_registration_does_not_disclose_taken_logins(delivery, admin):
    client, root, mail, messages, _ = delivery
    await open_registration(client, root)
    hashes = count_hashes(mail)
    responses = [
        await client.post(
            API + "/auth/registrations",
            json={"login": name, "email": address, "password": PASSWORD},
        )
        for name, address in [
            ("root-admin", "taken@example.com"),
            ("ＲＯＯＴ-ＡＤＭＩＮ", "taken-variant@example.com"),
            ("fresh-member", "fresh@example.com"),
        ]
    ]
    # Same status, body and work, whether or not the login exists.
    assert {(r.status_code, r.text) for r in responses} == {
        (202, '{"email_verification_required":true}')
    }
    assert len(hashes) == 3
    assert [await mail.dispatch_email() for _ in range(4)] == [1, 1, 1, 0]
    sent = {message.to: message for message in messages}
    # Only the mailbox owner learns that the login is taken; the notice carries no token.
    for address in ["taken@example.com", "taken-variant@example.com"]:
        assert sent[address].token is None
        assert sent[address].subject == "Your Nexus registration request"
    assert sent["fresh@example.com"].token.startswith("nxv_")
    assert admin.execute(
        "SELECT count(*) FROM identity.email_challenges WHERE purpose='exists'"
    ).fetchone() == (2,)


async def test_mail_budget_is_per_normalized_recipient_and_shared_by_every_kind(delivery, admin):
    client, root, mail, messages, _ = delivery
    await open_registration(client, root)
    _, session = await member(client, root, admin, "bind-member", "member@example.com")
    victim = "victim@example.com"
    # Spellings that email_validator maps to the same mailbox share one budget.
    for spelling in ["Victim@EXAMPLE.com", "victim@ｅｘａｍｐｌｅ.com"]:
        response = await client.post(
            API + "/auth/registrations", json={"login": spelling, "password": PASSWORD}
        )
        assert response.status_code == 202
    assert (
        await client.post(API + "/auth/password/forgot", json={"email": "VICTIM@example.com"})
    ).status_code == 202
    # A fourth message of a different kind exceeds the per-recipient budget.
    limited = await client.post(
        API + "/me/email", headers=auth(session), json={"email": victim, "password": PASSWORD}
    )
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "rate_limited"
    assert 0 < int(limited.headers["retry-after"]) <= 3600
    assert admin.execute(
        "SELECT count(*), count(DISTINCT email) FROM identity.email_challenges"
    ).fetchone() == (2, 1)


async def test_mail_budget_exhausted_by_one_requester_does_not_lock_out_others(http, admin):
    client, root, app, settings = http
    mail = app.state.identity_service
    messages, _ = install_mail(mail, settings)
    await member(client, root, admin, "victim-member", "victim@example.com")
    async with peers(app, "198.51.100.10", "2001:db8:7:7::1", "2001:db8:7:7::2") as (
        attacker,
        victim,
        neighbour,
    ):
        # The attacker keeps going well past the old shared per-address threshold (10/min)...
        statuses = [
            (
                await attacker.post(
                    API + "/auth/password/forgot", json={"email": "victim@example.com"}
                )
            ).status_code
            for _ in range(12)
        ]
        assert statuses == [202] * 3 + [429] * 9
        # The account owner, from their own network, still gets a reset message.
        assert (
            await victim.post(API + "/auth/password/forgot", json={"email": "victim@example.com"})
        ).status_code == 202
        assert await mail.dispatch_email() == 1
        assert messages[-1].to == "victim@example.com" and messages[-1].token.startswith("nxr_")
        # Addresses of the same IPv6 /64 are one requester.
        for _ in range(2):
            assert (
                await neighbour.post(
                    API + "/auth/password/forgot", json={"email": "victim@example.com"}
                )
            ).status_code == 202
        assert (
            await neighbour.post(
                API + "/auth/password/forgot", json={"email": "victim@example.com"}
            )
        ).status_code == 429


async def test_account_ip_and_global_mail_budgets(http, admin):
    _, _, _, settings = http
    app = create_app(
        settings.model_copy(
            update={
                "redis_namespace": "nexus:mail-budget:" + uuid4().hex,
                "mail_per_account": 2,
                "mail_per_ip": 4,
                "mail_global": 6,
            }
        )
    )
    async with (
        app.router.lifespan_context(app),
        peers(app, "127.0.0.1", "198.51.100.2", "198.51.100.3") as (
            first,
            second,
            third,
        ),
    ):
        install_mail(app.state.identity_service, settings)
        await app.state.identity_service.bootstrap_admin("root-admin", PASSWORD)
        root = await login(first, "root-admin")
        await create_user(first, root, "spray-member")
        session = await login(first, "spray-member")

        async def bind(address):
            return await first.post(
                API + "/me/email",
                headers=auth(session),
                json={"email": address, "password": PASSWORD},
            )

        async def forgot(client, address):
            return await client.post(API + "/auth/password/forgot", json={"email": address})

        # One account cannot spray binding mail across many addresses.
        assert [(await bind(f"{n}@example.com")).status_code for n in "ab"] == [202, 202]
        assert (await bind("c@example.com")).status_code == 429
        # One requester address has its own budget across recipients.
        assert (await forgot(first, "x@example.com")).status_code == 202
        assert (await forgot(first, "y@example.com")).status_code == 202
        assert (await forgot(first, "z@example.com")).status_code == 429
        # And every requester together shares the global budget.
        assert (await forgot(second, "p@example.com")).status_code == 202
        assert (await forgot(second, "q@example.com")).status_code == 202
        refused = await forgot(third, "r@example.com")
        assert refused.status_code == 429 and refused.headers["retry-after"]


async def test_backlogged_mail_is_still_delivered_and_redeemable(delivery, admin, caplog):
    caplog.set_level("WARNING", logger="nexus.mail")
    client, root, mail, messages, _ = delivery
    await member(client, root, admin, "slow-member", "slow@example.com")
    await member(client, root, admin, "late-member", "late@example.com")
    for address in ["slow@example.com", "late@example.com"]:
        assert (
            await client.post(API + "/auth/password/forgot", json={"email": address})
        ).status_code == 202
    # The queue fell behind by an hour: the original 15-minute token window has passed, but the
    # delivery deadline of one message has not.
    admin.execute("UPDATE identity.email_challenges SET expires_at=now()-interval '1 hour'")
    admin.execute(
        "UPDATE identity.email_challenges SET deliver_by=now()-interval '1 second' "
        "WHERE email='late@example.com'"
    )
    assert await mail.dispatch_email() == 1 and await mail.dispatch_email() == 0
    assert [m.to for m in messages] == ["slow@example.com"]
    assert [r.msg for r in caplog.records] == ["mail.expired"]
    reset = await client.post(
        API + "/auth/password/reset",
        json={"token": messages.pop().token, "password": "fresh-password-with-entropy"},
    )
    assert reset.status_code == 204


async def test_platform_roles_recover_through_administrators_not_email(delivery, admin):
    client, root, mail, messages, _ = delivery
    root_id = admin.execute("SELECT id FROM identity.users WHERE login='root-admin'").fetchone()[0]
    admin.execute(
        "UPDATE identity.users SET verified_email='root@example.com' WHERE id=%s", (root_id,)
    )
    forgot = await client.post(API + "/auth/password/forgot", json={"email": "root@example.com"})
    assert forgot.status_code == 202 and forgot.json() == {"accepted": True}
    assert await mail.dispatch_email() == 0 and messages == []

    # A role granted after the reset mail went out also closes the token.
    user_id, _ = await member(client, root, admin, "future-staff", "staff@example.com")
    await client.post(API + "/auth/password/forgot", json={"email": "staff@example.com"})
    assert await mail.dispatch_email() == 1
    admin.execute(
        "INSERT INTO identity.platform_role_bindings(user_id,role) VALUES(%s,'platform_finance')",
        (user_id,),
    )
    reset = {"token": messages.pop().token, "password": "fresh-password-with-entropy"}
    assert (await client.post(API + "/auth/password/reset", json=reset)).status_code == 401

    # Administrators can still issue setup credentials, and deployments may opt back in.
    issued = await client.post(f"{API}/users/{root_id}/password-reset-tokens", headers=auth(root))
    assert issued.status_code == 201
    mail._platform_email_recovery = True
    await client.post(API + "/auth/password/forgot", json={"email": "root@example.com"})
    assert await mail.dispatch_email() == 1 and messages.pop().to == "root@example.com"


async def test_permanent_rejections_are_dropped_without_failing_the_worker(delivery, admin, caplog):
    caplog.set_level("INFO")
    client, root, mail, messages, _ = delivery
    _, session = await member(client, root, admin, "bounce-member", "ok@example.com")
    original = mail.providers.transport
    mail.providers.transport = httpx.MockTransport(
        lambda request: httpx.Response(422, json={"name": "validation_error"})
    )
    assert (
        await client.post(
            API + "/me/email",
            headers=auth(session),
            json={"email": "bounce@example.com", "password": PASSWORD},
        )
    ).status_code == 202

    class Heartbeats:
        payloads = []

        async def write_heartbeat(self, worker_id, payload, ttl):
            self.payloads.append(payload)

        async def remove_heartbeat(self, worker_id, instance_id):
            pass

    async def ready():
        return {"postgres": "ok"}

    stop = asyncio.Event()
    worker = asyncio.create_task(
        run_worker(
            ready,
            Heartbeats(),
            worker_id="mail-test",
            interval=0.05,
            ttl=30,
            stop=stop,
            extra_consumers={"identity_mail": mail.dispatch_email},
        )
    )
    for _ in range(100):
        if not admin.execute("SELECT count(*) FROM identity.email_challenges").fetchone()[0]:
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.2)
    stop.set()
    await worker
    # Rejected once, removed, never retried, and the worker stayed ready throughout.
    assert admin.execute("SELECT count(*) FROM identity.email_challenges").fetchone() == (0,)
    failures = [r for r in caplog.records if r.msg == "mail.failed"]
    assert [(r.event_fields["outcome"], r.event_fields["provider_status"]) for r in failures] == [
        ("rejected", 422)
    ]
    assert not [r for r in caplog.records if r.msg == "worker.consumer_failed"]
    assert all('"ready": true' in payload for payload in Heartbeats.payloads)

    # A provider outage still keeps the message and is reported.
    mail.providers.transport = httpx.MockTransport(lambda request: httpx.Response(503))
    await client.post(
        API + "/me/email",
        headers=auth(session),
        json={"email": "outage@example.com", "password": PASSWORD},
    )
    with pytest.raises(httpx.HTTPStatusError):
        await mail.dispatch_email()
    assert admin.execute("SELECT count(*) FROM identity.email_challenges").fetchone() == (1,)
    mail.providers.transport = original


async def test_new_logins_are_normalized_and_reserved_names_cannot_be_claimed(
    delivery, admin, signup
):
    client, root, _, _, _ = delivery
    for name in ["admin", "ＲＯＯＴ", "System", "ali​ce"]:
        refused = await client.post(
            API + "/auth/registrations",
            json={"login": name, "email": "squatter@example.com", "password": PASSWORD},
        )
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "invalid_login"
    created = await signup("ＷＩＤＥ-Member", "wide@example.com")
    me = (await client.get(API + "/me", headers=auth(created["access_token"]))).json()
    assert me["user"]["login"] == "wide-member"
    assert await login(client, "ｗｉｄｅ-member")
    # An account stored before normalization keeps signing in with its original spelling.
    admin.execute(
        "INSERT INTO identity.users(id,login,password_hash) VALUES(%s,%s,%s)",
        (uuid4(), "ｌｅｇａｃｙ-user", Argon2Passwords().hash(PASSWORD)),
    )
    assert await login(client, "ＬＥＧＡＣＹ-user")
    # Administrators may create operator logins, but not hidden characters.
    assert (
        await client.post(API + "/users", headers=auth(root), json={"login": "support"})
    ).status_code == 201
    hidden = await client.post(API + "/users", headers=auth(root), json={"login": "sup​port"})
    assert hidden.status_code == 422
