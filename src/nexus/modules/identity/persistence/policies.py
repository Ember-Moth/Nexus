import base64
import json
from uuid import uuid4

from sqlalchemy import and_, delete, exists, or_, select, tuple_, update

from ..application.schemas import AccessPolicyView, Page
from .models import AccessPolicy, AccessPolicyModel, ProjectGrant


class PolicyRepository:
    def __init__(self, session):
        self._session = session

    async def _views(self, rows):
        if not rows:
            return []
        models = {row.id: [] for row in rows}
        for policy_id, model_id in (
            await self._session.execute(
                select(AccessPolicyModel.policy_id, AccessPolicyModel.model_id).where(
                    AccessPolicyModel.policy_id.in_(models)
                )
            )
        ).all():
            models[policy_id].append(model_id)
        return [
            AccessPolicyView(
                **{
                    name: getattr(row, name)
                    for name in AccessPolicyView.model_fields
                    if name != "allowed_models"
                },
                allowed_models=sorted(models[row.id]),
            )
            for row in rows
        ]

    async def get(self, org_id, policy_id):
        row = (
            await self._session.execute(
                select(AccessPolicy).where(
                    AccessPolicy.organization_id == org_id, AccessPolicy.id == policy_id
                )
            )
        ).scalar_one_or_none()
        return (await self._views([row]))[0] if row else None

    async def applicable(self, org_id, project_id, key_id):
        scopes = [AccessPolicy.scope_type == "organization"]
        if project_id:
            scopes.append(
                and_(AccessPolicy.scope_type == "project", AccessPolicy.project_id == project_id)
            )
        if key_id:
            scopes.append(and_(AccessPolicy.scope_type == "key", AccessPolicy.api_key_id == key_id))
        rows = (
            (
                await self._session.execute(
                    select(AccessPolicy).where(AccessPolicy.organization_id == org_id, or_(*scopes))
                )
            )
            .scalars()
            .all()
        )
        return await self._views(rows)

    async def list(self, org_id, membership_id, limit, cursor):
        stmt = select(AccessPolicy).where(AccessPolicy.organization_id == org_id)
        if membership_id:
            stmt = stmt.where(
                or_(
                    AccessPolicy.scope_type == "organization",
                    exists(
                        select(ProjectGrant.id).where(
                            ProjectGrant.organization_id == org_id,
                            ProjectGrant.project_id == AccessPolicy.project_id,
                            ProjectGrant.membership_id == membership_id,
                            ProjectGrant.status == "active",
                        )
                    ),
                )
            )
        if cursor:
            stmt = stmt.where(tuple_(AccessPolicy.created_at, AccessPolicy.id) > cursor)
        rows = (
            (
                await self._session.execute(
                    stmt.order_by(AccessPolicy.created_at, AccessPolicy.id).limit(limit + 1)
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
        return Page[AccessPolicyView](
            items=await self._views(rows[:limit]), next_cursor=next_cursor
        )

    async def create(self, org_id, layer, scope_type, scope_id, project_id, key_id, models):
        row = AccessPolicy(
            id=uuid4(),
            organization_id=org_id,
            layer=layer,
            scope_type=scope_type,
            scope_id=scope_id,
            project_id=project_id,
            api_key_id=key_id,
            version=1,
        )
        self._session.add(row)
        await self._session.flush()
        for model_id in models:
            self._session.add(AccessPolicyModel(policy_id=row.id, model_id=model_id))
        await self._session.flush()
        return await self.get(org_id, row.id)

    async def replace(self, org_id, policy_id, models):
        await self._session.execute(
            delete(AccessPolicyModel).where(AccessPolicyModel.policy_id == policy_id)
        )
        for model_id in models:
            self._session.add(AccessPolicyModel(policy_id=policy_id, model_id=model_id))
        await self._session.execute(
            update(AccessPolicy)
            .where(AccessPolicy.organization_id == org_id, AccessPolicy.id == policy_id)
            .values(version=AccessPolicy.version + 1)
        )
        await self._session.flush()
        return await self.get(org_id, policy_id)
