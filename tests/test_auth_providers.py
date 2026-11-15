import json
import smtplib
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from nexus.modules.identity.domain.errors import IdentityError, MailRejected
from nexus.modules.identity.providers.authentication import AuthProviders


@pytest.mark.parametrize("security", ["starttls", "tls"])
async def test_smtp_sends_one_message_after_tls(settings_factory, security):
    settings = settings_factory(
        mail_provider="smtp",
        mail_from="nexus@example.com",
        smtp_host="smtp.example.com",
        smtp_port=465 if security == "tls" else 587,
        smtp_security=security,
        smtp_username="mailer",
        smtp_password="test-password",
    )
    sender = AuthProviders(settings)
    smtp = MagicMock()
    smtp.return_value.__enter__.return_value = smtp.return_value
    factory = "smtplib.SMTP_SSL" if security == "tls" else "smtplib.SMTP"
    with patch(factory, smtp):
        await sender.send("reader@example.com", "reset", "fixture-token", uuid4())
    conn = smtp.return_value
    if security == "starttls":
        conn.starttls.assert_called_once()
    else:
        assert smtp.call_args.kwargs["context"].check_hostname
    conn.login.assert_called_once_with("mailer", "test-password")
    message = conn.send_message.call_args.args[0]
    assert message["To"] == "reader@example.com" and message["From"] == "nexus@example.com"
    assert "fixture-token" in message.get_content()


@pytest.mark.parametrize("status", ["delivered", "queued", "permanent_bounces"])
async def test_cloudflare_delivery_receipt(settings_factory, status):
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

    sender = AuthProviders(
        settings_factory(
            mail_provider="cloudflare",
            mail_from="sender@example.com",
            cloudflare_email_api_token="example-token",
            cloudflare_email_account_id=account,
        ),
        transport=httpx.MockTransport(provider),
    )
    if status == "permanent_bounces":
        # A synchronous bounce is final: the worker drops the message instead of retrying it.
        with pytest.raises(MailRejected):
            await sender.send(recipient, "reset", "verification-token", uuid4())
    else:
        await sender.send(recipient, "reset", "verification-token", uuid4())


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
async def test_turnstile_denials(settings_factory, code, response, expected):
    sender = AuthProviders(
        settings_factory(
            turnstile_secret_key="test-secret",
            turnstile_site_key="test-site",
            turnstile_hostname="console.example.com",
        ),
        transport=httpx.MockTransport(lambda req: httpx.Response(code, json=response)),
    )
    with pytest.raises(IdentityError) as rejected:
        await sender.verify(SecretStr("test-response"), "login")
    assert rejected.value.status == expected


def resend(settings_factory, response):
    return AuthProviders(
        settings_factory(
            mail_provider="resend", mail_from="sender@example.com", resend_api_key="example-key"
        ),
        transport=httpx.MockTransport(lambda request: response),
    )


@pytest.mark.parametrize("status", [400, 404, 422])
async def test_resend_client_errors_reject_the_message(settings_factory, status):
    sender = resend(settings_factory, httpx.Response(status, json={"name": "validation_error"}))
    with pytest.raises(MailRejected) as rejected:
        await sender.send("reader@example.com", "bind", "verification-token", uuid4())
    assert rejected.value.status == status


@pytest.mark.parametrize("status", [401, 403, 409, 429, 500, 503])
async def test_resend_credential_and_transient_errors_stay_retryable(settings_factory, status):
    sender = resend(settings_factory, httpx.Response(status, json={}))
    with pytest.raises(httpx.HTTPStatusError):
        await sender.send("reader@example.com", "bind", "verification-token", uuid4())


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
async def test_smtp_only_final_recipient_or_message_refusals_reject(
    settings_factory, error, permanent
):
    sender = AuthProviders(
        settings_factory(mail_provider="smtp", mail_from="nexus@example.com", smtp_host="smtp")
    )
    with patch.object(sender, "smtp_send", side_effect=error):
        with pytest.raises(MailRejected if permanent else type(error)):
            await sender.send("reader@example.com", "reset", "fixture-token", uuid4())


async def test_registration_notice_carries_no_token(settings_factory):
    sent = []

    def provider(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "receipt"})

    sender = AuthProviders(
        settings_factory(
            mail_provider="resend", mail_from="sender@example.com", resend_api_key="example-key"
        ),
        transport=httpx.MockTransport(provider),
    )
    await sender.send("reader@example.com", "exists", "nxe_" + "a" * 43, uuid4())
    assert "nxe_" not in sent[0]["text"] and "already taken" in sent[0]["text"]


@pytest.mark.parametrize(
    ("configured", "response", "reason"),
    [
        (True, {"success": False, "error-codes": ["invalid-input-secret"]}, "invalid_secret"),
        (True, {"success": False, "error-codes": ["missing-input-secret"]}, "invalid_secret"),
        (False, None, "not_configured"),
    ],
)
async def test_turnstile_configuration_faults_stay_server_side(
    settings_factory, caplog, configured, response, reason
):
    caplog.set_level("ERROR", logger="nexus.auth")
    keys = (
        dict(
            turnstile_secret_key="test-secret",
            turnstile_site_key="test-site",
            turnstile_hostname="console.example.com",
        )
        if configured
        else {}
    )
    sender = AuthProviders(
        settings_factory(**keys),
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=response)),
    )
    with pytest.raises(IdentityError) as rejected:
        await sender.verify(SecretStr("test-response"), "register")
    assert (rejected.value.code, rejected.value.status) == ("turnstile_unavailable", 503)
    assert "credential" not in str(rejected.value).lower()
    assert [r.event_fields["outcome"] for r in caplog.records] == [reason]
