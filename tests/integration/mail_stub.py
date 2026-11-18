"""Recorded identity mail and Turnstile stand-ins for integration tests."""

import json
import re
from collections import namedtuple
from urllib.parse import parse_qs

import httpx

from nexus.modules.identity.providers.authentication import SECRET_PROBE

PASSWORD = "integration-password-with-entropy"
API = "/api/v1"
MAIL_KEY = "test-mail-key"
TURNSTILE_SECRET = "test-secret"
SITE_KEY = "test-site-key"
Mail = namedtuple("Mail", "to token subject key")
MAIL = {
    "provider": "resend",
    "sender": "robot@example.com",
    "resend": {"api_key": MAIL_KEY},
}
TURNSTILE = {
    "site_key": SITE_KEY,
    "secret_key": TURNSTILE_SECRET,
    "hostname": "console.example.com",
}


def bearer(token, version=None):
    headers = {"Authorization": f"Bearer {token}"}
    if version is not None:
        headers["If-Match"] = f'"{version}"'
    return headers


def stub_provider(messages, challenges):
    """A Resend and Siteverify stand-in.

    Resend accepts the test key only. Siteverify knows the test secret only, rejects the probe
    token that checks a secret, and passes any other token as the action it names.
    """

    def provider(request):
        if request.url.host == "challenges.cloudflare.com":
            form = parse_qs(request.content.decode())
            if form["secret"] != [TURNSTILE_SECRET]:
                return httpx.Response(
                    200, json={"success": False, "error-codes": ["invalid-input-secret"]}
                )
            action = form["response"][0]
            if action == SECRET_PROBE:
                return httpx.Response(
                    200, json={"success": False, "error-codes": ["invalid-input-response"]}
                )
            challenges.append(action)
            return httpx.Response(
                200, json={"success": True, "hostname": "console.example.com", "action": action}
            )
        assert str(request.url) == "https://api.resend.com/emails"
        if request.headers["authorization"] != f"Bearer {MAIL_KEY}":
            return httpx.Response(401, json={"message": "API key is invalid"})
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

    return provider


async def put_settings(client, token, path, body):
    current = await client.get(API + path, headers=bearer(token))
    assert current.status_code == 200, current.text
    saved = await client.put(
        API + path, headers=bearer(token, current.json()["version"]), json=body
    )
    assert saved.status_code == 200, saved.text
    return saved.json()


async def install_mail(client, token, service, *, turnstile=True):
    """Store Resend and Turnstile settings through the API and record what the stubs receive.

    ``service`` is the identity service of the process under test; its provider calls go to the
    stubs. A Turnstile token passes when it equals the expected action.
    """
    messages, challenges = [], []
    service.providers.transport = httpx.MockTransport(stub_provider(messages, challenges))
    await put_settings(client, token, "/settings/mail", MAIL)
    if turnstile:
        await put_settings(client, token, "/settings/turnstile", TURNSTILE)
    return messages, challenges
