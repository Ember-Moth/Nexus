import asyncio
import secrets
from datetime import timedelta
from uuid import UUID, uuid4

from nexus.shared.errors import BootstrapRejected

from .authorization import AccessControl
from .errors import IdentityError, forbidden, missing, unauthorized
from .ports import AuthLimits, Cursor, Passwords, TransactionFactory
from .schemas import (
    BootstrapResult,
    CurrentUser,
    FinancialAccess,
    MemberCreate,
    MemberPatch,
    OrganizationCreate,
    OrganizationPatch,
    SessionCreated,
    SetupIssued,
)
from .tokens import digest_token


def normalize_login(login: str) -> str:
    value = login.strip().lower()
    if not 3 <= len(value) <= 254 or any(char.isspace() for char in value):
        raise IdentityError(
            "invalid_login", "Login must contain 3-254 non-whitespace characters", 422
        )
    return value


class IdentityService:
    async def platform_roles(self, token, user_id):
        async with self._access.authenticated(token) as (tx, actor):
            self._access.platform_admin(actor)
            result = await tx.directory.roles(user_id)
            if result is None:
                raise missing(user_id)
            return result

    async def update_platform_roles(self, token, user_id, body, version):
        async with self._access.authenticated(token) as (tx, actor):
            await tx.administrators.lock_bootstrap()
            actor = await self._access._actor(tx, digest_token(token, "nxs_"))
            self._access.platform_admin(actor)
            user = await tx.directory.user(user_id, lock=True)
            if user is None:
                raise missing(user_id)
            self._access.version(user.version, version)
            current = await tx.directory.roles(user_id)
            if body.roles and user.status != "active":
                raise IdentityError("user_inactive", "Active user required", 409)
            if (
                "platform_admin" in current.roles
                and "platform_admin" not in body.roles
                and not await tx.directory.other_administrators(user_id)
            ):
                raise IdentityError(
                    "last_administrator", "Cannot remove the last active administrator", 409
                )
            result = await tx.directory.replace_roles(user_id, body.roles)
            await tx.audit.record(
                actor.user.id,
                "user.platform_roles_changed",
                user_id,
                details={"before": current.roles, "after": result.roles},
            )
            return result

    async def report_scope(
        self, token, org_id=None, *, operation="usage", project_id=None, key_id=None
    ):
        async with self._access.authenticated(token) as (tx, actor):
            platform = bool(actor.platform_roles & {"platform_admin", "platform_finance"})
            finance = "platform_finance" in actor.platform_roles
            if org_id is None:
                if not platform or (operation == "finance" and not finance):
                    raise forbidden()
                return actor.user.id, None, platform, finance
            await self._access.organization(tx, actor, org_id, finance_read=True)
            member = await tx.directory.membership_for_user(org_id, actor.user.id)
            role = member.role if member and member.status == "active" else None
            projects = None
            if not platform and role == "org_developer":
                if operation != "usage":
                    raise forbidden(org_id)
                projects = await tx.applications.granted_projects(org_id, member.id)
            elif not platform and role not in {"org_owner", "org_finance"}:
                raise forbidden(org_id)
            if operation == "finance" and not finance:
                raise forbidden(org_id)
            if project_id is not None:
                if not await tx.applications.project(org_id, project_id) or (
                    projects is not None and project_id not in projects
                ):
                    raise missing(project_id)
            if key_id is not None:
                key = await tx.applications.key(org_id, key_id)
                if (
                    key is None
                    or (project_id and key.project_id != project_id)
                    or (projects is not None and key.project_id not in projects)
                ):
                    raise missing(key_id)
            return actor.user.id, projects, platform, finance

    async def financial_access(
        self,
        token,
        organization_id=None,
        *,
        operation="read",
        scope_type="organization",
        scope_id=None,
        layer="organization",
    ):
        async with self._access.authenticated(token) as (tx, actor):
            if operation == "finance" and "platform_finance" not in actor.platform_roles:
                raise forbidden()
            if operation in {"pricing", "payment_admin"} or (
                operation == "budget" and layer == "platform"
            ):
                self._access.platform_admin(actor)
            if organization_id is None:
                if operation not in {"finance", "pricing", "payment_admin"}:
                    raise forbidden()
                return actor.user.id
            org = await self._access.organization(tx, actor, organization_id, finance_read=True)
            member = await tx.directory.membership_for_user(organization_id, actor.user.id)
            role = member.role if member and member.status == "active" else None
            platform = bool(actor.platform_roles & {"platform_admin", "platform_finance"})
            if operation == "payment":
                if "platform_finance" not in actor.platform_roles and role not in {
                    "org_owner",
                    "org_finance",
                }:
                    raise forbidden(organization_id)
                if org.status != "active":
                    raise IdentityError(
                        "organization_inactive",
                        "An active organization is required for payment",
                        409,
                    )
            if operation == "budget":
                if "platform_admin" not in actor.platform_roles and role != "org_owner":
                    raise forbidden(organization_id)
                if org.status not in {"active", "provisioning"}:
                    raise IdentityError(
                        "organization_inactive", "Organization does not allow budget changes", 409
                    )
            elif operation == "read" and not platform and role not in {"org_owner", "org_finance"}:
                raise forbidden(organization_id)
            project_id = key_id = None
            if scope_type == "organization":
                if scope_id is not None and scope_id != organization_id:
                    raise missing(scope_id)
            elif scope_type == "project":
                if await tx.applications.project(organization_id, scope_id) is None:
                    raise missing(scope_id)
                project_id = scope_id
            else:
                key = await tx.applications.key(organization_id, scope_id)
                if key is None:
                    raise missing(scope_id)
                project_id, key_id = key.project_id, key.id
            return FinancialAccess(actor.user.id, organization_id, org.status, project_id, key_id)

    async def pending_organizations(self, limit=100):
        async with self._transactions() as tx:
            return await tx.directory.pending_organizations(limit)

    async def account_ready(self, event_id, organization_id, account_id, *, schema_version):
        if schema_version != 1:
            raise IdentityError(
                "unsupported_event_version", "Unsupported account event version", 422
            )
        async with self._transactions() as tx:
            await tx.directory.account_ready(event_id, organization_id, account_id)

    def __init__(
        self,
        transactions: TransactionFactory,
        passwords: Passwords,
        auth_limits: AuthLimits,
        *,
        session_ttl: int,
        setup_ttl: int,
    ) -> None:
        self._transactions = transactions
        self._access = AccessControl(transactions)
        self._passwords = passwords
        self._auth_limits = auth_limits
        self._session_ttl = session_ttl
        self._setup_ttl = setup_ttl
        self._hash_slots = asyncio.Semaphore(2)

    async def _hash(self, password: str) -> str:
        if not 16 <= len(password) <= 1024:
            raise IdentityError("invalid_password", "Password must contain 16-1024 characters", 422)
        async with self._hash_slots:
            return await asyncio.to_thread(self._passwords.hash, password)

    async def _throttle(self, subject: str, peer: str):
        try:
            allowed = await self._auth_limits.allow(subject, peer)
        except Exception:
            raise IdentityError(
                "authentication_unavailable", "Authentication is temporarily unavailable", 503
            ) from None
        if not allowed:
            raise IdentityError("rate_limited", "Too many authentication attempts", 429)

    async def login(self, login: str, password: str, peer: str) -> SessionCreated:
        login = normalize_login(login)
        if not 1 <= len(password) <= 1024:
            raise unauthorized()
        await self._throttle("login:" + login, peer)
        async with self._transactions() as tx:
            credentials = await tx.directory.credentials(login)
        password_hash = (
            credentials[1] if credentials and credentials[0].status == "active" else None
        )
        async with self._hash_slots:
            verified = await asyncio.to_thread(self._passwords.verify, password_hash, password)
        if not verified or credentials is None:
            raise unauthorized()
        token = "nxs_" + secrets.token_urlsafe(32)
        async with self._transactions() as tx:
            user = await tx.directory.user(credentials[0].id, lock=True)
            if user is None or not await tx.directory.password_matches(user.id, password_hash):
                raise unauthorized()
            expires = await tx.directory.now() + timedelta(seconds=self._session_ttl)
            await tx.directory.create_session(user.id, digest_token(token, "nxs_"), expires)
            await tx.audit.record(user.id, "session.created", user.id)
        return SessionCreated(access_token=token, expires_at=expires)

    async def logout(self, token: str):
        digest = digest_token(token, "nxs_")
        async with self._transactions() as tx:
            user_id = await tx.directory.revoke_session(digest)
            if user_id:
                await tx.audit.record(user_id, "session.revoked", user_id)

    async def me(self, token: str) -> CurrentUser:
        async with self._access.authenticated(token) as (tx, actor):
            return CurrentUser(
                user=actor.user,
                platform_roles=sorted(actor.platform_roles),
                memberships=await tx.directory.my_memberships(actor.user.id),
            )

    async def require_platform_admin(self, token: str) -> UUID:
        async with self._access.authenticated(token) as (_, actor):
            self._access.platform_admin(actor)
            return actor.user.id

    async def list_users(self, token: str, limit: int, cursor: Cursor):
        async with self._access.authenticated(token) as (tx, actor):
            self._access.platform_admin(actor)
            return await tx.directory.users(limit, cursor)

    async def _issue_setup(self, tx, actor, user):
        if user.status == "disabled":
            raise IdentityError(
                "user_disabled", "Disabled users cannot receive password setup credentials", 409
            )
        token = "nxp_" + secrets.token_urlsafe(32)
        expires = await tx.directory.now() + timedelta(seconds=self._setup_ttl)
        await tx.directory.issue_setup(user.id, digest_token(token, "nxp_"), expires)
        await tx.audit.record(actor.user.id, "password_setup.issued", user.id)
        return SetupIssued(user=user, setup_token=token, expires_at=expires)

    async def create_user(self, token: str, login: str):
        async with self._access.authenticated(token) as (tx, actor):
            self._access.platform_admin(actor)
            user = await tx.directory.create_user(normalize_login(login))
            await tx.audit.record(actor.user.id, "user.created", user.id)
            return await self._issue_setup(tx, actor, user)

    async def issue_password_setup(self, token: str, user_id: UUID):
        async with self._access.authenticated(token) as (tx, actor):
            self._access.platform_admin(actor)
            user = await tx.directory.user(user_id, lock=True)
            if user is None:
                raise missing(user_id)
            return await self._issue_setup(tx, actor, user)

    async def set_password(self, token: str, password: str, peer: str):
        digest = digest_token(token, "nxp_")
        await self._throttle("setup:" + digest, peer)
        async with self._transactions() as tx:
            user_id = await tx.directory.setup_user(digest)
        if user_id is None:
            raise unauthorized()
        password_hash = await self._hash(password)
        async with self._transactions() as tx:
            user = await tx.directory.user(user_id, lock=True)
            if (
                user is None
                or user.status == "disabled"
                or not await tx.directory.consume_setup(digest, user_id, password_hash)
            ):
                raise unauthorized()
            await tx.audit.record(user.id, "user.password_set", user.id)

    async def list_organizations(self, token: str, limit: int, cursor: Cursor):
        async with self._access.authenticated(token) as (tx, actor):
            global_reader = bool(actor.platform_roles & {"platform_admin", "platform_finance"})
            return await tx.directory.organizations(
                None if global_reader else actor.user.id, limit, cursor
            )

    async def get_organization(self, token: str, organization_id: UUID):
        async with self._access.authenticated(token) as (tx, actor):
            return await self._access.organization(tx, actor, organization_id, finance_read=True)

    async def create_organization(self, token: str, request: OrganizationCreate):
        async with self._access.authenticated(token) as (tx, actor):
            self._access.platform_admin(actor)
            owner_id = request.owner_user_id or actor.user.id
            owner = await tx.directory.user(owner_id, lock=True)
            if owner is None:
                raise missing(owner_id)
            if owner.status != "active":
                raise IdentityError("owner_inactive", "An active initial owner is required", 409)
            organization = await tx.directory.create_organization(request.name, owner_id)
            await tx.audit.record(
                actor.user.id,
                "organization.created",
                organization.id,
                organization_id=organization.id,
                details={"owner_user_id": str(owner_id)},
            )
            return organization

    async def update_organization(
        self, token: str, organization_id: UUID, request: OrganizationPatch, version: int
    ):
        async with self._access.authenticated(token) as (tx, actor):
            organization = await self._access.organization(
                tx, actor, organization_id, manage=True, lock=True
            )
            self._access.version(organization.version, version)
            if organization.status == "archived":
                raise IdentityError(
                    "organization_archived", "Archived organizations cannot be modified", 409
                )
            if request.status is not None:
                self._access.platform_admin(actor)
            result = await tx.directory.update_organization(
                organization_id, request.name, request.status
            )
            await tx.audit.record(
                actor.user.id,
                "organization.updated",
                organization_id,
                organization_id=organization_id,
                details=request.model_dump(exclude_unset=True),
            )
            return result

    async def list_members(self, token: str, organization_id: UUID, limit: int, cursor: Cursor):
        async with self._access.authenticated(token) as (tx, actor):
            await self._access.organization(tx, actor, organization_id, manage=True)
            return await tx.directory.members(organization_id, limit, cursor)

    async def get_member(self, token: str, organization_id: UUID, member_id: UUID):
        async with self._access.authenticated(token) as (tx, actor):
            await self._access.organization(tx, actor, organization_id, manage=True)
            member = await tx.directory.membership(organization_id, member_id)
            if member is None:
                raise missing(member_id)
            return member

    async def add_member(self, token: str, organization_id: UUID, request: MemberCreate):
        async with self._access.authenticated(token) as (tx, actor):
            organization = await self._access.organization(
                tx, actor, organization_id, manage=True, lock=True
            )
            if organization.status == "archived":
                raise IdentityError(
                    "organization_archived", "Archived organizations cannot be modified", 409
                )
            user = await tx.directory.user(request.user_id, lock=True)
            if user is None:
                raise missing(request.user_id)
            if user.status != "active":
                raise IdentityError("member_inactive", "Only active users may be added", 409)
            result = await tx.directory.create_member(organization_id, user.id, request.role)
            await tx.audit.record(
                actor.user.id,
                "membership.created",
                result.id,
                organization_id=organization_id,
                details={"user_id": str(user.id), "role": request.role},
            )
            return result

    async def update_member(
        self, token: str, organization_id: UUID, member_id: UUID, request: MemberPatch, version: int
    ):
        async with self._access.authenticated(token) as (tx, actor):
            organization = await self._access.organization(
                tx, actor, organization_id, manage=True, lock=True
            )
            if organization.status == "archived":
                raise IdentityError(
                    "organization_archived", "Archived organizations cannot be modified", 409
                )
            member = await tx.directory.membership(organization_id, member_id)
            if member is None:
                raise missing(member_id)
            self._access.version(member.version, version)
            role, status = request.role or member.role, request.status or member.status
            if role == "org_owner" and status == "active":
                user = await tx.directory.user(member.user_id, lock=True)
                if user is None or user.status != "active":
                    raise IdentityError("owner_inactive", "An owner must be an active user", 409)
            if (
                member.role == "org_owner"
                and member.status == "active"
                and (role != "org_owner" or status != "active")
            ):
                if await tx.directory.active_owner_count(organization_id) <= 1:
                    raise IdentityError("last_owner", "At least one active owner must remain", 409)
            result = await tx.directory.update_member(organization_id, member_id, role, status)
            await tx.audit.record(
                actor.user.id,
                "membership.updated",
                member_id,
                organization_id=organization_id,
                details={"role": role, "status": status},
            )
            return result

    async def bootstrap_admin(self, login: str, password: str) -> BootstrapResult:
        login = login.strip().lower()
        if not 3 <= len(login) <= 254 or any(char.isspace() for char in login):
            raise BootstrapRejected("login must contain 3-254 non-whitespace characters")
        if not 16 <= len(password) <= 1024:
            raise BootstrapRejected("administrator password must contain 16-1024 characters")
        # CPU work is performed outside both the event loop and the database transaction.
        password_hash = await asyncio.to_thread(self._passwords.hash, password)
        async with self._transactions() as tx:
            await tx.administrators.lock_bootstrap()
            existing = await tx.administrators.first_admin()
            if existing is not None:
                if existing[1] != login:
                    raise BootstrapRejected("an administrator already exists")
                return BootstrapResult(existing[0], created=False)
            if await tx.administrators.login_exists(login):
                raise BootstrapRejected("existing users cannot be promoted by bootstrap")
            user_id = uuid4()
            await tx.administrators.create(user_id, login, password_hash)
            await tx.audit.record_bootstrap(user_id)
            return BootstrapResult(user_id, created=True)
