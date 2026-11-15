import asyncio
import secrets
from datetime import timedelta
from uuid import UUID, uuid4

from nexus.shared.errors import BootstrapRejected
from nexus.shared.policy import CachedPolicy

from ..domain.errors import IdentityError, forbidden, missing, unauthorized
from ..domain.logins import login_candidates, new_login
from ..domain.tokens import digest_token
from .authorization import AccessControl
from .email_auth import EmailAuthentication
from .ports import AuthLimits, Cursor, MailLimits, Passwords, TransactionFactory
from .schemas import (
    BootstrapResult,
    CurrentUser,
    FinancialAccess,
    InvitationIssued,
    MemberCreate,
    MemberPatch,
    OrganizationCreate,
    OrganizationPatch,
    SessionCreated,
    SetupIssued,
)


class IdentityService(EmailAuthentication):
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
        providers,
        mail_cipher,
        mail_limits: MailLimits,
        login_hash_slots: int = 2,
        account_hash_slots: int = 1,
        hash_queue_timeout: float = 5.0,
    ) -> None:
        self._transactions = transactions
        self._access = AccessControl(transactions)
        self._passwords = passwords
        self._auth_limits = auth_limits
        self.providers = providers
        self.cipher = mail_cipher
        self._mail_limits = mail_limits
        # Limits, lifetimes and reserved logins; the switches are always read fresh.
        self._settings = CachedPolicy(self._read_policy)
        # Sign-in has its own Argon2 slots: a flood of sign-ups, resets or mailbox proofs can
        # only queue behind each other.
        self._login_slots = asyncio.Semaphore(login_hash_slots)
        self._account_slots = asyncio.Semaphore(account_hash_slots)
        self._hash_queue_timeout = hash_queue_timeout

    async def _argon2(self, slots: asyncio.Semaphore, function, *args):
        try:
            async with asyncio.timeout(self._hash_queue_timeout):
                await slots.acquire()
        except TimeoutError:
            raise IdentityError(
                "authentication_unavailable",
                "Authentication is busy; retry shortly",
                503,
                retry_after=1,
            ) from None
        # The slot stays taken until the thread finishes, even if the request is cancelled.
        work = asyncio.ensure_future(asyncio.to_thread(function, *args))
        work.add_done_callback(lambda _: slots.release())
        return await asyncio.shield(work)

    async def _hash(self, password: str) -> str:
        if not 16 <= len(password) <= 1024:
            raise IdentityError("invalid_password", "Password must contain 16-1024 characters", 422)
        return await self._argon2(self._account_slots, self._passwords.hash, password)

    async def _throttle(self, subject: str, peer: str):
        try:
            limits = (await self._settings.get()).sign_in
            allowed = await self._auth_limits.allow(
                subject,
                peer,
                account_limit=limits.per_account,
                ip_limit=limits.per_ip,
                window=limits.window_seconds,
            )
        except Exception:
            raise IdentityError(
                "authentication_unavailable", "Authentication is temporarily unavailable", 503
            ) from None
        if not allowed:
            raise IdentityError(
                "rate_limited",
                "Too many authentication attempts",
                429,
                retry_after=limits.window_seconds,
            )

    async def _verify(self, password_hash, password, *, login=False):
        slots = self._login_slots if login else self._account_slots
        return await self._argon2(slots, self._passwords.verify, password_hash, password)

    async def login(
        self, login: str, password: str, peer: str, turnstile_token=None
    ) -> SessionCreated:
        candidates = login_candidates(login)
        if not 1 <= len(password) <= 1024:
            raise unauthorized()
        # Every spelling of an account shares its canonical bucket.
        await self._throttle("login:" + candidates[0], peer)
        version = await self.guard("login", turnstile_token)
        async with self._transactions() as tx:
            credentials = await tx.directory.credentials(*candidates)
        password_hash = (
            credentials[1] if credentials and credentials[0].status == "active" else None
        )
        verified = await self._verify(password_hash, password, login=True)
        if not verified or credentials is None:
            raise unauthorized()
        async with self._transactions() as tx:
            await self.current(tx, version)
            user = await tx.directory.user(credentials[0].id, lock=True)
            if user is None or not await tx.directory.password_matches(user.id, password_hash):
                raise unauthorized()
            return await self._new_session(tx, user.id)

    async def _new_session(self, tx, user_id):
        token = "nxs_" + secrets.token_urlsafe(32)
        ttl = (await self._settings.get()).sessions.session_ttl_seconds
        expires = await tx.directory.now() + timedelta(seconds=ttl)
        await tx.directory.create_session(user_id, digest_token(token, "nxs_"), expires)
        await tx.audit.record(user_id, "session.created", user_id)
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
                verified_email=await tx.email.verified(actor.user.id),
                personal_organization_id=await tx.directory.personal_org_id(actor.user.id),
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
        ttl = (await self._settings.get()).sessions.password_setup_ttl_seconds
        expires = await tx.directory.now() + timedelta(seconds=ttl)
        await tx.directory.issue_setup(user.id, digest_token(token, "nxp_"), expires)
        await tx.audit.record(actor.user.id, "password_setup.issued", user.id)
        return SetupIssued(user=user, setup_token=token, expires_at=expires)

    async def create_user(self, token: str, login: str):
        async with self._access.authenticated(token) as (tx, actor):
            self._access.platform_admin(actor)
            user = await tx.directory.create_user(new_login(login))
            await tx.audit.record(actor.user.id, "user.created", user.id)
            return await self._issue_setup(tx, actor, user)

    async def issue_password_setup(self, token: str, user_id: UUID):
        async with self._access.authenticated(token) as (tx, actor):
            self._access.platform_admin(actor)
            user = await tx.directory.user(user_id, lock=True)
            if user is None:
                raise missing(user_id)
            return await self._issue_setup(tx, actor, user)

    async def set_password(self, token: str, password: str, peer: str, turnstile_token=None):
        digest = digest_token(token, "nxp_")
        await self._throttle("setup:" + digest, peer)
        version = await self.guard("set_password", turnstile_token)
        async with self._transactions() as tx:
            user_id = await tx.directory.setup_user(digest)
        if user_id is None:
            raise unauthorized()
        password_hash = await self._hash(password)
        async with self._transactions() as tx:
            await self.current(tx, version)
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
            if organization.personal_owner_id is not None and request.status == "archived":
                raise IdentityError(
                    "personal_organization_required",
                    "A personal organization cannot be archived",
                    409,
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
            await tx.directory.cancel_pending_invitations(organization_id, user.login)
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
            if organization.personal_owner_id == member.user_id and (
                role != "org_owner" or status != "active"
            ):
                raise IdentityError(
                    "personal_owner_required", "The bound owner must remain active", 409
                )
            user = await tx.directory.user(member.user_id, lock=True)
            if role == "org_owner" and status == "active" and user.status != "active":
                raise IdentityError("owner_inactive", "An owner must be an active user", 409)
            if (
                member.role == "org_owner"
                and member.status == "active"
                and (role != "org_owner" or status != "active")
            ):
                if await tx.directory.active_owner_count(organization_id) <= 1:
                    raise IdentityError("last_owner", "At least one active owner must remain", 409)
            await tx.directory.cancel_pending_invitations(organization_id, user.login)
            result = await tx.directory.update_member(organization_id, member_id, role, status)
            await tx.audit.record(
                actor.user.id,
                "membership.updated",
                member_id,
                organization_id=organization_id,
                details={"role": role, "status": status},
            )
            return result

    async def invite(self, token, org_id, body):
        candidates = login_candidates(body.login)
        async with self._access.authenticated(token) as (tx, actor):
            org = await self._access.organization(tx, actor, org_id, manage=True, lock=True)
            # Invite an existing account under the spelling it is stored with.
            existing = await tx.directory.credentials(*candidates)
            login = existing[0].login if existing else candidates[0]
            if org.status not in {"active", "provisioning"}:
                raise IdentityError(
                    "organization_inactive", "Organization cannot accept invitations", 409
                )
            secret = "nxi_" + secrets.token_urlsafe(32)
            expires = await tx.directory.now() + timedelta(days=7)
            invitation = await tx.directory.issue_invitation(
                org_id, login, body.role, actor.user.id, digest_token(secret, "nxi_"), expires
            )
            await tx.audit.record(
                actor.user.id,
                "invitation.created",
                invitation.id,
                organization_id=org_id,
                details={"role": body.role},
            )
            return InvitationIssued(**invitation.model_dump(), token=secret)

    async def list_invitations(self, token, org_id, limit, cursor):
        async with self._access.authenticated(token) as (tx, actor):
            await self._access.organization(tx, actor, org_id, manage=True)
            return await tx.directory.invitations(org_id, limit, cursor)

    async def revoke_invitation(self, token, org_id, invitation_id):
        async with self._access.authenticated(token) as (tx, actor):
            await self._access.organization(tx, actor, org_id, manage=True, lock=True)
            if not await tx.directory.revoke_invitation(org_id, invitation_id):
                raise missing(invitation_id)
            await tx.audit.record(
                actor.user.id, "invitation.revoked", invitation_id, organization_id=org_id
            )

    async def accept_invitation(self, token, body):
        async with self._access.authenticated(token) as (tx, actor):
            digest = digest_token(body.token.get_secret_value(), "nxi_")
            org_id = await tx.directory.invitation_org(digest)
            if org_id is None:
                raise IdentityError(
                    "invitation_unavailable", "Invitation unavailable for this account", 404
                )
            org = await tx.directory.organization(org_id, lock=True)
            if org is None or org.status not in {"active", "provisioning"}:
                raise IdentityError(
                    "organization_inactive", "Organization cannot accept invitations", 409
                )
            member, accepted = await tx.directory.redeem_invitation(org_id, digest, actor.user)
            if accepted:
                await tx.audit.record(
                    actor.user.id,
                    "invitation.accepted",
                    member.id,
                    organization_id=org_id,
                    details={"role": member.role},
                )
            return member

    async def bootstrap_admin(self, login: str, password: str) -> BootstrapResult:
        legacy = login.strip().lower()
        try:
            login = new_login(login)
        except IdentityError as error:
            message = str(error)
            raise BootstrapRejected(message[0].lower() + message[1:]) from None
        if not 16 <= len(password) <= 1024:
            raise BootstrapRejected("administrator password must contain 16-1024 characters")
        # CPU work is performed outside both the event loop and the database transaction.
        password_hash = await asyncio.to_thread(self._passwords.hash, password)
        async with self._transactions() as tx:
            await tx.administrators.lock_bootstrap()
            existing = await tx.administrators.first_admin()
            if existing is not None:
                if existing[1] not in {login, legacy}:
                    raise BootstrapRejected("an administrator already exists")
                return BootstrapResult(existing[0], created=False)
            if await tx.administrators.login_exists(login):
                raise BootstrapRejected("existing users cannot be promoted by bootstrap")
            user_id = uuid4()
            await tx.administrators.create(user_id, login, password_hash)
            await tx.audit.record_bootstrap(user_id)
            return BootstrapResult(user_id, created=True)
