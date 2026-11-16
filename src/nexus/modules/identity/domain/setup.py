"""The built-in administrator and the first-run setup it must complete.

Migration ``0047_builtin_admin`` creates the administrator with these defaults, and
``nexus setup reset`` restores them. Until the setup wizard has stored a new email and password,
the management API refuses everything except signing in and the wizard itself.
"""

from uuid import UUID

from .errors import IdentityError

BUILTIN_ADMIN_ID = UUID("00000000-0000-4000-8000-000000000001")
BUILTIN_ADMIN_LOGIN = "admin"
DEFAULT_ADMIN_EMAIL = "admin@example.com"
DEFAULT_ADMIN_PASSWORD = "password"
# Argon2id of DEFAULT_ADMIN_PASSWORD with the parameters of Argon2Passwords.
DEFAULT_ADMIN_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$yzLccdji9Va1kKs1NYFVnQ$"
    "Azl8a7QXycjkcYPyGwz9gIVuLqFtwLHlTObS0HVofSA"
)

# Documentation domains (RFC 2606, RFC 6761); email_validator already refuses .test, .invalid,
# .local, .localhost, .onion and .arpa.
RESERVED_DOMAINS = ("example.com", "example.net", "example.org", "example")


def setup_required() -> IdentityError:
    return IdentityError(
        "setup_required", "The built-in administrator must complete the setup wizard", 409
    )


def reserved_domain(address: str) -> bool:
    domain = address.rsplit("@", 1)[-1]
    return any(domain == name or domain.endswith("." + name) for name in RESERVED_DOMAINS)


def check_admin_email(address: str) -> str:
    """The administrator's new address: a real mailbox, not the default or a documentation one."""
    if address == DEFAULT_ADMIN_EMAIL or reserved_domain(address):
        raise IdentityError(
            "email_reserved", "Use an address you own, not a reserved example domain", 422
        )
    return address


def check_admin_password(password: str) -> None:
    if password == DEFAULT_ADMIN_PASSWORD:
        raise IdentityError("invalid_password", "Choose a password other than the default", 422)
