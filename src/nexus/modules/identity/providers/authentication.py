import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

import httpx

from nexus.shared.telemetry import event, request_id

from ..domain.email import mailbox
from ..domain.errors import IdentityError, MailRejected

logger = logging.getLogger("nexus.auth")

# HTTP statuses that refuse the message itself. 401/403 point at our credentials or sender
# domain, and 408/409/425/429 are transient; those keep the message queued and the worker
# reports the provider as unavailable.
RETRYABLE_CLIENT_ERRORS = frozenset({401, 403, 408, 409, 425, 429})


def permanent_status(status: int) -> bool:
    return 400 <= status < 500 and status not in RETRYABLE_CLIENT_ERRORS


def turnstile_unavailable(reason: str) -> IdentityError:
    # Anonymous callers learn only that the check is unavailable; the cause goes to the log.
    event(
        logger, logging.ERROR, "turnstile.unavailable", request_id=request_id.get(), outcome=reason
    )
    return IdentityError("turnstile_unavailable", "Turnstile is unavailable", 503)


class AuthProviders:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.name = settings.mail_provider
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
            raise turnstile_unavailable("not_configured")
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
                if not isinstance(result, dict):
                    raise ValueError
                errors = result.get("error-codes", [])
                if not isinstance(errors, list) or "internal-error" in errors:
                    raise ValueError
        except httpx.HTTPError:
            raise turnstile_unavailable("transport") from None
        except ValueError:
            raise turnstile_unavailable("malformed_response") from None
        if "missing-input-secret" in errors or "invalid-input-secret" in errors:
            raise turnstile_unavailable("invalid_secret")
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

    @staticmethod
    def failure_status(error):
        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code
        if isinstance(error, smtplib.SMTPResponseException):
            return error.smtp_code
        if isinstance(error, smtplib.SMTPRecipientsRefused) and error.recipients:
            return next(iter(error.recipients.values()))[0]
        return None

    async def send(self, recipient, purpose, token, delivery_id, *, site_name="Nexus"):
        if not self.mail_ready:
            raise RuntimeError("Identity mail provider unavailable")
        # The site name comes from the site policy, which only administrators and policy files
        # write; it is never text the requester typed.
        subject = {
            "register": "Confirm your {} registration",
            "bind": "Verify your {} email",
            "reset": "Reset your {} password",
            "exists": "Your {} registration request",
        }[purpose].format(site_name)
        if purpose == "exists":
            # Fixed text with no token and nothing the requester typed.
            body = (
                f"{subject}\n\nSomeone asked to register a {site_name} account with this "
                "address, but the requested login is already taken. If this was you, sign in or "
                "reset your password instead. Otherwise you can ignore this message.\n"
            )
        else:
            body = f"{subject}\n\nUse this one-time token within 15 minutes:\n\n{token}\n"
        if self.settings.mail_provider == "smtp":
            try:
                await asyncio.to_thread(self.smtp_send, recipient, subject, body, delivery_id)
            except smtplib.SMTPRecipientsRefused as error:
                codes = [code for code, _ in error.recipients.values()]
                if codes and all(code >= 500 for code in codes):
                    raise MailRejected(codes[0]) from None
                raise
            except smtplib.SMTPDataError as error:
                if error.smtp_code >= 500:
                    raise MailRejected(error.smtp_code) from None
                raise
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
            if permanent_status(response.status_code):
                raise MailRejected(response.status_code)
            response.raise_for_status()
            result = response.json()
        if self.settings.mail_provider == "resend":
            if not result.get("id"):
                raise RuntimeError("Missing Resend receipt")
        else:
            accepted = result.get("result") or {}
            if recipient in (accepted.get("permanent_bounces") or []):
                raise MailRejected("permanent_bounce")
            if result.get("success") is not True or recipient not in (
                accepted.get("queued", []) + accepted.get("delivered", [])
            ):
                raise RuntimeError("Cloudflare did not accept recipient")
        return response.status_code
