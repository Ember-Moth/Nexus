import asyncio
import json
import re
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr
from test_identity_api import PASSWORD, auth

from nexus.modules.identity.providers.authentication import AuthProviders

pytestmark = pytest.mark.integration
API = "/api/v1"


@pytest.fixture
def delivery(http):
    client, root, app, settings = http
    mail = app.state.identity_service
    messages = []
    challenge_requests = []

    def provider(request):
        if request.url.host == "challenges.cloudflare.com":
            form = parse_qs(request.content.decode())
            assert form["secret"] == ["test-secret"]
            action = form["response"][0]
            challenge_requests.append(action)
            return httpx.Response(
                200, json={"success": True, "hostname": "console.example.com", "action": action}
            )
        assert str(request.url) == "https://api.resend.com/emails"
        assert request.headers["authorization"] == "Bearer test-mail-key"
        payload = json.loads(request.content)
        token = re.search(r"nx[vrb]_[a-zA-Z0-9_-]{43}", payload["text"])[0]
        messages.append((payload["to"][0], token, request.headers["Idempotency-Key"]))
        return httpx.Response(200, json={"id": request.headers["Idempotency-Key"]})

    mail.providers = AuthProviders(
        settings.model_copy(
            update=dict(
                mail_provider="resend",
                resend_api_key=SecretStr("test-mail-key"),
                mail_from="robot@example.com",
                turnstile_hostname="console.example.com",
                turnstile_secret_key=SecretStr("test-secret"),
                turnstile_site_key="test-site-key",
            )
        ),
        transport=httpx.MockTransport(provider),
    )
    return client, root, mail, messages, challenge_requests


async def test_verified_signup_login_and_recovery(delivery, admin):
    client, root, mail, messages, actions = delivery
    settings = API + "/settings/authentication"
    policy = (await client.get(settings, headers=auth(root))).json()
    assert policy == {"email_verification": False, "turnstile": False, "version": 1}
    payload = {"email_verification": True, "turnstile": True}
    assert (await client.put(settings, headers=auth(root, 1), json=payload)).status_code == 200
    assert (await client.put(settings, headers=auth(root, 1), json=payload)).status_code == 412
    options = (await client.get(API + "/auth/options")).json()
    assert options == {
        **payload,
        "turnstile_site_key": "test-site-key",
        "email_delivery_available": True,
    }

    registration = {"login": "new-member", "email": "Member@example.com", "password": PASSWORD}
    assert (await client.post(API + "/auth/registrations", json=registration)).status_code == 403
    assert (
        await client.post(
            API + "/auth/registrations", json={**registration, "turnstile_token": "login"}
        )
    ).status_code == 403
    pending = await client.post(
        API + "/auth/registrations", json={**registration, "turnstile_token": "register"}
    )
    assert pending.status_code == 202 and pending.json() == {"email_verification_required": True}
    login_body = {"login": "new-member", "password": PASSWORD, "turnstile_token": "login"}
    assert (await client.post(API + "/auth/sessions", json=login_body)).status_code == 401
    assert await mail.dispatch_email() == 1
    recipient, token, _ = messages.pop()
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
    recipient, reset, _ = messages.pop()
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
    bind = API + "/me/email"
    args = {"email": "legacy@example.com", "password": PASSWORD}
    assert (
        await client.post(bind, headers=auth(root), json={**args, "password": "incorrect"})
    ).status_code == 401
    queued = await client.post(bind, headers=auth(root), json=args)
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
        secret not in str(fields) for secret in [PASSWORD, args["email"], root, messages[-1][1]]
    )
    assert (
        await client.post(bind + "/verify", headers=auth(root), json={"token": messages.pop()[1]})
    ).status_code == 204
    assert (
        await client.post(API + "/auth/password/forgot", json={"email": args["email"]})
    ).status_code == 202
    assert await mail.dispatch_email() == 1
    old_reset = messages.pop()[1]
    assert (
        await client.post(bind, headers=auth(root), json={**args, "email": "new@example.com"})
    ).status_code == 202
    assert await mail.dispatch_email() == 1
    assert (
        await client.post(bind + "/verify", headers=auth(root), json={"token": messages.pop()[1]})
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
            API + "/auth/password/reset", json={"token": messages.pop()[1], "password": PASSWORD}
        )
    ).status_code == 401
    assert await mail.dispatch_email() == 0
    assert admin.execute("SELECT count(*) FROM identity.email_challenges").fetchone() == (0,)


async def test_optional_email_is_not_trusted_for_recovery(delivery):
    client, _, mail, messages, _ = delivery
    address = "optional@example.com"
    registered = await client.post(
        API + "/auth/registrations",
        json={"login": "optional-member", "password": PASSWORD, "email": address},
    )
    assert registered.status_code == 201
    token = registered.json()["access_token"]
    me = await client.get(API + "/me", headers=auth(token))
    assert me.json()["verified_email"] is None
    assert (
        await client.post(API + "/auth/password/forgot", json={"email": address})
    ).status_code == 202
    assert await mail.dispatch_email() == 1 and await mail.dispatch_email() == 0
    assert messages[0][1].startswith("nxb_")
    assert (
        await client.post(
            API + "/me/email/verify", headers=auth(token), json={"token": messages[0][1]}
        )
    ).status_code == 204
    assert (await client.get(API + "/me", headers=auth(token))).json()["verified_email"] == address
