"""Identity mail and Turnstile settings as stored, and as a process uses them.

The rows of ``identity.mail_policy`` and ``identity.turnstile_policy`` keep each secret encrypted
(``credential`` with its ``key_version``). A process decrypts them into ``MailTransport`` and
``TurnstileKeys``, which never leave it: they are not serialized, logged or returned.
"""

from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

MAIL_PROVIDERS = ("smtp", "resend", "cloudflare")
# The authentication switches that need each provider while they are on.
MAIL_SWITCHES = ("registration", "email_verification")
TURNSTILE_SWITCHES = ("turnstile",)


@dataclass(frozen=True)
class StoredMail:
    provider: str | None
    sender: str | None
    smtp_host: str | None
    smtp_port: int
    smtp_security: str
    smtp_username: str | None
    cloudflare_account_id: str | None
    credential: bytes | None = field(repr=False)
    key_version: int | None
    version: int
    managed_sha256: str | None
    managed_at: datetime | None


@dataclass(frozen=True)
class StoredTurnstile:
    site_key: str | None
    hostname: str | None
    credential: bytes | None = field(repr=False)
    key_version: int | None
    version: int
    managed_sha256: str | None
    managed_at: datetime | None


@dataclass(frozen=True)
class MailTransport:
    """A configured mail provider with its secret in clear."""

    provider: str
    sender: str
    secret: str | None = field(default=None, repr=False)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_security: str = "starttls"
    smtp_username: str | None = None
    cloudflare_account_id: str | None = None


@dataclass(frozen=True)
class TurnstileKeys:
    """A usable Turnstile configuration: the secret in clear and the hostname Siteverify reports."""

    site_key: str
    secret: str = field(repr=False)
    hostname: str


@dataclass(frozen=True)
class ProviderState:
    """What one process uses for identity mail and Turnstile; None when not usable."""

    mail: MailTransport | None
    turnstile: TurnstileKeys | None


def console_host(console_url: str | None) -> str | None:
    """The host name of the site's console address, the default Turnstile hostname."""
    if not console_url:
        return None
    return urlsplit(console_url).hostname


def turnstile_hostname(stored: StoredTurnstile, console_url: str | None) -> str | None:
    return stored.hostname or console_host(console_url)
