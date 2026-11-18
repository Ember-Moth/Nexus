"""Identity mail and Turnstile settings stored encrypted in the identity policy tables."""

import asyncio
import hashlib
import json
from uuid import uuid4

import httpx
import pytest
from mail_stub import (
    MAIL,
    MAIL_KEY,
    PASSWORD,
    SITE_KEY,
    TURNSTILE,
    TURNSTILE_SECRET,
    put_settings,
    stub_provider,
)
from policies import put_policy
from test_email_auth import member
from test_identity_api import auth, create_user, login

from nexus.api import create_app
from nexus.bootstrap import Application
from nexus.commands import import_policies
from nexus.config import RuntimeSettings
from nexus.shared.policy import POLICY_TTL_SECONDS, PolicyRejected

pytestmark = pytest.mark.integration
API = "/api/v1"
MAIL_PATH = API + "/settings/mail"
TURNSTILE_PATH = API + "/settings/turnstile"
AUTH_PATH = API + "/settings/authentication"
SECRETS = (MAIL_KEY, TURNSTILE_SECRET, "replacement-key", "smtp-secret-value")


def code(response):
    return response.json()["error"]["code"]


def no_secrets(*texts):
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


def audits(admin, action):
    return [
        row[0]
        for row in admin.execute(
            "SELECT details FROM identity.audit_events WHERE action=%s ORDER BY created_at",
            (action,),
        )
    ]


async def put(client, token, path, body):
    current = (await client.get(path, headers=auth(token))).json()
    return await client.put(path, headers=auth(token, current["version"]), json=body)


def stubbed(app):
    messages, challenges = [], []
    app.state.identity_service.providers.transport = httpx.MockTransport(
        stub_provider(messages, challenges)
    )
    return messages


async def test_reads_never_return_secrets_and_omitted_secrets_are_kept(http, admin):
    client, root, app, _ = http
    messages = stubbed(app)
    initial = await client.get(MAIL_PATH, headers=auth(root))
    assert initial.headers["etag"] == '"1"'
    assert initial.json() == {
        "provider": None,
        "sender": None,
        "smtp": None,
        "resend": None,
        "cloudflare": None,
        "available": False,
        "required_by": [],
        "key_version": None,
        "managed": None,
        "version": 1,
    }
    saved = await put(client, root, MAIL_PATH, MAIL)
    assert saved.status_code == 200, saved.text
    view = (await client.get(MAIL_PATH, headers=auth(root))).json()
    assert view["resend"] == {"api_key_set": True} and view["available"] is True
    assert (view["key_version"], view["version"]) == (1, 2)
    no_secrets(saved.text, json.dumps(view))
    stored = admin.execute("SELECT credential,key_version FROM identity.mail_policy").fetchone()
    assert MAIL_KEY.encode() not in bytes(stored[0]) and stored[1] == 1
    # A PUT without the secret keeps it: the provider still accepts the stored key.
    kept = await put(client, root, MAIL_PATH, {"provider": "resend", "sender": "ops@example.com"})
    assert kept.status_code == 200, kept.text
    assert kept.json()["resend"] == {"api_key_set": True}
    sent = await client.post(
        MAIL_PATH + "/test", headers=auth(root), json={"recipient": "reader@example.com"}
    )
    assert sent.json()["accepted"] is True, sent.text
    assert messages.pop().subject == "Test message from Nexus"
    # Null removes a secret; Resend cannot work without one.
    cleared = await put(
        client,
        root,
        MAIL_PATH,
        {"provider": "resend", "sender": "ops@example.com", "resend": {"api_key": None}},
    )
    assert (cleared.status_code, code(cleared)) == (422, "invalid_mail_settings")
    # Another provider replaces the section and drops the previous secret.
    smtp = {
        "provider": "smtp",
        "sender": "ops@example.com",
        "smtp": {
            "host": "smtp.corp.internal",
            "username": "mailer",
            "password": "smtp-secret-value",
        },
    }
    switched = await put(client, root, MAIL_PATH, smtp)
    assert switched.status_code == 200, switched.text
    assert switched.json()["smtp"] == {
        "host": "smtp.corp.internal",
        "port": 587,
        "security": "starttls",
        "username": "mailer",
        "password_set": True,
    }
    assert switched.json()["resend"] is None
    no_secrets(switched.text)
    # Without a username there is no password to keep.
    anonymous = await put(
        client, root, MAIL_PATH, {**smtp, "smtp": {"host": "smtp.corp.internal", "port": 25}}
    )
    assert anonymous.json()["smtp"]["password_set"] is False
    assert admin.execute("SELECT credential FROM identity.mail_policy").fetchone() == (None,)
    details = audits(admin, "mail_policy.updated")
    no_secrets(json.dumps(details))
    assert details[0]["resend.api_key"] == [None, "set"]
    assert details[1] == {"sender": ["robot@example.com", "ops@example.com"]}
    assert details[2]["smtp.password"] == [None, "set"]
    assert details[2]["resend.api_key"] == ["set", None]

    # Turnstile keeps its secret the same way; the hostname defaults to the console host.
    keys = await put(client, root, TURNSTILE_PATH, {**TURNSTILE, "hostname": None})
    assert keys.status_code == 200, keys.text
    assert keys.json()["effective_hostname"] == "console.nexus.test"
    renamed = await put(client, root, TURNSTILE_PATH, {"site_key": SITE_KEY, "hostname": "a.test"})
    assert renamed.json()["secret_key_set"] is True and renamed.json()["available"] is True
    assert renamed.json()["effective_hostname"] == "a.test"
    no_secrets(keys.text, renamed.text, json.dumps(audits(admin, "turnstile_policy.updated")))


async def test_providers_in_use_cannot_be_removed(delivery, admin):
    client, root, _, _, _ = delivery
    switches = await put_policy(client, root, AUTH_PATH, {"email_verification": True})
    assert switches["email_verification"] is True
    view = (await client.get(MAIL_PATH, headers=auth(root))).json()
    assert view["required_by"] == ["email_verification"]
    removed = await put(client, root, MAIL_PATH, {"provider": None})
    assert (removed.status_code, code(removed)) == (409, "mail_in_use")
    # Turnstile is on with the console host as its hostname.
    await put_settings(client, root, "/settings/turnstile", {**TURNSTILE, "hostname": None})
    await put_policy(client, root, AUTH_PATH, {"turnstile": True})
    dropped = await put(client, root, TURNSTILE_PATH, {"site_key": None})
    assert (dropped.status_code, code(dropped)) == (409, "turnstile_in_use")
    site = (await client.get(API + "/settings/site", headers=auth(root))).json()
    console = await client.put(
        API + "/settings/site",
        headers=auth(root, site["version"]),
        json={"site_name": site["site_name"], "console_url": None},
    )
    assert (console.status_code, code(console)) == (409, "turnstile_in_use")
    # Once the switches are off, the providers can go.
    await put_policy(client, root, AUTH_PATH, {"email_verification": False, "turnstile": False})
    assert (await put(client, root, MAIL_PATH, {"provider": None})).status_code == 200
    assert (await put(client, root, TURNSTILE_PATH, {"site_key": None})).status_code == 200
    # And a switch cannot be turned on without its provider.
    current = (await client.get(AUTH_PATH, headers=auth(root))).json()
    body = {k: v for k, v in current.items() if k not in {"version", "managed"}}
    for switch, status, error in [
        ("email_verification", 503, "mail_unavailable"),
        ("turnstile", 409, "turnstile_not_configured"),
    ]:
        refused = await client.put(
            AUTH_PATH, headers=auth(root, current["version"]), json={**body, switch: True}
        )
        assert (refused.status_code, code(refused)) == (status, error)
    options = (await client.get(API + "/auth/options")).json()
    assert options["email_delivery_available"] is False


async def test_changes_reach_every_api_process_within_the_policy_ttl(http):
    client, root, app, settings = http
    stubbed(app)
    other = create_app(settings.model_copy(update={"redis_namespace": "nexus:p2:" + uuid4().hex}))
    async with (
        other.router.lifespan_context(other),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=other), base_url="http://other"
        ) as second,
    ):

        async def options():
            return (await second.get(API + "/auth/options")).json()

        assert (await options())["email_delivery_available"] is False
        await put_settings(client, root, "/settings/mail", MAIL)
        await put_settings(client, root, "/settings/turnstile", TURNSTILE)
        await put_policy(client, root, AUTH_PATH, {"turnstile": True})
        # The writer applies its change at once; another process within the TTL.
        mine = (await client.get(API + "/auth/options")).json()
        assert mine["email_delivery_available"] is True
        assert mine["turnstile_site_key"] == SITE_KEY
        await asyncio.sleep(POLICY_TTL_SECONDS + 0.1)
        theirs = await options()
        assert theirs["email_delivery_available"] is True
        assert theirs["turnstile_site_key"] == SITE_KEY
        # The other process (an API, or a Worker sending queued mail) decrypts the same secret.
        transport = await other.state.identity_service.mail_transport()
        assert (transport.provider, transport.secret) == ("resend", MAIL_KEY)
        # Keys can change while Turnstile is on, as long as they stay complete.
        await put_settings(client, root, "/settings/turnstile", {**TURNSTILE, "site_key": "k2"})
        await asyncio.sleep(POLICY_TTL_SECONDS + 0.1)
        assert (await options())["turnstile_site_key"] == "k2"


async def test_test_message_reports_the_provider_answer(http, admin):
    client, root, app, _ = http
    messages = stubbed(app)
    unconfigured = await client.post(
        MAIL_PATH + "/test", headers=auth(root), json={"recipient": "ops@corp.internal"}
    )
    assert (unconfigured.status_code, code(unconfigured)) == (503, "mail_unavailable")
    await put_settings(client, root, "/settings/mail", MAIL)
    await put_policy(client, root, AUTH_PATH, {"mail": {"per_recipient": 2}})
    sent = await client.post(
        MAIL_PATH + "/test", headers=auth(root), json={"recipient": "Ops@Corp.Internal"}
    )
    assert sent.status_code == 200, sent.text
    assert sent.json() == {
        "accepted": True,
        "provider": "resend",
        "provider_status": 200,
        "detail": None,
    }
    assert messages.pop().to == "ops@corp.internal"
    # A key the provider refuses: the answer is summarized, the key never appears.
    await put_settings(
        client,
        root,
        "/settings/mail",
        {**MAIL, "resend": {"api_key": "replacement-key"}},
    )
    failed = await client.post(
        MAIL_PATH + "/test", headers=auth(root), json={"recipient": "ops@corp.internal"}
    )
    assert failed.json() == {
        "accepted": False,
        "provider": "resend",
        "provider_status": 401,
        "detail": "HTTP 401: API key is invalid",
    }
    # Test messages draw on the identity mail budgets.
    limited = await client.post(
        MAIL_PATH + "/test", headers=auth(root), json={"recipient": "ops@corp.internal"}
    )
    assert (limited.status_code, code(limited)) == (429, "rate_limited")
    trail = audits(admin, "mail_policy.test_sent")
    assert [(d["recipient"], d["accepted"], d["provider_status"]) for d in trail] == [
        ("ops@corp.internal", True, 200),
        ("ops@corp.internal", False, 401),
    ]
    no_secrets(failed.text, json.dumps(trail))


async def test_turnstile_secret_is_checked_with_siteverify(http, admin):
    client, root, app, _ = http
    stubbed(app)
    wrong = await put(client, root, TURNSTILE_PATH, {**TURNSTILE, "secret_key": "not-the-secret"})
    assert (wrong.status_code, code(wrong)) == (422, "invalid_turnstile_secret")
    assert admin.execute("SELECT version FROM identity.turnstile_policy").fetchone() == (1,)
    app.state.identity_service.providers.transport = httpx.MockTransport(
        lambda request: httpx.Response(503)
    )
    down = await put(client, root, TURNSTILE_PATH, TURNSTILE)
    assert (down.status_code, code(down)) == (503, "turnstile_unavailable")
    stubbed(app)
    saved = await put(client, root, TURNSTILE_PATH, TURNSTILE)
    assert saved.status_code == 200, saved.text
    assert saved.json()["available"] is True and saved.json()["key_version"] == 1
    # Only a new secret is checked; keys without one are refused.
    missing = await put(client, root, TURNSTILE_PATH, {"site_key": SITE_KEY, "secret_key": None})
    assert (missing.status_code, code(missing)) == (422, "invalid_turnstile_settings")
    await create_user(client, root, "t-member")
    other = await login(client, "t-member")
    forbidden = await client.get(TURNSTILE_PATH, headers=auth(other))
    assert forbidden.status_code == 403


def policy_file(tmp_path, groups, name="policies.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"version": 1, "groups": groups}))
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


async def test_import_resolves_secret_references_and_locks_the_groups(
    http, admin, tmp_path, monkeypatch
):
    client, root, app, settings = http
    messages = stubbed(app)
    monkeypatch.setenv("NEXUS_TEST_RESEND_KEY", MAIL_KEY)
    secret_file = tmp_path / "turnstile-secret"
    secret_file.write_text(TURNSTILE_SECRET + "\n")
    groups = {
        "identity.mail_policy": {
            "mode": "managed",
            "values": {
                "provider": "resend",
                "sender": "robot@example.com",
                "resend": {"api_key": {"env": "NEXUS_TEST_RESEND_KEY"}},
            },
        },
        "identity.turnstile_policy": {
            "mode": "managed",
            "values": {"site_key": SITE_KEY, "secret_key": {"file": str(secret_file)}},
        },
        # The switches need the providers configured by the same file.
        "identity.auth_policy": {
            "mode": "initial",
            "values": {"email_verification": True, "turnstile": True},
        },
    }
    path, digest = policy_file(tmp_path, groups)
    no_secrets(json.dumps(json.loads((tmp_path / "policies.json").read_text())))
    dry = await import_policies(settings, path, dry_run=True)
    assert dry["groups"]["identity.mail_policy"]["changes"] == {
        "provider": [None, "resend"],
        "sender": [None, "robot@example.com"],
        "resend.api_key": [None, "set"],
        "managed": [False, True],
    }
    assert admin.execute("SELECT version FROM identity.mail_policy").fetchone() == (1,)
    applied = await import_policies(settings, path)
    no_secrets(json.dumps(applied))
    assert {g["status"] for g in applied["groups"].values()} == {"applied"}
    mail = (await client.get(MAIL_PATH, headers=auth(root))).json()
    assert mail["managed"]["file_sha256"] == digest and mail["available"] is True
    turnstile = (await client.get(TURNSTILE_PATH, headers=auth(root))).json()
    assert turnstile["effective_hostname"] == "console.nexus.test"
    assert turnstile["required_by"] == ["turnstile"]
    (audit,) = audits(admin, "mail_policy.updated")
    assert audit["actor"] == "config-import" and audit["mode"] == "managed"
    no_secrets(json.dumps(audits(admin, "turnstile_policy.updated")))
    # Managed groups are read-only for administrators.
    for target, body in [(MAIL_PATH, {"provider": None}), (TURNSTILE_PATH, {"site_key": None})]:
        refused = await put(client, root, target, body)
        assert (refused.status_code, code(refused)) == (409, "policy_managed_externally")
    # The imported key works.
    sent = await client.post(
        MAIL_PATH + "/test", headers=auth(root), json={"recipient": "ops@corp.internal"}
    )
    assert sent.json()["accepted"] is True and messages.pop().to == "ops@corp.internal"
    # The same file and the same secrets change nothing; a new secret behind the same reference
    # replaces the stored one.
    again = await import_policies(settings, path)
    assert {g["status"] for g in again["groups"].values()} == {"unchanged"}
    monkeypatch.setenv("NEXUS_TEST_RESEND_KEY", "replacement-key")
    rotated = await import_policies(settings, path)
    assert rotated["groups"]["identity.mail_policy"]["changes"] == {
        "resend.api_key": ["set", "replaced"]
    }

    # Secrets in clear, unknown references and unset variables reject the whole file.
    for value, message in [
        ("plain-text-key", "resend.api_key must be a reference"),
        ({"vault": "x"}, "resend.api_key must be a reference"),
        ({"env": "NEXUS_TEST_UNSET"}, "environment variable NEXUS_TEST_UNSET is not set"),
        ({"file": str(tmp_path / "absent")}, "cannot read"),
    ]:
        bad = json.loads(json.dumps(groups))
        bad["identity.mail_policy"]["values"]["resend"]["api_key"] = value
        bad_path, _ = policy_file(tmp_path, bad, "bad.json")
        with pytest.raises(PolicyRejected, match=message):
            await import_policies(settings, bad_path)
    # Removing a provider while its switch stays on is refused; turning it off with it is not.
    removal = {
        "identity.mail_policy": {"mode": "managed", "values": {"provider": None}},
    }
    removal_path, _ = policy_file(tmp_path, removal, "removal.json")
    with pytest.raises(PolicyRejected, match="identity.mail_policy: email verification needs"):
        await import_policies(settings, removal_path)
    removal["identity.auth_policy"] = {
        "mode": "managed",
        "values": {"email_verification": False},
    }
    removal_path, _ = policy_file(tmp_path, removal, "removal-off.json")
    done = await import_policies(settings, removal_path)
    assert done["groups"]["identity.mail_policy"]["changes"]["resend.api_key"] == ["set", None]
    assert admin.execute("SELECT provider,credential FROM identity.mail_policy").fetchone() == (
        None,
        None,
    )


async def test_the_worker_sends_queued_mail_with_the_stored_settings(
    delivery, admin, service_directory
):
    """Identity mail leaves from the Worker, which reads the same encrypted settings."""
    client, root, mail, _, _ = delivery
    _, session = await member(client, root, admin, "worker-member", "worker@example.com")
    queued = await client.post(
        API + "/me/email",
        headers=auth(session),
        json={"email": "bound@example.com", "password": PASSWORD},
    )
    assert queued.status_code == 202, queued.text
    settings = RuntimeSettings(
        _env_file=service_directory / ".env.worker", environment="test", process_role="worker"
    )
    worker = Application(settings)
    try:
        sent = []
        worker.identity.providers.transport = httpx.MockTransport(stub_provider(sent, []))
        transport = await worker.identity.mail_transport()
        assert (transport.provider, transport.secret) == ("resend", MAIL_KEY)
        assert await worker.identity.dispatch_email() == 1
        assert sent[0].to == "bound@example.com" and sent[0].token.startswith("nxb_")
    finally:
        await worker.close()
