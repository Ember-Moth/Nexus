"""Project-scoped management and application credential lifecycle."""

import hashlib
import json
import re
import secrets
from datetime import timedelta

from ..domain.errors import IdentityError, missing, unauthorized
from ..domain.tokens import digest_token
from .authorization import AccessControl
from .ports import TransactionFactory
from .schemas import KeyIssued


class ProjectService:
    def __init__(self, transactions: TransactionFactory):
        self._transactions = transactions
        self._access = AccessControl(transactions)

    async def list_projects(self, token, org_id, limit, cursor):
        async with self._access.authenticated(token) as (tx, actor):
            member_id = await self._access.project_scope(tx, actor, org_id)
            return await tx.applications.projects(org_id, member_id, limit, cursor)

    async def get_project(self, token, org_id, project_id):
        async with self._access.authenticated(token) as (tx, actor):
            return await self._access.project(tx, actor, org_id, project_id)

    async def create_project(self, token, org_id, body):
        async with self._access.authenticated(token) as (tx, actor):
            await self._access.project_scope(tx, actor, org_id, write=True, owner=True)
            result = await tx.applications.create_project(org_id, body.name)
            await tx.audit.record(
                actor.user.id, "project.created", result.id, organization_id=org_id
            )
            return result

    async def update_project(self, token, org_id, project_id, body, version):
        async with self._access.authenticated(token) as (tx, actor):
            project = await self._access.project(
                tx, actor, org_id, project_id, write=True, owner=True
            )
            self._access.version(project.version, version)
            if project.status == "archived":
                raise IdentityError("project_archived", "Archived projects cannot be modified", 409)
            changes = body.model_dump(exclude_unset=True)
            if "name" in changes:
                changes["name"] = changes["name"].strip()
            result = await tx.applications.update_project(org_id, project_id, changes)
            await tx.audit.record(
                actor.user.id,
                "project.updated",
                project_id,
                organization_id=org_id,
                details=changes,
            )
            return result

    async def list_grants(self, token, org_id, limit, cursor):
        async with self._access.authenticated(token) as (tx, actor):
            await self._access.project_scope(tx, actor, org_id, owner=True)
            return await tx.applications.grants(org_id, limit, cursor)

    async def get_grant(self, token, org_id, grant_id):
        async with self._access.authenticated(token) as (tx, actor):
            await self._access.project_scope(tx, actor, org_id, owner=True)
            grant = await tx.applications.grant(org_id, grant_id)
            if grant is None:
                raise missing(grant_id)
            return grant

    async def _grant_target(self, tx, actor, org_id, project_id, membership_id):
        await self._access.project(
            tx, actor, org_id, project_id, write=True, owner=True, require_active=True
        )
        member = await tx.directory.membership(org_id, membership_id)
        if member is None:
            raise missing(membership_id)
        if member.status != "active" or member.role != "org_developer":
            raise IdentityError(
                "invalid_grant_target", "An active developer membership is required", 409
            )
        user = await tx.directory.user(member.user_id, lock=True)
        if user is None or user.status != "active":
            raise IdentityError("invalid_grant_target", "An active developer user is required", 409)

    async def create_grant(self, token, org_id, body):
        async with self._access.authenticated(token) as (tx, actor):
            await self._grant_target(tx, actor, org_id, body.project_id, body.membership_id)
            result = await tx.applications.create_grant(org_id, body.project_id, body.membership_id)
            await tx.audit.record(
                actor.user.id, "project_grant.created", result.id, organization_id=org_id
            )
            return result

    async def update_grant(self, token, org_id, grant_id, body, version):
        async with self._access.authenticated(token) as (tx, actor):
            await self._access.project_scope(tx, actor, org_id, write=True, owner=True)
            grant = await tx.applications.grant(org_id, grant_id)
            if grant is None:
                raise missing(grant_id)
            self._access.version(grant.version, version)
            if body.status == "active":
                await self._grant_target(tx, actor, org_id, grant.project_id, grant.membership_id)
            result = await tx.applications.update_grant(org_id, grant_id, body.status)
            await tx.audit.record(
                actor.user.id,
                "project_grant.updated",
                grant_id,
                organization_id=org_id,
                details={"status": body.status},
            )
            return result

    async def _account(
        self,
        tx,
        actor,
        org_id,
        service_account_id,
        *,
        write=False,
        require_active=False,
        emergency=False,
    ):
        # Scope checks precede resource lookup, and again under the organization lock for writes.
        await self._access.project_scope(
            tx, actor, org_id, write=write, credentials=True, emergency=emergency
        )
        account = await tx.applications.service_account(org_id, service_account_id)
        if account is None:
            raise missing(service_account_id)
        await self._access.project(
            tx,
            actor,
            org_id,
            account.project_id,
            write=write,
            credentials=True,
            require_active=require_active,
            emergency=emergency,
        )
        if require_active and account.status != "active":
            raise IdentityError("service_account_inactive", "Service account is disabled", 409)
        return account

    async def list_service_accounts(self, token, org_id, project_id, limit, cursor):
        async with self._access.authenticated(token) as (tx, actor):
            member_id = await self._access.project_scope(tx, actor, org_id, credentials=True)
            if project_id:
                await self._access.project(tx, actor, org_id, project_id, credentials=True)
            return await tx.applications.service_accounts(
                org_id, member_id, project_id, limit, cursor
            )

    async def get_service_account(self, token, org_id, account_id):
        async with self._access.authenticated(token) as (tx, actor):
            return await self._account(tx, actor, org_id, account_id)

    async def create_service_account(self, token, org_id, body):
        async with self._access.authenticated(token) as (tx, actor):
            await self._access.project(
                tx,
                actor,
                org_id,
                body.project_id,
                write=True,
                credentials=True,
                require_active=True,
            )
            result = await tx.applications.create_service_account(
                org_id, body.project_id, body.name
            )
            await tx.audit.record(
                actor.user.id, "service_account.created", result.id, organization_id=org_id
            )
            return result

    async def update_service_account(self, token, org_id, account_id, body, version):
        async with self._access.authenticated(token) as (tx, actor):
            account = await self._account(tx, actor, org_id, account_id, write=True)
            await self._access.project(
                tx, actor, org_id, account.project_id, credentials=True, require_active=True
            )
            self._access.version(account.version, version)
            changes = body.model_dump(exclude_unset=True)
            if "name" in changes:
                changes["name"] = changes["name"].strip()
            result = await tx.applications.update_service_account(org_id, account_id, changes)
            await tx.audit.record(
                actor.user.id,
                "service_account.updated",
                account_id,
                organization_id=org_id,
                details=changes,
            )
            return result

    async def _key(self, tx, actor, org_id, key_id, *, write=False, emergency=False):
        await self._access.project_scope(
            tx, actor, org_id, write=write, credentials=True, emergency=emergency
        )
        key = await tx.applications.key(org_id, key_id)
        if key is None:
            raise missing(key_id)
        await self._access.project(
            tx, actor, org_id, key.project_id, write=write, credentials=True, emergency=emergency
        )
        return key

    async def list_keys(self, token, org_id, project_id, account_id, limit, cursor):
        async with self._access.authenticated(token) as (tx, actor):
            member_id = await self._access.project_scope(tx, actor, org_id, credentials=True)
            if project_id:
                await self._access.project(tx, actor, org_id, project_id, credentials=True)
            if account_id:
                account = await self._account(tx, actor, org_id, account_id)
                if project_id and account.project_id != project_id:
                    raise missing(account_id)
            return await tx.applications.keys(
                org_id, member_id, project_id, account_id, limit, cursor
            )

    async def get_key(self, token, org_id, key_id):
        async with self._access.authenticated(token) as (tx, actor):
            return await self._key(tx, actor, org_id, key_id)

    @staticmethod
    def _request_identity(idempotency_key, body, *, key_id=None, version=None):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key):
            raise IdentityError("invalid_idempotency_key", "Invalid Idempotency-Key", 422)
        payload = {
            "body": body.model_dump(mode="json"),
            "key_id": str(key_id) if key_id else None,
            "version": version,
        }
        return hashlib.sha256(idempotency_key.encode()).hexdigest(), hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    async def _replay(self, tx, actor, org_id, operation, key_digest, request_digest):
        record = await tx.applications.replay(org_id, actor.user.id, operation, key_digest)
        if record is None:
            return None
        if record[0] != request_digest:
            raise IdentityError(
                "idempotency_conflict", "Idempotency-Key was used with different parameters", 409
            )
        key = await tx.applications.key(org_id, record[1])
        return KeyIssued(key=key, secret_status="secret_unavailable")

    @staticmethod
    def _expiry(expires_at, now):
        if expires_at is not None and expires_at <= now:
            raise IdentityError("invalid_expiry", "Expiry must be in the future", 422)

    async def create_key(self, token, org_id, body, idempotency_key):
        key_digest, request_digest = self._request_identity(idempotency_key, body)
        async with self._access.authenticated(token) as (tx, actor):
            account = await self._account(tx, actor, org_id, body.service_account_id, write=True)
            replay = await self._replay(tx, actor, org_id, "key.create", key_digest, request_digest)
            if replay:
                return replay
            await self._account(tx, actor, org_id, account.id, require_active=True)
            self._expiry(body.expires_at, await tx.directory.now())
            secret = "nxk_" + secrets.token_urlsafe(32)
            key = await tx.applications.create_key(
                org_id,
                account.project_id,
                account.id,
                body.name,
                digest_token(secret, "nxk_"),
                secret[:12],
                body.expires_at,
            )
            await tx.applications.remember(
                org_id, actor.user.id, "key.create", key_digest, request_digest, key.id
            )
            await tx.audit.record(actor.user.id, "api_key.created", key.id, organization_id=org_id)
            return KeyIssued(key=key, secret=secret, secret_status="available")

    async def revoke_key(self, token, org_id, key_id, version):
        async with self._access.authenticated(token) as (tx, actor):
            key = await self._key(tx, actor, org_id, key_id, write=True, emergency=True)
            self._access.version(key.version, version)
            if key.revoked_at is not None:
                return key
            result = await tx.applications.revoke_key(org_id, key_id, await tx.directory.now())
            await tx.audit.record(actor.user.id, "api_key.revoked", key_id, organization_id=org_id)
            return result

    async def rotate_key(self, token, org_id, key_id, body, version, idempotency_key):
        key_digest, request_digest = self._request_identity(
            idempotency_key, body, key_id=key_id, version=version
        )
        async with self._access.authenticated(token) as (tx, actor):
            old = await self._key(tx, actor, org_id, key_id, write=True)
            replay = await self._replay(tx, actor, org_id, "key.rotate", key_digest, request_digest)
            if replay:
                return replay
            self._access.version(old.version, version)
            await self._account(tx, actor, org_id, old.service_account_id, require_active=True)
            now = await tx.directory.now()
            if (
                old.revoked_at is not None
                or old.rotated_at is not None
                or (old.expires_at is not None and old.expires_at <= now)
            ):
                raise IdentityError(
                    "key_not_rotatable", "Key is revoked, expired or already rotated", 409
                )
            expires = body.expires_at if body.expires_at is not None else old.expires_at
            self._expiry(expires, now)
            secret = "nxk_" + secrets.token_urlsafe(32)
            replacement = await tx.applications.create_key(
                org_id,
                old.project_id,
                old.service_account_id,
                body.name or old.name,
                digest_token(secret, "nxk_"),
                secret[:12],
                expires,
                old.id,
            )
            cutoff = now + timedelta(seconds=body.grace_seconds)
            if old.expires_at is not None:
                cutoff = min(cutoff, old.expires_at)
            await tx.applications.retire_key(org_id, old.id, now, cutoff, body.grace_seconds == 0)
            await tx.applications.remember(
                org_id, actor.user.id, "key.rotate", key_digest, request_digest, replacement.id
            )
            await tx.audit.record(
                actor.user.id,
                "api_key.rotated",
                old.id,
                organization_id=org_id,
                details={
                    "replacement_id": str(replacement.id),
                    "grace_seconds": body.grace_seconds,
                },
            )
            return KeyIssued(key=replacement, secret=secret, secret_status="available")

    async def authenticate_api_key(self, secret):
        digest = digest_token(secret, "nxk_")
        async with self._transactions() as tx:
            identity = await tx.applications.authenticate(digest)
            if identity is None:
                raise unauthorized()
            return identity
