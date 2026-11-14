import json
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from nexus.modules.identity.domain.errors import IdentityError
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
        with pytest.raises(RuntimeError):
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
