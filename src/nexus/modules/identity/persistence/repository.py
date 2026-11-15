import base64
import json
from datetime import datetime
from types import TracebackType
from uuid import UUID, uuid4

from sqlalchemy import delete, exists, func, select, text, tuple_, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.infrastructure.postgres import ModuleDatabase

from ..application.ports import Actor, Cursor
from ..application.schemas import (
    InvitationView,
    MemberListItem,
    MemberView,
    OrganizationCreated,
    OrganizationView,
    Page,
    PlatformRolesView,
    UserView,
)
from ..domain.errors import IdentityError
from ..domain.events import ORGANIZATION_CREATED, ORGANIZATION_CREATED_VERSION
from .email import EmailRepository
from .models import (
    AdminSession,
    AuditEvent,
    Inbox,
    Membership,
    Organization,
    OrganizationInvitation,
    OutboxEvent,
    PasswordToken,
    PlatformRole,
    User,
)
from .policies import PolicyRepository
from .projects import ApplicationRepository


def view(kind, row):
    return kind(**{name: getattr(row, name) for name in kind.model_fields})


class DirectoryRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def now(self) -> datetime:
        return (await self._session.execute(select(func.clock_timestamp()))).scalar_one()

    async def credentials(self, *logins: str):
        """The account stored under the first matching spelling, in the order given."""
        rows = {
            row.login: row
            for row in (await self._session.execute(select(User).where(User.login.in_(logins))))
            .scalars()
            .all()
        }
        row = next((rows[login] for login in logins if login in rows), None)
        return (view(UserView, row), row.password_hash) if row else None

    async def user(self, user_id: UUID, *, lock: bool = False):
        stmt = select(User).where(User.id == user_id).execution_options(populate_existing=True)
        if lock:
            stmt = stmt.with_for_update()
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return view(UserView, row) if row else None

    async def password_matches(self, user_id: UUID, password_hash: str) -> bool:
        return bool(
            (
                await self._session.execute(
                    select(User.id).where(
                        User.id == user_id,
                        User.password_hash == password_hash,
                        User.status == "active",
                    )
                )
            ).first()
        )

    async def create_user(self, login: str, password_hash: str | None = None):
        row = User(
            id=uuid4(),
            login=login,
            status="active" if password_hash else "pending",
            password_hash=password_hash,
        )
        self._session.add(row)
        await self._session.flush()
        return view(UserView, row)

    async def roles(self, user_id):
        user = await self.user(user_id)
        if user is None:
            return None
        roles = (
            (
                await self._session.execute(
                    select(PlatformRole.role).where(PlatformRole.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        return PlatformRolesView(user_id=user_id, version=user.version, roles=sorted(roles))

    async def replace_roles(self, user_id, roles):
        await self._session.execute(delete(PlatformRole).where(PlatformRole.user_id == user_id))
        self._session.add_all(PlatformRole(user_id=user_id, role=role) for role in roles)
        await self._session.execute(
            update(User).where(User.id == user_id).values(version=User.version + 1)
        )
        await self._session.flush()
        return await self.roles(user_id)

    async def other_administrators(self, user_id):
        return (
            await self._session.execute(
                select(func.count())
                .select_from(PlatformRole)
                .join(User, User.id == PlatformRole.user_id)
                .where(
                    PlatformRole.role == "platform_admin",
                    User.status == "active",
                    User.id != user_id,
                )
            )
        ).scalar_one()

    async def pending_organizations(self, limit):
        rows = (
            await self._session.execute(
                select(OutboxEvent.id, OutboxEvent.aggregate_id, OutboxEvent.schema_version)
                .join(Organization, Organization.id == OutboxEvent.aggregate_id)
                .where(
                    OutboxEvent.topic == ORGANIZATION_CREATED,
                    Organization.billing_account_id.is_(None),
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(limit)
            )
        ).all()
        return [
            OrganizationCreated(event_id=row[0], organization_id=row[1], schema_version=row[2])
            for row in rows
        ]

    async def account_ready(self, event_id, organization_id, account_id):
        row = (
            await self._session.execute(
                select(Organization).where(Organization.id == organization_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise IdentityError("not_found", "Organization not found", 404)
        receipt = await self._session.get(Inbox, ("billing.account_ready", event_id))
        if receipt:
            if receipt.organization_id != organization_id or receipt.result_id != account_id:
                raise IdentityError("event_conflict", "Conflicting account event", 409)
            return
        if row.billing_account_id is not None and row.billing_account_id != account_id:
            raise IdentityError("account_conflict", "Organization already has an account", 409)
        row.billing_account_id = account_id
        if row.status == "provisioning":
            row.status = "active"
        elif row.status == "suspended" and row.resume_status == "provisioning":
            row.resume_status = "active"
        row.version += 1
        self._session.add(
            Inbox(
                consumer="billing.account_ready",
                event_id=event_id,
                organization_id=organization_id,
                result_id=account_id,
            )
        )
        self._session.add(
            AuditEvent(
                id=uuid4(),
                actor_type="system",
                actor_id=None,
                organization_id=organization_id,
                action="organization.account_ready",
                resource_id=organization_id,
                details={"account_id": str(account_id), "event_id": str(event_id)},
            )
        )

    async def _page(self, statement, model, kind, limit: int, cursor: Cursor):
        if cursor:
            statement = statement.where(tuple_(model.created_at, model.id) > cursor)
        rows = (
            (
                await self._session.execute(
                    statement.order_by(model.created_at, model.id).limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        items = [view(kind, row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([last.created_at.isoformat(), str(last.id)]).encode()
            ).decode()
        return Page[kind](items=items, next_cursor=next_cursor)

    async def users(self, limit: int, cursor: Cursor):
        return await self._page(select(User), User, UserView, limit, cursor)

    async def actor(self, digest: str):
        row = (
            await self._session.execute(
                select(User, AdminSession.id)
                .join(AdminSession, AdminSession.user_id == User.id)
                .where(
                    AdminSession.token_digest == digest,
                    AdminSession.revoked_at.is_(None),
                    AdminSession.expires_at > func.clock_timestamp(),
                    User.status == "active",
                )
            )
        ).first()
        if not row:
            return None
        roles = (
            (
                await self._session.execute(
                    select(PlatformRole.role).where(PlatformRole.user_id == row[0].id)
                )
            )
            .scalars()
            .all()
        )
        return Actor(view(UserView, row[0]), row[1], frozenset(roles))

    async def create_session(self, user_id: UUID, digest: str, expires_at: datetime):
        self._session.add(
            AdminSession(id=uuid4(), user_id=user_id, token_digest=digest, expires_at=expires_at)
        )

    async def revoke_session(self, digest: str):
        return (
            await self._session.execute(
                update(AdminSession)
                .where(
                    AdminSession.token_digest == digest,
                    AdminSession.revoked_at.is_(None),
                )
                .values(revoked_at=func.clock_timestamp())
                .returning(AdminSession.user_id)
            )
        ).scalar_one_or_none()

    async def setup_user(self, digest: str):
        return (
            await self._session.execute(
                select(PasswordToken.user_id).where(
                    PasswordToken.token_digest == digest,
                    PasswordToken.revoked_at.is_(None),
                    PasswordToken.expires_at > func.clock_timestamp(),
                )
            )
        ).scalar_one_or_none()

    async def issue_setup(self, user_id: UUID, digest: str, expires_at: datetime):
        await self._session.execute(
            update(PasswordToken)
            .where(PasswordToken.user_id == user_id, PasswordToken.revoked_at.is_(None))
            .values(revoked_at=func.clock_timestamp())
        )
        self._session.add(
            PasswordToken(id=uuid4(), user_id=user_id, token_digest=digest, expires_at=expires_at)
        )

    async def consume_setup(self, digest: str, user_id: UUID, password_hash: str) -> bool:
        row = (
            await self._session.execute(
                update(PasswordToken)
                .where(
                    PasswordToken.token_digest == digest,
                    PasswordToken.user_id == user_id,
                    PasswordToken.revoked_at.is_(None),
                    PasswordToken.expires_at > func.clock_timestamp(),
                )
                .values(revoked_at=func.clock_timestamp())
                .returning(PasswordToken.id)
            )
        ).first()
        if not row:
            return False
        await self.replace_password(user_id, password_hash)
        return True

    async def replace_password(self, user_id, password_hash):
        await self._session.execute(
            update(PasswordToken)
            .where(PasswordToken.user_id == user_id, PasswordToken.revoked_at.is_(None))
            .values(revoked_at=func.clock_timestamp())
        )
        await EmailRepository(self._session).clear(user_id, ["reset", "bind"])
        await self._session.execute(
            update(User)
            .where(User.id == user_id)
            .values(password_hash=password_hash, status="active")
        )
        await self._session.execute(
            update(AdminSession)
            .where(AdminSession.user_id == user_id, AdminSession.revoked_at.is_(None))
            .values(revoked_at=func.clock_timestamp())
        )

    async def personal_org_id(self, user_id):
        return (
            await self._session.execute(
                select(Organization.id).where(Organization.personal_owner_id == user_id)
            )
        ).scalar_one()

    async def issue_invitation(self, org_id, login, role, invited_by, digest, expires):
        active_member = (
            select(Membership.id)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.organization_id == org_id,
                Membership.status == "active",
                User.login == login,
            )
        )
        if (await self._session.execute(select(exists(active_member)))).scalar_one():
            raise IdentityError("already_member", "User is already an active member", 409)
        await self.cancel_pending_invitations(org_id, login)
        row = OrganizationInvitation(
            id=uuid4(),
            organization_id=org_id,
            login=login,
            role=role,
            invited_by=invited_by,
            token_digest=digest,
            expires_at=expires,
        )
        self._session.add(row)
        await self._session.flush()
        return view(InvitationView, row)

    async def cancel_pending_invitations(self, org_id, login):
        table = OrganizationInvitation
        await self._session.execute(
            update(table)
            .where(
                table.organization_id == org_id,
                table.login == login,
                table.accepted_at.is_(None),
                table.revoked_at.is_(None),
            )
            .values(revoked_at=func.clock_timestamp())
        )

    async def invitation_org(self, digest):
        return (
            await self._session.execute(
                select(OrganizationInvitation.organization_id).where(
                    OrganizationInvitation.token_digest == digest
                )
            )
        ).scalar_one_or_none()

    async def redeem_invitation(self, org_id, digest, user):
        row = (
            await self._session.execute(
                select(OrganizationInvitation)
                .where(
                    OrganizationInvitation.organization_id == org_id,
                    OrganizationInvitation.token_digest == digest,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.login != user.login or row.revoked_at is not None:
            raise IdentityError(
                "invitation_unavailable", "Invitation unavailable for this account", 404
            )
        member = await self.membership_for_user(org_id, user.id)
        if row.accepted_at is not None:
            # Replaying acceptance must never re-enable a removed member or reapply an old role.
            if row.accepted_by == user.id and member and member.status == "active":
                return member, False
            raise IdentityError("invitation_consumed", "Invitation already consumed", 409)
        now = await self.now()
        if row.expires_at <= now:
            raise IdentityError("invitation_expired", "Invitation has expired", 410)
        if member is None:
            member = await self.create_member(org_id, user.id, row.role)
        elif member.status != "active":
            member = await self.update_member(org_id, member.id, row.role, "active")
        row.accepted_by = user.id
        row.accepted_at = now
        return member, True

    async def invitations(self, org_id, limit, cursor):
        t = OrganizationInvitation
        return await self._page(
            select(t).where(
                t.organization_id == org_id,
                t.accepted_at.is_(None),
                t.revoked_at.is_(None),
                t.expires_at > func.clock_timestamp(),
            ),
            t,
            InvitationView,
            limit,
            cursor,
        )

    async def revoke_invitation(self, org_id, invitation_id):
        t = OrganizationInvitation
        return (
            await self._session.execute(
                update(t)
                .where(
                    t.organization_id == org_id,
                    t.id == invitation_id,
                    t.accepted_at.is_(None),
                    t.revoked_at.is_(None),
                )
                .values(revoked_at=func.clock_timestamp())
                .returning(t.id)
            )
        ).scalar_one_or_none()

    async def organization(self, organization_id: UUID, *, lock: bool = False):
        stmt = (
            select(Organization)
            .where(Organization.id == organization_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            stmt = stmt.with_for_update()
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return view(OrganizationView, row) if row else None

    async def organizations(self, user_id: UUID | None, limit: int, cursor: Cursor):
        stmt = select(Organization)
        if user_id is not None:
            stmt = stmt.where(
                exists(
                    select(Membership.id).where(
                        Membership.organization_id == Organization.id,
                        Membership.user_id == user_id,
                        Membership.status == "active",
                    )
                )
            )
        return await self._page(stmt, Organization, OrganizationView, limit, cursor)

    async def create_organization(self, name: str, owner_id: UUID):
        row = Organization(id=uuid4(), name=name, status="provisioning", version=1)
        self._session.add(row)
        await self._session.flush()
        await self.create_member(row.id, owner_id, "org_owner")
        self._session.add(
            OutboxEvent(
                id=uuid4(),
                topic=ORGANIZATION_CREATED,
                schema_version=ORGANIZATION_CREATED_VERSION,
                aggregate_id=row.id,
                aggregate_version=1,
                payload={"organization_id": str(row.id), "owner_user_id": str(owner_id)},
            )
        )
        return view(OrganizationView, row)

    async def update_organization(
        self, organization_id: UUID, name: str | None, status: str | None
    ):
        row = (
            await self._session.execute(
                select(Organization).where(Organization.id == organization_id)
            )
        ).scalar_one()
        if status is not None and status != row.status:
            if status == "suspended" and row.status in {"active", "provisioning"}:
                row.resume_status = row.status
            elif row.status == "suspended" and status == row.resume_status:
                row.resume_status = None
            elif status == "archived":
                row.resume_status = None
            else:
                raise IdentityError(
                    "invalid_transition", "Organization cannot enter that state", 409
                )
            row.status = status
        if name is not None:
            row.name = name.strip()
        row.version += 1
        await self._session.flush()
        return view(OrganizationView, row)

    async def membership_for_user(self, organization_id: UUID, user_id: UUID):
        row = (
            await self._session.execute(
                select(Membership).where(
                    Membership.organization_id == organization_id, Membership.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        return view(MemberView, row) if row else None

    async def membership(self, organization_id: UUID, membership_id: UUID):
        row = (
            await self._session.execute(
                select(Membership).where(
                    Membership.organization_id == organization_id, Membership.id == membership_id
                )
            )
        ).scalar_one_or_none()
        return view(MemberView, row) if row else None

    async def my_memberships(self, user_id: UUID):
        rows = (
            await self._session.execute(
                select(Membership)
                .where(Membership.user_id == user_id, Membership.status == "active")
                .order_by(Membership.created_at, Membership.id)
            )
        ).scalars()
        return [view(MemberView, row) for row in rows]

    async def members(self, organization_id: UUID, limit: int, cursor: Cursor):
        page = await self._page(
            select(Membership).where(Membership.organization_id == organization_id),
            Membership,
            MemberView,
            limit,
            cursor,
        )
        logins = dict(
            (
                await self._session.execute(
                    select(User.id, User.login).where(User.id.in_([m.user_id for m in page.items]))
                )
            ).all()
        )
        return Page[MemberListItem](
            items=[MemberListItem(**m.model_dump(), login=logins[m.user_id]) for m in page.items],
            next_cursor=page.next_cursor,
        )

    async def create_member(self, organization_id: UUID, user_id: UUID, role: str):
        row = Membership(
            id=uuid4(),
            organization_id=organization_id,
            user_id=user_id,
            role=role,
            status="active",
            version=1,
        )
        self._session.add(row)
        await self._session.flush()
        return view(MemberView, row)

    async def update_member(
        self, organization_id: UUID, membership_id: UUID, role: str, status: str
    ):
        await self._session.execute(
            update(Membership)
            .where(Membership.organization_id == organization_id, Membership.id == membership_id)
            .values(role=role, status=status, version=Membership.version + 1)
        )
        return await self.membership(organization_id, membership_id)

    async def active_owner_count(self, organization_id: UUID):
        return (
            await self._session.execute(
                select(func.count())
                .select_from(Membership)
                .join(User, User.id == Membership.user_id)
                .where(
                    Membership.organization_id == organization_id,
                    Membership.status == "active",
                    Membership.role == "org_owner",
                    User.status == "active",
                )
            )
        ).scalar_one()


class AdministratorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_bootstrap(self) -> None:
        await self._session.execute(text("SELECT pg_advisory_xact_lock(336170374483)"))

    async def first_admin(self) -> tuple[UUID, str] | None:
        row = (
            await self._session.execute(
                select(User.id, User.login)
                .join(PlatformRole, PlatformRole.user_id == User.id)
                .where(PlatformRole.role == "platform_admin")
                .limit(1)
            )
        ).first()
        return (row[0], row[1]) if row else None

    async def login_exists(self, login: str) -> bool:
        return (
            await self._session.execute(select(User.id).where(User.login == login))
        ).first() is not None

    async def create(self, user_id: UUID, login: str, password_hash: str) -> None:
        self._session.add(User(id=user_id, login=login, password_hash=password_hash))
        await self._session.flush()
        self._session.add(PlatformRole(user_id=user_id, role="platform_admin"))


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_bootstrap(self, user_id: UUID) -> None:
        self._session.add(
            AuditEvent(
                id=uuid4(),
                actor_type="system",
                action="administrator.bootstrapped",
                resource_id=user_id,
                details={"source": "bootstrap_cli"},
            )
        )

    async def record_system(self, action: str, resource_id: UUID, details: dict) -> None:
        """An operator command run outside any session, such as a settings import."""
        self._session.add(
            AuditEvent(
                id=uuid4(),
                actor_type="system",
                action=action,
                resource_id=resource_id,
                details=details,
            )
        )

    async def record(
        self,
        actor_id: UUID,
        action: str,
        resource_id: UUID,
        *,
        organization_id: UUID | None = None,
        details: dict | None = None,
    ):
        self._session.add(
            AuditEvent(
                id=uuid4(),
                actor_type="user",
                actor_id=actor_id,
                action=action,
                resource_id=resource_id,
                organization_id=organization_id,
                details=details or {},
            )
        )


def integrity_conflict(error):
    diagnostic = getattr(error.orig, "diag", None)
    constraint = getattr(diagnostic, "constraint_name", None)
    if constraint == "users_login_key":
        return IdentityError("login_in_use", "Login is already in use", 409)
    if constraint == "users_verified_email_key":
        return IdentityError("email_in_use", "Email is already bound to an account", 409)
    return IdentityError("conflict", "Resource already exists or conflicts with current state", 409)


class IdentityUnitOfWork:
    def __init__(self, database: ModuleDatabase) -> None:
        self._context = database.transaction()

    async def __aenter__(self) -> IdentityUnitOfWork:
        try:
            session = await self._context.__aenter__()
        except SQLAlchemyError:
            raise IdentityError(
                "storage_unavailable", "Storage is temporarily unavailable", 503
            ) from None
        self.administrators = AdministratorRepository(session)
        self.audit = AuditRepository(session)
        self.directory = DirectoryRepository(session)
        self.email = EmailRepository(session)
        self.applications = ApplicationRepository(session)
        self.policies = PolicyRepository(session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        try:
            result = await self._context.__aexit__(exc_type, exc, traceback)
            if isinstance(exc, IntegrityError):
                raise integrity_conflict(exc)
            if isinstance(exc, SQLAlchemyError):
                raise IdentityError(
                    "storage_unavailable", "Storage is temporarily unavailable", 503
                )
            return result
        except IntegrityError as error:
            raise integrity_conflict(error) from None
        except SQLAlchemyError:
            raise IdentityError(
                "storage_unavailable", "Storage is temporarily unavailable", 503
            ) from None
