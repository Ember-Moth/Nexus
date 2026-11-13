from contextlib import asynccontextmanager
from uuid import UUID

from .errors import IdentityError, forbidden, missing, unauthorized
from .ports import TransactionFactory
from .tokens import digest_token


class AccessControl:
    def __init__(self, transactions: TransactionFactory):
        self._transactions = transactions

    async def _actor(self, tx, token_digest: str):
        actor = await tx.directory.actor(token_digest)
        if actor is None:
            raise unauthorized()
        return actor

    @asynccontextmanager
    async def authenticated(self, token: str):
        token_digest = digest_token(token, "nxs_")
        actor = None
        try:
            async with self._transactions() as tx:
                actor = await self._actor(tx, token_digest)
                yield tx, actor
        except IdentityError as error:
            if actor is not None and error.status in {403, 404}:
                # Persist denial evidence after rolling back the rejected business transaction.
                async with self._transactions() as audit_tx:
                    await audit_tx.audit.record(
                        actor.user.id,
                        "access.denied",
                        error.resource_id or actor.user.id,
                        details={"reason": error.reason},
                    )
            raise

    @staticmethod
    def platform_admin(actor):
        if "platform_admin" not in actor.platform_roles:
            raise forbidden()

    async def organization(
        self, tx, actor, organization_id: UUID, *, manage=False, lock=False, finance_read=False
    ):
        is_admin = "platform_admin" in actor.platform_roles
        global_reader = finance_read and "platform_finance" in actor.platform_roles
        if not is_admin and not global_reader:
            member = await tx.directory.membership_for_user(organization_id, actor.user.id)
            if member is None or member.status != "active":
                raise IdentityError(
                    "not_found",
                    "Resource not found",
                    404,
                    resource_id=organization_id,
                    reason="organization_outside_access_scope",
                )
        organization = await tx.directory.organization(organization_id, lock=lock)
        if organization is None:
            raise missing(organization_id)
        if not is_admin and not global_reader:
            member = await tx.directory.membership_for_user(organization_id, actor.user.id)
            if member is None or member.status != "active":
                raise missing(organization_id)
            if manage and (
                member.role != "org_owner" or organization.status not in {"active", "provisioning"}
            ):
                raise forbidden(organization_id)
        return organization

    @staticmethod
    def version(actual: int, expected: int):
        if actual != expected:
            raise IdentityError("version_conflict", "Resource version has changed", 412)
