"""First-run setup of the built-in administrator, and the site policy it writes.

The migration creates the built-in administrator with a published default address and password.
Until it completes the wizard, ``require_setup_complete`` refuses every management call that the
API process does not explicitly leave open. The wizard stores the site description, a new
address and a new password in one transaction, marks setup complete and revokes every other
session of the administrator. ``reset_setup`` (``nexus setup reset`` on the host) restores the
defaults and locks the API again.
"""

from uuid import UUID

from nexus.shared.policy import ImportPlan, changes, plan

from ..domain.email import mailbox
from ..domain.errors import IdentityError, forbidden
from ..domain.setup import (
    BUILTIN_ADMIN_ID,
    BUILTIN_ADMIN_LOGIN,
    DEFAULT_ADMIN_EMAIL,
    check_admin_email,
    check_admin_password,
    setup_required,
)
from ..domain.tokens import digest_token
from .schemas import SetupStatus, SiteInfo, SitePolicyView

SITE_GROUP = "identity.site_policy"
# Audit resource of system changes to the singleton site policy.
SITE_RESOURCE = UUID(int=1)
RESET_ACTOR = "setup-reset"


def site_values(policy: SitePolicyView) -> dict:
    return SiteInfo.model_validate(
        policy.model_dump(include=set(SiteInfo.model_fields))
    ).model_dump(mode="json")


def invalid_verification_token() -> IdentityError:
    return IdentityError(
        "invalid_verification_token", "The verification token is invalid, used or expired", 422
    )


def token_digest(token: str) -> str:
    """Digest of a mailed address proof; a value that cannot be one is simply invalid."""
    try:
        return digest_token(token, "nxb_")
    except IdentityError:
        raise invalid_verification_token() from None


def managed_externally() -> IdentityError:
    return IdentityError(
        "policy_managed_externally", "The site policy is managed by a policy file", 409
    )


class SiteSetup:
    """Shares the identity service authentication and transaction primitives."""

    async def _read_site(self) -> SitePolicyView:
        async with self._transactions() as tx:
            return await tx.site.policy()

    async def require_setup_complete(self) -> None:
        """Refuse with ``setup_required`` until the built-in administrator finishes the wizard."""
        if (await self._site.get()).setup_completed_at is not None:
            return
        # Refusals read the row itself, so a finished wizard opens every process at once; a
        # reset reaches the others within the policy cache lifetime.
        current = await self._read_site()
        self._site.put(current)
        if current.setup_completed_at is None:
            raise setup_required()

    async def site(self) -> SitePolicyView:
        """The cached site policy, for mail and links built by this and other modules."""
        return await self._site.get()

    async def setup_status(self, token: str) -> SetupStatus:
        async with self._access.authenticated(token) as (tx, actor):
            site = await tx.site.policy()
        self._site.put(site)
        return SetupStatus(
            required=site.setup_completed_at is None,
            builtin_admin=actor.user.id == BUILTIN_ADMIN_ID,
            email_delivery_available=self.providers.mail_ready,
            site=site,
        )

    @staticmethod
    def _wizard(actor, site: SitePolicyView) -> None:
        if actor.user.id != BUILTIN_ADMIN_ID:
            raise forbidden()
        if site.setup_completed_at is not None:
            raise IdentityError("setup_completed", "Setup has already been completed", 409)

    async def request_setup_email(self, token: str, email: str, peer: str) -> None:
        """Mail a one-time token that proves the administrator's new address."""
        address = check_admin_email(mailbox(email))
        self.require_mail()
        await self._throttle("bind:" + peer, peer)
        async with self._access.authenticated(token) as (tx, actor):
            self._wizard(actor, await tx.site.policy())
        await self._mail_budget(address, peer, f"user:{actor.user.id}")
        async with self._access.authenticated(token) as (tx, actor):
            self._wizard(actor, await tx.site.policy(lock=True))
            if await tx.directory.email_taken(address, actor.user.id):
                raise IdentityError("email_in_use", "Email is already bound to an account", 409)
            job = await self.queue(tx, "bind", address, user_id=actor.user.id)
        self.log_queued(job, "bind")

    async def complete_setup(self, token: str, body) -> SetupStatus:
        address = check_admin_email(mailbox(body.email))
        password = body.password.get_secret_value()
        check_admin_password(password)
        async with self._access.authenticated(token) as (tx, actor):
            self._wizard(actor, await tx.site.policy())
        password_hash = await self._hash(password)
        async with self._access.authenticated(token) as (tx, actor):
            # The row lock serializes concurrent submissions; the later one finds it completed.
            site = await tx.site.policy(lock=True)
            self._wizard(actor, site)
            if site.managed is not None and body.site.model_dump(mode="json") != site_values(site):
                raise managed_externally()
            verified = self.providers.mail_ready
            if verified:
                if body.email_token is None:
                    raise IdentityError(
                        "email_verification_required",
                        "Verify the new address with the mailed token first",
                        422,
                    )
                proof = await tx.email.consume(
                    token_digest(body.email_token.get_secret_value()), "bind"
                )
                if not proof or proof["user_id"] != actor.user.id or proof["email"] != address:
                    raise invalid_verification_token()
            if await tx.directory.email_taken(address, actor.user.id):
                raise IdentityError("email_in_use", "Email is already bound to an account", 409)
            if verified:
                await tx.email.bind(actor.user.id, address)
            else:
                await tx.email.set_unverified(actor.user.id, address)
            # This session stays; every other one, including any opened with the default
            # password by someone else, ends here.
            await tx.directory.replace_password(
                actor.user.id, password_hash, keep_session=actor.session_id
            )
            completed = await tx.site.complete(body.site)
            await tx.audit.record(
                actor.user.id,
                "setup.completed",
                actor.user.id,
                details={
                    "email_verified": verified,
                    "site": changes(site_values(site), site_values(completed)),
                },
            )
        self._site.put(completed)
        return SetupStatus(
            required=False,
            builtin_admin=True,
            email_delivery_available=self.providers.mail_ready,
            site=completed,
        )

    async def reset_setup(self) -> dict:
        """Restore the built-in administrator's defaults and lock the management API again."""
        async with self._transactions() as tx:
            previous = await tx.site.policy(lock=True)
            await tx.directory.restore_builtin_admin()
            reopened = await tx.site.reopen()
            await tx.audit.record_system(
                "setup.reset",
                BUILTIN_ADMIN_ID,
                {"actor": RESET_ACTOR, "was_completed": previous.setup_completed_at is not None},
            )
        self._site.put(reopened)
        return {
            "user_id": str(BUILTIN_ADMIN_ID),
            "login": BUILTIN_ADMIN_LOGIN,
            "email": DEFAULT_ADMIN_EMAIL,
            "setup_required": True,
        }

    async def site_policy(self, token: str) -> SitePolicyView:
        async with self._access.authenticated(token) as (tx, actor):
            self._access.platform_admin(actor)
            return await tx.site.policy()

    async def update_site_policy(self, token: str, body: SiteInfo, version: int) -> SitePolicyView:
        async with self._access.authenticated(token) as (tx, actor):
            self._access.platform_admin(actor)
            current = await tx.site.policy(lock=True)
            if current.managed is not None:
                raise managed_externally()
            result = await tx.site.set_info(body, version) if current.version == version else None
            if result is None:
                raise IdentityError("version_conflict", "Site policy has changed", 412)
            await tx.audit.record(
                actor.user.id,
                "site_policy.updated",
                SITE_RESOURCE,
                details=changes(site_values(current), site_values(result)),
            )
        self._site.put(result)
        return result

    async def plan_site_import(self, mode, values, file_sha256) -> ImportPlan:
        """Plan the ``identity.site_policy`` group of a policy file; nothing is written yet.

        Only the site description is importable: setup completion belongs to the wizard and to
        ``nexus setup reset``, so a file naming it is rejected as an unknown field.
        """
        current = await self._read_site()
        target, found, skipped = plan(
            SITE_GROUP,
            mode,
            site_values(current),
            current.version,
            current.managed is not None,
            values,
            SiteInfo,
        )
        info = SiteInfo.model_validate(target)

        async def write(details):
            async with self._transactions() as tx:
                result = await tx.site.set_info(
                    info, current.version, managed=file_sha256 if mode == "managed" else None
                )
                if result is None:
                    raise IdentityError("version_conflict", "Site policy has changed", 412)
                await tx.audit.record_system(
                    "site_policy.updated", SITE_RESOURCE, {**details, **found}
                )
            self._site.put(result)

        return ImportPlan(
            SITE_GROUP, mode, mode == "managed", found, skipped, write if found else None
        )
