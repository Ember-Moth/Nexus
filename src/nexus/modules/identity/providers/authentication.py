import asyncio
import smtplib
import ssl
from email.message import EmailMessage

import httpx

from ..domain.email import mailbox
from ..domain.errors import IdentityError


class AuthProviders:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport
        self.site_key = settings.turnstile_site_key
        self.turnstile_ready = bool(
            self.site_key and settings.turnstile_secret_key and settings.turnstile_hostname
        )
        self.sender = mailbox(settings.mail_from) if settings.mail_from else None
        self.mail_ready = bool(
            self.sender
            and {
                "smtp": settings.smtp_host
                and bool(settings.smtp_username) == bool(settings.smtp_password),
                "cloudflare": settings.cloudflare_email_api_token
                and settings.cloudflare_email_account_id,
                "resend": settings.resend_api_key,
            }.get(settings.mail_provider)
        )
        if settings.mail_provider and not self.mail_ready:
            raise ValueError("Incomplete identity mail provider configuration")

    async def verify(self, token, action):
        if not self.turnstile_ready:
            raise IdentityError("turnstile_unavailable", "Turnstile is unavailable", 503)
        if not token:
            raise IdentityError("turnstile_required", "Complete the Turnstile challenge", 403)
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=5) as client:
                response = await client.post(
                    "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                    data={
                        "secret": self.settings.turnstile_secret_key.get_secret_value(),
                        "response": token.get_secret_value(),
                    },
                )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict) or set(result.get("error-codes", [])) & {
                    "internal-error",
                    "missing-input-secret",
                    "invalid-input-secret",
                }:
                    raise ValueError
        except httpx.HTTPError, ValueError:
            raise IdentityError("turnstile_unavailable", "Turnstile is unavailable", 503) from None
        if (
            result.get("success") is not True
            or result.get("action") != action
            or (result.get("hostname") != self.settings.turnstile_hostname)
        ):
            raise IdentityError("turnstile_rejected", "Turnstile challenge rejected", 403)

    def smtp_send(self, recipient, subject, body, delivery_id):
        settings = self.settings
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = self.sender, recipient, subject
        message["Message-ID"] = f"<{delivery_id}@{self.sender.rsplit('@', 1)[1]}>"
        message.set_content(body)
        context = ssl.create_default_context()
        if settings.smtp_security == "tls":
            connection = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=8, context=context
            )
        else:
            connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=8)
        with connection:
            if settings.smtp_security == "starttls":
                connection.starttls(context=context)
            if settings.smtp_username:
                connection.login(settings.smtp_username, settings.smtp_password.get_secret_value())
            connection.send_message(message)

    async def send(self, recipient, purpose, token, delivery_id):
        if not self.mail_ready:
            raise RuntimeError("Identity mail provider unavailable")
        subject = {
            "register": "Confirm your Nexus registration",
            "bind": "Verify your Nexus email",
            "reset": "Reset your Nexus password",
        }[purpose]
        body = f"{subject}\n\nUse this one-time token within 15 minutes:\n\n{token}\n"
        if self.settings.mail_provider == "smtp":
            await asyncio.to_thread(self.smtp_send, recipient, subject, body, delivery_id)
            return
        payload = {"from": self.sender, "subject": subject, "text": body}
        if self.settings.mail_provider == "resend":
            key = self.settings.resend_api_key
            url = "https://api.resend.com/emails"
            headers = {"Idempotency-Key": str(delivery_id)}
            payload["to"] = [recipient]
        else:
            key = self.settings.cloudflare_email_api_token
            account = self.settings.cloudflare_email_account_id
            url = f"https://api.cloudflare.com/client/v4/accounts/{account}/email/sending/send"
            headers = {}
            payload["to"] = recipient
        headers["Authorization"] = "Bearer " + key.get_secret_value()
        async with httpx.AsyncClient(transport=self.transport, timeout=8) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()
        if self.settings.mail_provider == "resend":
            if not result.get("id"):
                raise RuntimeError("Missing Resend receipt")
        else:
            accepted = result.get("result") or {}
            if result.get("success") is not True or recipient not in (
                accepted.get("queued", []) + accepted.get("delivered", [])
            ):
                raise RuntimeError("Cloudflare did not accept recipient")
