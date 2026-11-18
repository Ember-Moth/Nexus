"""Identity mail and Turnstile settings: stored encrypted, edited by platform administrators.

Each policy is a singleton row with one secret at most: the selected mail provider's password,
API key or token, and the Turnstile secret key. Secrets are encrypted with the application key
ring (purpose ``identity-provider``) and bound to their policy and provider, so a ciphertext
cannot be moved to another column or provider. Every write re-encrypts the stored secret with
the current key, which is how a key rotation reaches these rows.

Processes read the decrypted settings through a short-lived copy (``POLICY_TTL_SECONDS``), so a
change applies to every API and Worker process within seconds; the writer applies it at once.
Identity mail itself is sent by the Worker (``dispatch_email``); the test message is sent by the
API process that receives the request, so its outcome can be returned.
"""

import logging
from dataclasses import asdict
from uuid import UUID, uuid4

from nexus.shared.errors import ServiceError
from nexus.shared.policy import changes
from nexus.shared.telemetry import event, request_id
from nexus.shared.views import ManagedSource

from ..domain.delivery import (
    MAIL_SWITCHES,
    TURNSTILE_SWITCHES,
    MailTransport,
    ProviderState,
    StoredMail,
    StoredTurnstile,
    TurnstileKeys,
    console_host,
    turnstile_hostname,
)
from ..domain.email import mailbox
from ..domain.errors import IdentityError
from .schemas import (
    CloudflareMailView,
    MailPolicyView,
    MailSettings,
    MailTestResult,
    ResendView,
    SmtpView,
    TurnstilePolicyView,
    TurnstileSettings,
)

logger = logging.getLogger("nexus.mail")

MAIL_GROUP = "identity.mail_policy"
TURNSTILE_GROUP = "identity.turnstile_policy"
# Audit resources of the singleton policies; also the first half of each secret's binding.
MAIL_RESOURCE = UUID(int=2)
TURNSTILE_RESOURCE = UUID(int=3)
# The second half: which credential a ciphertext is.
CREDENTIALS = {
    "smtp": UUID(int=1),
    "resend": UUID(int=2),
    "cloudflare": UUID(int=3),
    "turnstile": UUID(int=4),
}
# The secret field of each mail provider's section.
SECRET_FIELDS = {"smtp": "password", "resend": "api_key", "cloudflare": "api_token"}


def managed_externally(name: str) -> IdentityError:
    return IdentityError(
        "policy_managed_externally", f"The {name} settings are managed by a policy file", 409
    )


def mail_values(stored: StoredMail) -> dict:
    """The stored settings in the shape of the PUT body, without the secret."""
    provider = stored.provider
    return {
        "provider": provider,
        "sender": stored.sender,
        "smtp": {
            "host": stored.smtp_host,
            "port": stored.smtp_port,
            "security": stored.smtp_security,
            "username": stored.smtp_username,
        }
        if provider == "smtp"
        else None,
        "resend": {} if provider == "resend" else None,
        "cloudflare": {"account_id": stored.cloudflare_account_id}
        if provider == "cloudflare"
        else None,
    }


def mail_facts(row: dict, *, replaced: bool = False) -> dict:
    """What an audit record shows of the mail settings: never a secret, only whether one is set."""
    provider = row["provider"]
    smtp = provider == "smtp"
    facts = {
        "provider": provider,
        "sender": row["sender"],
        "smtp.host": row["smtp_host"],
        "smtp.port": row["smtp_port"] if smtp else None,
        "smtp.security": row["smtp_security"] if smtp else None,
        "smtp.username": row["smtp_username"],
        "cloudflare.account_id": row["cloudflare_account_id"],
    }
    for name, field in SECRET_FIELDS.items():
        stored = provider == name and row["credential"] is not None
        facts[f"{name}.{field}"] = ("replaced" if replaced else "set") if stored else None
    return facts


def turnstile_values(stored: StoredTurnstile) -> dict:
    return {"site_key": stored.site_key, "hostname": stored.hostname}


def turnstile_facts(row: dict, *, replaced: bool = False) -> dict:
    stored = row["credential"] is not None
    return {
        "site_key": row["site_key"],
        "hostname": row["hostname"],
        "secret_key": ("replaced" if replaced else "set") if stored else None,
    }


def stored_row(stored) -> dict:
    values = asdict(stored)
    for name in ("version", "managed_sha256", "managed_at"):
        values.pop(name)
    return values


def switches_on(policy, names) -> list[str]:
    return [name for name in names if getattr(policy, name)]


def managed_source(stored) -> ManagedSource | None:
    if stored.managed_sha256 is None:
        return None
    return ManagedSource(file_sha256=stored.managed_sha256, imported_at=stored.managed_at)


class ProviderSettings:
    """Shares the identity service authentication and transaction primitives."""

    # Runtime copies -------------------------------------------------------------------------

    def _decrypt(self, resource, provider, credential, version) -> str:
        return self._provider_cipher.decrypt(credential, resource, CREDENTIALS[provider], version)

    def _mail_transport(self, stored: StoredMail) -> MailTransport | None:
        if stored.provider is None:
            return None
        secret = None
        if stored.credential is not None:
            try:
                secret = self._decrypt(
                    MAIL_RESOURCE, stored.provider, stored.credential, stored.key_version
                )
            except ServiceError:
                event(
                    logger,
                    logging.WARNING,
                    "mail.credential_unavailable",
                    provider=stored.provider,
                    key_version=stored.key_version,
                )
                return None
        return MailTransport(
            provider=stored.provider,
            sender=stored.sender,
            secret=secret,
            smtp_host=stored.smtp_host,
            smtp_port=stored.smtp_port,
            smtp_security=stored.smtp_security,
            smtp_username=stored.smtp_username,
            cloudflare_account_id=stored.cloudflare_account_id,
        )

    def _turnstile_keys(self, stored: StoredTurnstile, console_url) -> TurnstileKeys | None:
        hostname = turnstile_hostname(stored, console_url)
        if stored.site_key is None or hostname is None:
            return None
        try:
            secret = self._decrypt(
                TURNSTILE_RESOURCE, "turnstile", stored.credential, stored.key_version
            )
        except ServiceError:
            event(
                logger,
                logging.WARNING,
                "turnstile.credential_unavailable",
                key_version=stored.key_version,
            )
            return None
        return TurnstileKeys(stored.site_key, secret, hostname)

    async def _load_providers(self) -> ProviderState:
        async with self._transactions() as tx:
            mail = await tx.providers.mail()
            turnstile = await tx.providers.turnstile()
            site = await tx.site.policy()
        return ProviderState(
            self._mail_transport(mail), self._turnstile_keys(turnstile, site.console_url)
        )

    async def mail_transport(self) -> MailTransport | None:
        return (await self._providers.get()).mail

    async def turnstile_keys(self) -> TurnstileKeys | None:
        return (await self._providers.get()).turnstile

    async def require_mail(self) -> MailTransport:
        transport = await self.mail_transport()
        if transport is None:
            raise IdentityError("mail_unavailable", "Email delivery is not configured", 503)
        return transport

    # Targets of a write ---------------------------------------------------------------------

    def _secret(
        self, resource, provider, supplied, value, current, same
    ) -> tuple[str | None, bool]:
        """The secret a write stores, in clear, and whether it replaces a different stored one.

        ``supplied`` says whether the request named the field (a value or null); ``same`` whether
        the stored secret belongs to the same provider.
        """
        stored = current.credential if same else None

        def previous():
            return self._decrypt(resource, provider, stored, current.key_version)

        if supplied:
            if value is None:
                return None, False
            new = value.get_secret_value()
            if stored is None:
                return new, False
            try:
                if previous() == new:
                    return new, False
            except ServiceError:
                pass
            return new, True
        if stored is None:
            return None, False
        try:
            return previous(), False
        except ServiceError:
            raise IdentityError(
                "credential_unavailable",
                "The stored secret cannot be decrypted with the current keys; enter it again",
                503,
            ) from None

    def _encrypted(self, resource, provider, secret) -> dict:
        if secret is None:
            return {"credential": None, "key_version": None}
        version, ciphertext = self._provider_cipher.encrypt(secret, resource, CREDENTIALS[provider])
        return {"credential": ciphertext, "key_version": version}

    def _mail_target(self, body: MailSettings, current: StoredMail, error) -> tuple[dict, bool]:
        """The row a write stores, and whether it replaces the secret with a new value."""
        provider = body.provider
        row = {
            "provider": provider,
            "sender": body.sender,
            "smtp_host": None,
            "smtp_port": 587,
            "smtp_security": "starttls",
            "smtp_username": None,
            "cloudflare_account_id": None,
            "credential": None,
            "key_version": None,
        }
        if provider is None:
            return row, False
        section = getattr(body, provider)
        if provider == "smtp":
            row.update(
                smtp_host=section.host,
                smtp_port=section.port,
                smtp_security=section.security,
                smtp_username=section.username,
            )
        elif provider == "cloudflare":
            row["cloudflare_account_id"] = section.account_id
        field = SECRET_FIELDS[provider]
        supplied = section is not None and field in section.model_fields_set
        value = getattr(section, field) if supplied else None
        if provider == "smtp" and section.username is None:
            # A server without authentication: there is no password to keep.
            if value is not None:
                raise error("invalid_mail_settings", "An SMTP password needs a username", 422)
            return row, False
        secret, replaced = self._secret(
            MAIL_RESOURCE, provider, supplied, value, current, current.provider == provider
        )
        if secret is None:
            required = {"smtp": "SMTP password", "resend": "API key", "cloudflare": "API token"}
            raise error("invalid_mail_settings", f"The {required[provider]} is required", 422)
        row.update(self._encrypted(MAIL_RESOURCE, provider, secret))
        return row, replaced

    def _turnstile_target(
        self, body: TurnstileSettings, current: StoredTurnstile, console_url, error
    ) -> tuple[dict, bool]:
        row = {"site_key": body.site_key, "hostname": body.hostname}
        if body.site_key is None:
            return {**row, "credential": None, "key_version": None}, False
        supplied = "secret_key" in body.model_fields_set
        secret, replaced = self._secret(
            TURNSTILE_RESOURCE,
            "turnstile",
            supplied,
            body.secret_key,
            current,
            current.site_key is not None,
        )
        if secret is None:
            raise error("invalid_turnstile_settings", "A secret key is required", 422)
        if (body.hostname or console_host(console_url)) is None:
            raise error(
                "invalid_turnstile_settings",
                "Set the hostname, or the console URL of the site it defaults to",
                422,
            )
        return {**row, **self._encrypted(TURNSTILE_RESOURCE, "turnstile", secret)}, replaced

    # Views ----------------------------------------------------------------------------------

    def _mail_view(self, stored: StoredMail, policy) -> MailPolicyView:
        provider = stored.provider
        stored_secret = stored.credential is not None
        return MailPolicyView(
            provider=provider,
            sender=stored.sender,
            smtp=SmtpView(
                host=stored.smtp_host,
                port=stored.smtp_port,
                security=stored.smtp_security,
                username=stored.smtp_username,
                password_set=stored_secret,
            )
            if provider == "smtp"
            else None,
            resend=ResendView(api_key_set=stored_secret) if provider == "resend" else None,
            cloudflare=CloudflareMailView(
                account_id=stored.cloudflare_account_id, api_token_set=stored_secret
            )
            if provider == "cloudflare"
            else None,
            available=self._mail_transport(stored) is not None,
            required_by=switches_on(policy, MAIL_SWITCHES),
            key_version=stored.key_version,
            managed=managed_source(stored),
            version=stored.version,
        )

    def _turnstile_view(self, stored: StoredTurnstile, site, policy) -> TurnstilePolicyView:
        return TurnstilePolicyView(
            site_key=stored.site_key,
            secret_key_set=stored.credential is not None,
            hostname=stored.hostname,
            effective_hostname=turnstile_hostname(stored, site.console_url)
            if stored.site_key
            else None,
            available=self._turnstile_keys(stored, site.console_url) is not None,
            required_by=switches_on(policy, TURNSTILE_SWITCHES),
            key_version=stored.key_version,
            managed=managed_source(stored),
            version=stored.version,
        )

    # Mail -----------------------------------------------------------------------------------

    async def mail_policy(self, token) -> MailPolicyView:
        async with self._access.authenticated(token) as (tx, actor):
            self._access.platform_admin(actor)
            return self._mail_view(await tx.providers.mail(), await tx.email.policy())

    async def update_mail_policy(self, token, body: MailSettings, version) -> MailPolicyView:
        async with self._access.authenticated(token) as (tx, actor):
            self._access.platform_admin(actor)
            await tx.providers.lock_policies("mail_policy")
            current = await tx.providers.mail()
            if current.managed_sha256 is not None:
                raise managed_externally("mail")
            if current.version != version:
                raise IdentityError("version_conflict", "Mail settings have changed", 412)
            row, replaced = self._mail_target(body, current, IdentityError)
            policy = await tx.email.policy()
            if row["provider"] is None and (on := switches_on(policy, MAIL_SWITCHES)):
                raise IdentityError(
                    "mail_in_use",
                    f"Turn off {' and '.join(on)} before removing the mail settings",
                    409,
                )
            result = await tx.providers.set_mail(row, version)
            if result is None:
                raise IdentityError("version_conflict", "Mail settings have changed", 412)
            await tx.audit.record(
                actor.user.id,
                "mail_policy.updated",
                MAIL_RESOURCE,
                details=changes(
                    mail_facts(stored_row(current)), mail_facts(row, replaced=replaced)
                ),
            )
        self._providers.invalidate()
        return self._mail_view(result, policy)

    async def send_test_mail(self, token, recipient: str, peer: str) -> MailTestResult:
        """Send one message with the stored settings and report the provider's answer."""
        address = mailbox(recipient)
        async with self._access.authenticated(token) as (tx, actor):
            self._access.platform_admin(actor)
            stored = await tx.providers.mail()
        transport = self._mail_transport(stored)
        if transport is None:
            raise IdentityError("mail_unavailable", "Email delivery is not configured", 503)
        await self._mail_budget(address, peer, f"user:{actor.user.id}")
        delivery_id = uuid4()
        site_name = (await self.site()).site_name
        try:
            status = await self.providers.send(
                transport, address, "test", None, delivery_id, site_name=site_name
            )
            result = MailTestResult(
                accepted=True, provider=transport.provider, provider_status=status, detail=None
            )
        except Exception as error:
            result = MailTestResult(
                accepted=False,
                provider=transport.provider,
                provider_status=self.providers.failure_status(error),
                detail=self.providers.failure_summary(error, transport),
            )
        async with self._transactions() as tx:
            await tx.audit.record(
                actor.user.id,
                "mail_policy.test_sent",
                MAIL_RESOURCE,
                details={"recipient": address, **result.model_dump(mode="json")},
            )
        event(
            logger,
            logging.INFO if result.accepted else logging.WARNING,
            "mail.test",
            request_id=request_id.get(),
            job_id=delivery_id,
            provider=transport.provider,
            provider_status=result.provider_status,
            outcome="accepted" if result.accepted else "failed",
        )
        return result

    # Turnstile ------------------------------------------------------------------------------

    async def turnstile_policy(self, token) -> TurnstilePolicyView:
        async with self._access.authenticated(token) as (tx, actor):
            self._access.platform_admin(actor)
            return self._turnstile_view(
                await tx.providers.turnstile(), await tx.site.policy(), await tx.email.policy()
            )

    async def update_turnstile_policy(
        self, token, body: TurnstileSettings, version
    ) -> TurnstilePolicyView:
        await self.require_platform_admin(token)
        if body.secret_key is not None:
            # Checked before anything is locked: Siteverify is a call across the internet.
            if not await self.providers.secret_valid(body.secret_key.get_secret_value()):
                raise IdentityError(
                    "invalid_turnstile_secret", "Siteverify does not accept this secret key", 422
                )
        async with self._access.authenticated(token) as (tx, actor):
            self._access.platform_admin(actor)
            await tx.providers.lock_policies("turnstile_policy")
            current = await tx.providers.turnstile()
            if current.managed_sha256 is not None:
                raise managed_externally("Turnstile")
            if current.version != version:
                raise IdentityError("version_conflict", "Turnstile settings have changed", 412)
            site = await tx.site.policy()
            row, replaced = self._turnstile_target(body, current, site.console_url, IdentityError)
            policy = await tx.email.policy()
            if row["site_key"] is None and policy.turnstile:
                raise IdentityError(
                    "turnstile_in_use", "Turn off Turnstile before removing its keys", 409
                )
            result = await tx.providers.set_turnstile(row, version)
            if result is None:
                raise IdentityError("version_conflict", "Turnstile settings have changed", 412)
            await tx.audit.record(
                actor.user.id,
                "turnstile_policy.updated",
                TURNSTILE_RESOURCE,
                details=changes(
                    turnstile_facts(stored_row(current)), turnstile_facts(row, replaced=replaced)
                ),
            )
        self._providers.invalidate()
        return self._turnstile_view(result, site, policy)

    async def _check_console(self, tx, console_url) -> None:
        """A console URL that the Turnstile hostname defaults to stays while Turnstile is on."""
        policy = await tx.email.policy()
        if not policy.turnstile:
            return
        stored = await tx.providers.turnstile()
        if self._turnstile_keys(stored, console_url) is None:
            raise IdentityError(
                "turnstile_in_use",
                "Turnstile uses the console host name; set its hostname before changing it",
                409,
            )
