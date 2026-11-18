import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

import httpx

from nexus.shared.telemetry import event, request_id

from ..domain.delivery import MailTransport, TurnstileKeys
from ..domain.errors import IdentityError, MailRejected

logger = logging.getLogger("nexus.auth")

SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
# Sent with a secret to check it: Siteverify answers invalid-input-response for a valid secret
# and invalid-input-secret for one it does not know. Never a real challenge token.
SECRET_PROBE = "nexus-secret-check"
# HTTP statuses that refuse the message itself. 401/403 point at our credentials or sender
# domain, and 408/409/425/429 are transient; those keep the message queued and the worker
# reports the provider as unavailable.
RETRYABLE_CLIENT_ERRORS = frozenset({401, 403, 408, 409, 425, 429})
SUMMARY_LENGTH = 300


def permanent_status(status: int) -> bool:
    return 400 <= status < 500 and status not in RETRYABLE_CLIENT_ERRORS


def turnstile_unavailable(reason: str) -> IdentityError:
    # Anonymous callers learn only that the check is unavailable; the cause goes to the log.
    event(
        logger, logging.ERROR, "turnstile.unavailable", request_id=request_id.get(), outcome=reason
    )
    return IdentityError("turnstile_unavailable", "Turnstile is unavailable", 503)


class ProviderFailure(Exception):
    """A provider answer that is neither an acceptance nor a refusal of the message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def provider_message(response: httpx.Response) -> str:
    """The provider's own explanation of an error answer, as Resend and Cloudflare send it."""
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase
    if isinstance(body, dict):
        if isinstance(body.get("message"), str):
            return body["message"]
        errors = body.get("errors")
        if isinstance(errors, list):
            parts = [str(e.get("message")) for e in errors if isinstance(e, dict)]
            if parts:
                return "; ".join(parts)
    return response.reason_phrase


class AuthProviders:
    """Mail delivery and Turnstile Siteverify; settings come with every call."""

    def __init__(self, *, transport=None):
        self.transport = transport

    async def siteverify(self, secret: str, token: str) -> dict:
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=5) as client:
                response = await client.post(SITEVERIFY, data={"secret": secret, "response": token})
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
        return result

    async def verify(self, keys: TurnstileKeys | None, token, action):
        if keys is None:
            raise turnstile_unavailable("not_configured")
        if not token:
            raise IdentityError("turnstile_required", "Complete the Turnstile challenge", 403)
        result = await self.siteverify(keys.secret, token.get_secret_value())
        errors = result.get("error-codes", [])
        if "missing-input-secret" in errors or "invalid-input-secret" in errors:
            raise turnstile_unavailable("invalid_secret")
        if (
            result.get("success") is not True
            or result.get("action") != action
            or (result.get("hostname") != keys.hostname)
        ):
            raise IdentityError("turnstile_rejected", "Turnstile challenge rejected", 403)

    async def secret_valid(self, secret: str) -> bool:
        """Whether Siteverify knows this secret, asked with a token that cannot be valid."""
        errors = (await self.siteverify(secret, SECRET_PROBE)).get("error-codes", [])
        return not ({"missing-input-secret", "invalid-input-secret"} & set(errors))

    @staticmethod
    def smtp_send(transport: MailTransport, recipient, subject, body, delivery_id):
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = transport.sender, recipient, subject
        message["Message-ID"] = f"<{delivery_id}@{transport.sender.rsplit('@', 1)[1]}>"
        message.set_content(body)
        context = ssl.create_default_context()
        if transport.smtp_security == "tls":
            connection = smtplib.SMTP_SSL(
                transport.smtp_host, transport.smtp_port, timeout=8, context=context
            )
        else:
            connection = smtplib.SMTP(transport.smtp_host, transport.smtp_port, timeout=8)
        with connection:
            if transport.smtp_security == "starttls":
                connection.starttls(context=context)
            if transport.smtp_username:
                connection.login(transport.smtp_username, transport.secret)
            connection.send_message(message)

    @staticmethod
    def failure_status(error):
        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code
        if isinstance(error, ProviderFailure):
            return error.status
        if isinstance(error, MailRejected):
            return error.status
        if isinstance(error, smtplib.SMTPResponseException):
            return error.smtp_code
        if isinstance(error, smtplib.SMTPRecipientsRefused) and error.recipients:
            return next(iter(error.recipients.values()))[0]
        return None

    @staticmethod
    def failure_summary(error, transport: MailTransport) -> str:
        """A short explanation of a failed delivery for administrators; never a credential."""
        if isinstance(error, smtplib.SMTPResponseException):
            reply = error.smtp_error
            text = reply.decode(errors="replace") if isinstance(reply, bytes) else str(reply)
            summary = f"SMTP {error.smtp_code}: {text}"
        elif isinstance(error, smtplib.SMTPRecipientsRefused):
            code, reply = next(iter(error.recipients.values()), (None, b""))
            summary = f"SMTP {code}: {reply.decode(errors='replace')} (recipient refused)"
        elif isinstance(error, ProviderFailure | MailRejected):
            summary = str(error)
        elif isinstance(error, httpx.HTTPError):
            summary = f"{type(error).__name__}: could not reach the provider"
        elif isinstance(error, OSError | smtplib.SMTPException):
            summary = f"{type(error).__name__}: {error}"
        else:
            summary = type(error).__name__
        if transport.secret:
            summary = summary.replace(transport.secret, "[redacted]")
        summary = " ".join(summary.split())
        return summary[:SUMMARY_LENGTH]

    async def send(
        self, transport: MailTransport | None, recipient, purpose, token, delivery_id, *, site_name
    ):
        # The site name comes from the site policy, which only administrators and policy files
        # write; it is never text the requester typed.
        subject = {
            "register": "Confirm your {} registration",
            "bind": "Verify your {} email",
            "reset": "Reset your {} password",
            "exists": "Your {} registration request",
            "test": "Test message from {}",
        }[purpose].format(site_name)
        if purpose == "exists":
            # Fixed text with no token and nothing the requester typed.
            body = (
                f"{subject}\n\nSomeone asked to register a {site_name} account with this "
                "address, but the requested login is already taken. If this was you, sign in or "
                "reset your password instead. Otherwise you can ignore this message.\n"
            )
        elif purpose == "test":
            body = (
                f"{subject}\n\nAn administrator sent this message to check the identity mail "
                f"settings of {site_name}. No action is needed.\n"
            )
        else:
            body = f"{subject}\n\nUse this one-time token within 15 minutes:\n\n{token}\n"
        return await self.deliver(transport, recipient, subject, body, delivery_id)

    async def deliver(self, transport: MailTransport | None, recipient, subject, body, delivery_id):
        if transport is None:
            raise RuntimeError("Identity mail provider unavailable")
        if transport.provider == "smtp":
            try:
                await asyncio.to_thread(
                    self.smtp_send, transport, recipient, subject, body, delivery_id
                )
            except smtplib.SMTPRecipientsRefused as error:
                codes = [code for code, _ in error.recipients.values()]
                if codes and all(code >= 500 for code in codes):
                    summary = self.failure_summary(error, transport)
                    raise MailRejected(codes[0], summary) from None
                raise
            except smtplib.SMTPDataError as error:
                if error.smtp_code >= 500:
                    raise MailRejected(
                        error.smtp_code, self.failure_summary(error, transport)
                    ) from None
                raise
            return 250
        payload = {"from": transport.sender, "subject": subject, "text": body}
        if transport.provider == "resend":
            url = "https://api.resend.com/emails"
            headers = {"Idempotency-Key": str(delivery_id)}
            payload["to"] = [recipient]
        else:
            account = transport.cloudflare_account_id
            url = f"https://api.cloudflare.com/client/v4/accounts/{account}/email/sending/send"
            headers = {}
            payload["to"] = recipient
        headers["Authorization"] = "Bearer " + transport.secret
        async with httpx.AsyncClient(transport=self.transport, timeout=8) as client:
            response = await client.post(url, json=payload, headers=headers)
            if permanent_status(response.status_code):
                raise MailRejected(
                    response.status_code,
                    f"HTTP {response.status_code}: {provider_message(response)}",
                )
            if response.status_code >= 400:
                raise ProviderFailure(
                    response.status_code,
                    f"HTTP {response.status_code}: {provider_message(response)}",
                )
            result = response.json()
        if transport.provider == "resend":
            if not result.get("id"):
                raise ProviderFailure(response.status_code, "Resend returned no message ID")
        else:
            accepted = result.get("result") or {}
            if recipient in (accepted.get("permanent_bounces") or []):
                raise MailRejected("permanent_bounce", "the recipient address bounced permanently")
            if result.get("success") is not True or recipient not in (
                accepted.get("queued", []) + accepted.get("delivered", [])
            ):
                raise ProviderFailure(
                    response.status_code, "Cloudflare did not accept the recipient"
                )
        return response.status_code
