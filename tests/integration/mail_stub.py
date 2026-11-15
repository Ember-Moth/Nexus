"""Recorded identity mail and Turnstile stand-ins for integration tests."""

import json
import re
from collections import namedtuple
from urllib.parse import parse_qs

import httpx
from pydantic import SecretStr

from nexus.modules.identity.providers.authentication import AuthProviders

PASSWORD = "integration-password-with-entropy"
Mail = namedtuple("Mail", "to token subject key")


def install_mail(service, settings):
    """Send identity mail through a recorded Resend stub and accept any Turnstile action.

    A Turnstile token passes when it equals the expected action.
    """
    messages = []
    challenges = []

    def provider(request):
        if request.url.host == "challenges.cloudflare.com":
            form = parse_qs(request.content.decode())
            assert form["secret"] == ["test-secret"]
            action = form["response"][0]
            challenges.append(action)
            return httpx.Response(
                200, json={"success": True, "hostname": "console.example.com", "action": action}
            )
        assert str(request.url) == "https://api.resend.com/emails"
        assert request.headers["authorization"] == "Bearer test-mail-key"
        payload = json.loads(request.content)
        token = re.search(r"nx[vrb]_[a-zA-Z0-9_-]{43}", payload["text"])
        messages.append(
            Mail(
                payload["to"][0],
                token and token[0],
                payload["subject"],
                request.headers["Idempotency-Key"],
            )
        )
        return httpx.Response(200, json={"id": request.headers["Idempotency-Key"]})

    service.providers = AuthProviders(
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
    return messages, challenges
