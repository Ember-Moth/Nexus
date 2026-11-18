import json
import smtplib
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from nexus.modules.identity.application.schemas import MailSettings, TurnstileSettings
from nexus.modules.identity.domain.delivery import MailTransport, TurnstileKeys
from nexus.modules.identity.domain.errors import IdentityError, MailRejected
from nexus.modules.identity.providers.authentication import SECRET_PROBE, AuthProviders

KEYS = TurnstileKeys("test-site", "test-secret", "console.example.com")
RESEND = MailTransport("resend", "sender@example.com", "example-key")


def smtp(**changes):
    values = dict(
        provider="smtp",
        sender="nexus@example.com",
        secret="test-password",
        smtp_host="smtp.example.com",
        smtp_username="mailer",
    )
    return MailTransport(**(values | changes))


async def send(sender, transport, purpose="reset", token="fixture-token"):
    return await sender.send(
        transport, "reader@example.com", purpose, token, uuid4(), site_name="Nexus"
    )


@pytest.mark.parametrize("security", ["starttls", "tls"])
async def test_smtp_sends_one_message_after_tls(security):
    transport = smtp(smtp_port=465 if security == "tls" else 587, smtp_security=security)
    sender = AuthProviders()
    connection = MagicMock()
    connection.return_value.__enter__.return_value = connection.return_value
    factory = "smtplib.SMTP_SSL" if security == "tls" else "smtplib.SMTP"
    with patch(factory, connection):
        await send(sender, transport)
    conn = connection.return_value
    if security == "starttls":
        conn.starttls.assert_called_once()
    else:
        assert connection.call_args.kwargs["context"].check_hostname
    conn.login.assert_called_once_with("mailer", "test-password")
    message = conn.send_message.call_args.args[0]
    assert message["To"] == "reader@example.com" and message["From"] == "nexus@example.com"
    assert "fixture-token" in message.get_content()


@pytest.mark.parametrize("status", ["delivered", "queued", "permanent_bounces"])
async def test_cloudflare_delivery_receipt(status):
    account = "1" * 32
    recipient = "reader@example.com"

    def provider(request):
        assert (
            str(request.url)
            == f"https://api.cloudflare.com/client/v4/accounts/{account}/email/sending/send"
        )
        assert request.headers["authorization"] == "Bearer example-token"
        assert json.loads(request.content)["to"] == recipient
        return httpx.Response(200, json={"success": True, "result": {status: [recipient]}})

    sender = AuthProviders(transport=httpx.MockTransport(provider))
    transport = MailTransport(
        "cloudflare", "sender@example.com", "example-token", cloudflare_account_id=account
    )
    if status == "permanent_bounces":
        # A synchronous bounce is final: the worker drops the message instead of retrying it.
        with pytest.raises(MailRejected):
            await send(sender, transport)
    else:
        await send(sender, transport)


@pytest.mark.parametrize(
    ("code", "response", "expected"),
    [
        (200, {"success": False, "error-codes": ["timeout-or-duplicate"]}, 403),
        (200, {"success": True, "hostname": "different.example.com", "action": "login"}, 403),
        (200, {"success": True, "hostname": "console.example.com", "action": "register"}, 403),
        (200, [], 503),
        (503, {}, 503),
    ],
)
async def test_turnstile_denials(code, response, expected):
    sender = AuthProviders(
        transport=httpx.MockTransport(lambda req: httpx.Response(code, json=response))
    )
    with pytest.raises(IdentityError) as rejected:
        await sender.verify(KEYS, SecretStr("test-response"), "login")
    assert rejected.value.status == expected


def resend(response):
    return AuthProviders(transport=httpx.MockTransport(lambda request: response))


@pytest.mark.parametrize("status", [400, 404, 422])
async def test_resend_client_errors_reject_the_message(status):
    sender = resend(httpx.Response(status, json={"message": "The to field is invalid"}))
    with pytest.raises(MailRejected) as rejected:
        await send(sender, RESEND, "bind")
    assert rejected.value.status == status
    summary = sender.failure_summary(rejected.value, RESEND)
    assert summary == f"HTTP {status}: The to field is invalid"


@pytest.mark.parametrize("status", [401, 403, 409, 429, 500, 503])
async def test_resend_credential_and_transient_errors_stay_retryable(status):
    sender = resend(httpx.Response(status, json={}))
    with pytest.raises(Exception) as failed:
        await send(sender, RESEND, "bind")
    assert not isinstance(failed.value, MailRejected)
    assert sender.failure_status(failed.value) == status


@pytest.mark.parametrize(
    ("error", "permanent"),
    [
        (smtplib.SMTPRecipientsRefused({"reader@example.com": (550, b"no such user")}), True),
        (smtplib.SMTPRecipientsRefused({"reader@example.com": (451, b"greylisted")}), False),
        (smtplib.SMTPDataError(554, b"message refused"), True),
        (smtplib.SMTPDataError(452, b"try later"), False),
        (smtplib.SMTPAuthenticationError(535, b"bad credentials"), False),
        (smtplib.SMTPSenderRefused(553, b"sender refused", "nexus@example.com"), False),
    ],
)
async def test_smtp_only_final_recipient_or_message_refusals_reject(error, permanent):
    sender = AuthProviders()
    with patch.object(sender, "smtp_send", side_effect=error):
        with pytest.raises(MailRejected if permanent else type(error)):
            await send(sender, smtp())


async def test_registration_notice_carries_no_token():
    sent = []

    def provider(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "receipt"})

    sender = AuthProviders(transport=httpx.MockTransport(provider))
    await send(sender, RESEND, "exists", "nxe_" + "a" * 43)
    assert "nxe_" not in sent[0]["text"] and "already taken" in sent[0]["text"]


async def test_mail_needs_a_configured_provider():
    with pytest.raises(RuntimeError):
        await send(AuthProviders(), None)


def test_failure_summaries_never_contain_the_secret():
    transport = smtp(secret="s3cret-pass")
    error = smtplib.SMTPAuthenticationError(535, b"5.7.8 rejected s3cret-pass for mailer")
    summary = AuthProviders.failure_summary(error, transport)
    assert summary == "SMTP 535: 5.7.8 rejected [redacted] for mailer"
    long = AuthProviders.failure_summary(OSError("x" * 1000), transport)
    assert len(long) == 300


@pytest.mark.parametrize(
    ("errors", "valid"),
    [
        (["invalid-input-response"], True),
        ([], True),
        (["invalid-input-secret"], False),
        (["missing-input-secret"], False),
    ],
)
async def test_turnstile_secret_check_uses_a_token_that_cannot_pass(errors, valid):
    def provider(request):
        form = parse_qs(request.content.decode())
        assert form == {"secret": ["candidate"], "response": [SECRET_PROBE]}
        return httpx.Response(200, json={"success": not errors, "error-codes": errors})

    sender = AuthProviders(transport=httpx.MockTransport(provider))
    assert await sender.secret_valid("candidate") is valid


@pytest.mark.parametrize(
    ("keys", "response", "reason"),
    [
        (KEYS, {"success": False, "error-codes": ["invalid-input-secret"]}, "invalid_secret"),
        (KEYS, {"success": False, "error-codes": ["missing-input-secret"]}, "invalid_secret"),
        (None, None, "not_configured"),
    ],
)
async def test_turnstile_configuration_faults_stay_server_side(caplog, keys, response, reason):
    caplog.set_level("ERROR", logger="nexus.auth")
    sender = AuthProviders(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=response))
    )
    with pytest.raises(IdentityError) as rejected:
        await sender.verify(keys, SecretStr("test-response"), "register")
    assert (rejected.value.code, rejected.value.status) == ("turnstile_unavailable", 503)
    assert "credential" not in str(rejected.value).lower()
    assert [r.event_fields["outcome"] for r in caplog.records] == [reason]


def test_mail_settings_name_one_provider():
    smtp_only = MailSettings.model_validate(
        {
            "provider": "smtp",
            "sender": "Robot@Example.com",
            "smtp": {"host": "SMTP.Example.com", "username": "u"},
        }
    )
    assert smtp_only.sender == "robot@example.com" and smtp_only.smtp.host == "smtp.example.com"
    assert "password" not in smtp_only.smtp.model_fields_set
    cleared = MailSettings.model_validate(
        {"provider": "resend", "sender": "a@b.co", "resend": {"api_key": None}}
    )
    assert "api_key" in cleared.resend.model_fields_set and cleared.resend.api_key is None
    for invalid in (
        {"provider": None, "sender": "a@b.co"},
        {"provider": "smtp", "sender": "a@b.co"},
        {"provider": "resend"},
        {"provider": "resend", "sender": "a@b.co", "smtp": {"host": "h"}},
        {"provider": "cloudflare", "sender": "a@b.co", "cloudflare": {"account_id": "x"}},
        {"provider": "resend", "sender": "a@b.co", "resend": {"api_key": "two words"}},
        {"provider": "smtp", "sender": "a@b.co", "smtp": {"host": "http://smtp"}},
    ):
        with pytest.raises(ValidationError):
            MailSettings.model_validate(invalid)


def test_turnstile_settings_keep_keys_together():
    keys = TurnstileSettings.model_validate({"site_key": "0x4AAA", "hostname": "Console.Test"})
    assert keys.hostname == "console.test" and "secret_key" not in keys.model_fields_set
    assert TurnstileSettings.model_validate({"site_key": None}).hostname is None
    for invalid in (
        {"site_key": None, "secret_key": "x"},
        {"site_key": None, "hostname": "console.test"},
        {"site_key": "has space"},
        {"site_key": "0x4AAA", "hostname": "https://console.test"},
    ):
        with pytest.raises(ValidationError):
            TurnstileSettings.model_validate(invalid)
