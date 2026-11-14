import base64
import json
from datetime import datetime
from uuid import uuid4

from sqlalchemy import exists, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..application.ports import Cursor
from ..application.schemas import (
    ApplicationIdentity,
    GrantView,
    KeyView,
    Page,
    ProjectView,
    ServiceAccountView,
)
from .models import (
    ApiKey,
    IdempotencyRecord,
    Membership,
    Organization,
    Project,
    ProjectGrant,
    ServiceAccount,
)


def view(kind, row):
    return kind(**{name: getattr(row, name) for name in kind.model_fields})


class ApplicationRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

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
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([last.created_at.isoformat(), str(last.id)]).encode()
            ).decode()
        return Page[kind](items=[view(kind, row) for row in rows[:limit]], next_cursor=next_cursor)

    def _grant_filter(self, organization_id, membership_id, project_column):
        return exists(
            select(ProjectGrant.id)
            .join(Membership, Membership.id == ProjectGrant.membership_id)
            .where(
                ProjectGrant.organization_id == organization_id,
                ProjectGrant.project_id == project_column,
                ProjectGrant.membership_id == membership_id,
                ProjectGrant.status == "active",
                Membership.organization_id == organization_id,
                Membership.status == "active",
                Membership.role == "org_developer",
            )
        )

    async def has_grant(self, organization_id, project_id, membership_id) -> bool:
        return bool(
            (
                await self._session.execute(
                    select(Project.id).where(
                        Project.organization_id == organization_id,
                        Project.id == project_id,
                        self._grant_filter(organization_id, membership_id, Project.id),
                    )
                )
            ).first()
        )

    async def granted_projects(self, org_id, member_id):
        return list(
            (
                await self._session.execute(
                    select(Project.id).where(
                        Project.organization_id == org_id,
                        self._grant_filter(org_id, member_id, Project.id),
                    )
                )
            ).scalars()
        )

    async def projects(self, organization_id, membership_id, limit, cursor):
        stmt = select(Project).where(Project.organization_id == organization_id)
        if membership_id is not None:
            stmt = stmt.where(self._grant_filter(organization_id, membership_id, Project.id))
        return await self._page(stmt, Project, ProjectView, limit, cursor)

    async def project(self, organization_id, project_id):
        row = (
            await self._session.execute(
                select(Project).where(
                    Project.organization_id == organization_id, Project.id == project_id
                )
            )
        ).scalar_one_or_none()
        return view(ProjectView, row) if row else None

    async def create_project(self, organization_id, name):
        row = Project(
            id=uuid4(), organization_id=organization_id, name=name, status="active", version=1
        )
        self._session.add(row)
        await self._session.flush()
        return view(ProjectView, row)

    async def update_project(self, organization_id, project_id, changes):
        await self._session.execute(
            update(Project)
            .where(Project.organization_id == organization_id, Project.id == project_id)
            .values(**changes, version=Project.version + 1)
        )
        return await self.project(organization_id, project_id)

    async def grants(self, organization_id, limit, cursor):
        return await self._page(
            select(ProjectGrant).where(ProjectGrant.organization_id == organization_id),
            ProjectGrant,
            GrantView,
            limit,
            cursor,
        )

    async def grant(self, organization_id, grant_id):
        row = (
            await self._session.execute(
                select(ProjectGrant).where(
                    ProjectGrant.organization_id == organization_id, ProjectGrant.id == grant_id
                )
            )
        ).scalar_one_or_none()
        return view(GrantView, row) if row else None

    async def create_grant(self, organization_id, project_id, membership_id):
        row = ProjectGrant(
            id=uuid4(),
            organization_id=organization_id,
            project_id=project_id,
            membership_id=membership_id,
            status="active",
            version=1,
        )
        self._session.add(row)
        await self._session.flush()
        return view(GrantView, row)

    async def update_grant(self, organization_id, grant_id, status):
        await self._session.execute(
            update(ProjectGrant)
            .where(ProjectGrant.organization_id == organization_id, ProjectGrant.id == grant_id)
            .values(status=status, version=ProjectGrant.version + 1)
        )
        return await self.grant(organization_id, grant_id)

    async def service_account(self, organization_id, service_account_id):
        row = (
            await self._session.execute(
                select(ServiceAccount).where(
                    ServiceAccount.organization_id == organization_id,
                    ServiceAccount.id == service_account_id,
                )
            )
        ).scalar_one_or_none()
        return view(ServiceAccountView, row) if row else None

    async def service_accounts(self, organization_id, membership_id, project_id, limit, cursor):
        stmt = select(ServiceAccount).where(ServiceAccount.organization_id == organization_id)
        if project_id:
            stmt = stmt.where(ServiceAccount.project_id == project_id)
        if membership_id:
            stmt = stmt.where(
                self._grant_filter(organization_id, membership_id, ServiceAccount.project_id)
            )
        return await self._page(stmt, ServiceAccount, ServiceAccountView, limit, cursor)

    async def create_service_account(self, organization_id, project_id, name):
        row = ServiceAccount(
            id=uuid4(),
            organization_id=organization_id,
            project_id=project_id,
            name=name,
            status="active",
            version=1,
        )
        self._session.add(row)
        await self._session.flush()
        return view(ServiceAccountView, row)

    async def update_service_account(self, organization_id, service_account_id, changes):
        await self._session.execute(
            update(ServiceAccount)
            .where(
                ServiceAccount.organization_id == organization_id,
                ServiceAccount.id == service_account_id,
            )
            .values(**changes, version=ServiceAccount.version + 1)
        )
        return await self.service_account(organization_id, service_account_id)

    async def key(self, organization_id, key_id):
        row = (
            await self._session.execute(
                select(ApiKey).where(ApiKey.organization_id == organization_id, ApiKey.id == key_id)
            )
        ).scalar_one_or_none()
        return view(KeyView, row) if row else None

    async def keys(
        self, organization_id, membership_id, project_id, service_account_id, limit, cursor
    ):
        stmt = select(ApiKey).where(ApiKey.organization_id == organization_id)
        if project_id:
            stmt = stmt.where(ApiKey.project_id == project_id)
        if service_account_id:
            stmt = stmt.where(ApiKey.service_account_id == service_account_id)
        if membership_id:
            stmt = stmt.where(self._grant_filter(organization_id, membership_id, ApiKey.project_id))
        return await self._page(stmt, ApiKey, KeyView, limit, cursor)

    async def create_key(
        self,
        organization_id,
        project_id,
        service_account_id,
        name,
        digest,
        prefix,
        expires_at,
        rotated_from_id=None,
    ):
        row = ApiKey(
            id=uuid4(),
            organization_id=organization_id,
            project_id=project_id,
            service_account_id=service_account_id,
            name=name,
            digest=digest,
            prefix=prefix,
            expires_at=expires_at,
            rotated_from_id=rotated_from_id,
            version=1,
        )
        self._session.add(row)
        await self._session.flush()
        return view(KeyView, row)

    async def revoke_key(self, organization_id, key_id, at: datetime):
        await self._session.execute(
            update(ApiKey)
            .where(
                ApiKey.organization_id == organization_id,
                ApiKey.id == key_id,
                ApiKey.revoked_at.is_(None),
            )
            .values(revoked_at=at, version=ApiKey.version + 1)
        )
        return await self.key(organization_id, key_id)

    async def retire_key(self, organization_id, key_id, at, expires_at, immediate):
        values = {"rotated_at": at, "version": ApiKey.version + 1}
        if immediate:
            values["revoked_at"] = at
        else:
            values["expires_at"] = expires_at
        await self._session.execute(
            update(ApiKey)
            .where(ApiKey.organization_id == organization_id, ApiKey.id == key_id)
            .values(**values)
        )

    async def replay(self, organization_id, actor_id, operation, key_digest):
        row = (
            await self._session.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.organization_id == organization_id,
                    IdempotencyRecord.actor_id == actor_id,
                    IdempotencyRecord.operation == operation,
                    IdempotencyRecord.key_digest == key_digest,
                )
            )
        ).scalar_one_or_none()
        return (row.request_digest, row.resource_id) if row else None

    async def remember(
        self, organization_id, actor_id, operation, key_digest, request_digest, resource_id
    ):
        self._session.add(
            IdempotencyRecord(
                id=uuid4(),
                organization_id=organization_id,
                actor_id=actor_id,
                operation=operation,
                key_digest=key_digest,
                request_digest=request_digest,
                resource_id=resource_id,
            )
        )

    async def authenticate(self, digest):
        row = (
            await self._session.execute(
                select(ApiKey)
                .join(ServiceAccount, ServiceAccount.id == ApiKey.service_account_id)
                .join(Project, Project.id == ApiKey.project_id)
                .join(Organization, Organization.id == ApiKey.organization_id)
                .where(
                    ApiKey.digest == digest,
                    ApiKey.revoked_at.is_(None),
                    (ApiKey.expires_at.is_(None) | (ApiKey.expires_at > func.clock_timestamp())),
                    ServiceAccount.status == "active",
                    Project.status == "active",
                    Organization.status == "active",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return ApplicationIdentity(
            organization_id=row.organization_id,
            project_id=row.project_id,
            service_account_id=row.service_account_id,
            key_id=row.id,
        )
