import base64
import json
from contextlib import asynccontextmanager
from uuid import uuid4

from sqlalchemy import and_, exists, func, select, text, tuple_, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from nexus.infrastructure.postgres import ModuleDatabase
from nexus.shared.errors import ServiceError

from .models import AuditEvent, CatalogModel, Credential, Deployment, Upstream
from .schemas import CredentialView, DeploymentView, ModelView, Page, UpstreamView


def view(kind, row, **extra):
    return kind(
        **{name: getattr(row, name) for name in kind.model_fields if name not in extra}, **extra
    )


class CatalogRepository:
    def __init__(self, session):
        self._session = session

    async def lock(self):
        # Low-frequency configuration writes serialize reference validation and mutation.
        await self._session.execute(text("SELECT pg_advisory_xact_lock(336170374485)"))

    async def audit(self, actor_id, action, resource_id, details=None):
        self._session.add(
            AuditEvent(
                id=uuid4(),
                actor_id=actor_id,
                action=action,
                resource_id=resource_id,
                details=details or {},
            )
        )

    def _ready(self, versions):
        return and_(
            Deployment.status == "active",
            CatalogModel.status == "active",
            Upstream.status == "active",
            Credential.status == "active",
            Credential.key_version.in_(versions),
            CatalogModel.protocols.op("?")(Upstream.protocol_family),
        )

    def _model_query(self, versions):
        ready = exists(
            select(Deployment.id)
            .join(Upstream, Upstream.id == Deployment.upstream_id)
            .join(Credential, Credential.id == Deployment.credential_id)
            .where(Deployment.model_id == CatalogModel.id, self._ready(versions))
        )
        return select(CatalogModel, ready.label("configured"))

    def _deployment_query(self, versions):
        return (
            select(Deployment, self._ready(versions).label("configured"))
            .join(CatalogModel, CatalogModel.id == Deployment.model_id)
            .join(Upstream, Upstream.id == Deployment.upstream_id)
            .join(Credential, Credential.id == Deployment.credential_id)
        )

    async def _page(self, query, model, kind, limit, cursor, computed=False):
        if cursor:
            query = query.where(tuple_(model.created_at, model.id) > cursor)
        rows = (
            await self._session.execute(query.order_by(model.created_at, model.id).limit(limit + 1))
        ).all()
        items = [
            view(kind, row[0], **({"configured": bool(row[1])} if computed else {}))
            for row in rows[:limit]
        ]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1][0]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps([last.created_at.isoformat(), str(last.id)]).encode()
            ).decode()
        return Page[kind](items=items, next_cursor=next_cursor)

    async def list_models(self, allowed, versions, limit, cursor):
        query = self._model_query(versions)
        if allowed is not None:
            query = query.where(
                CatalogModel.id.in_(allowed),
                CatalogModel.status == "active",
                query.selected_columns.configured,
            )
        return await self._page(query, CatalogModel, ModelView, limit, cursor, True)

    async def model(self, model_id, versions):
        row = (
            await self._session.execute(
                self._model_query(versions).where(CatalogModel.id == model_id)
            )
        ).first()
        return view(ModelView, row[0], configured=bool(row[1])) if row else None

    async def known_models(self, ids):
        return frozenset(
            (
                await self._session.execute(
                    select(CatalogModel.id).where(
                        CatalogModel.id.in_(ids), CatalogModel.status == "active"
                    )
                )
            ).scalars()
        )

    async def create_model(self, values, versions):
        row = CatalogModel(id=uuid4(), version=1, status="active", **values)
        self._session.add(row)
        await self._session.flush()
        return await self.model(row.id, versions)

    async def update_model(self, model_id, values, versions):
        await self._session.execute(
            update(CatalogModel)
            .where(CatalogModel.id == model_id)
            .values(**values, version=CatalogModel.version + 1)
        )
        return await self.model(model_id, versions)

    async def active_protocols(self, model_id):
        return frozenset(
            (
                await self._session.execute(
                    select(Upstream.protocol_family)
                    .join(Deployment, Deployment.upstream_id == Upstream.id)
                    .where(Deployment.model_id == model_id, Deployment.status == "active")
                )
            ).scalars()
        )

    async def upstreams(self, limit, cursor):
        return await self._page(select(Upstream), Upstream, UpstreamView, limit, cursor)

    async def upstream(self, upstream_id):
        row = (
            await self._session.execute(select(Upstream).where(Upstream.id == upstream_id))
        ).scalar_one_or_none()
        return view(UpstreamView, row) if row else None

    async def create_upstream(self, values):
        row = Upstream(id=uuid4(), version=1, status="active", **values)
        self._session.add(row)
        await self._session.flush()
        return view(UpstreamView, row)

    async def update_upstream(self, upstream_id, values):
        await self._session.execute(
            update(Upstream)
            .where(Upstream.id == upstream_id)
            .values(**values, version=Upstream.version + 1)
        )
        return await self.upstream(upstream_id)

    async def credentials(self, upstream_id, limit, cursor):
        return await self._page(
            select(Credential).where(Credential.upstream_id == upstream_id),
            Credential,
            CredentialView,
            limit,
            cursor,
        )

    async def credential(self, upstream_id, credential_id):
        row = (
            await self._session.execute(
                select(Credential).where(
                    Credential.upstream_id == upstream_id, Credential.id == credential_id
                )
            )
        ).scalar_one_or_none()
        return view(CredentialView, row) if row else None

    async def create_credential(self, credential_id, upstream_id, name, key_version, ciphertext):
        row = Credential(
            id=credential_id,
            upstream_id=upstream_id,
            name=name,
            key_version=key_version,
            ciphertext=ciphertext,
            status="active",
            version=1,
        )
        self._session.add(row)
        await self._session.flush()
        return view(CredentialView, row)

    async def revoke_credential(self, upstream_id, credential_id):
        await self._session.execute(
            update(Credential)
            .where(Credential.upstream_id == upstream_id, Credential.id == credential_id)
            .values(
                status="revoked", revoked_at=func.clock_timestamp(), version=Credential.version + 1
            )
        )
        return await self.credential(upstream_id, credential_id)

    async def deployments(self, versions, model_id, upstream_id, limit, cursor):
        query = self._deployment_query(versions)
        if model_id:
            query = query.where(Deployment.model_id == model_id)
        if upstream_id:
            query = query.where(Deployment.upstream_id == upstream_id)
        return await self._page(query, Deployment, DeploymentView, limit, cursor, True)

    async def deployment(self, deployment_id, versions):
        row = (
            await self._session.execute(
                self._deployment_query(versions).where(Deployment.id == deployment_id)
            )
        ).first()
        return view(DeploymentView, row[0], configured=bool(row[1])) if row else None

    async def create_deployment(self, values, versions):
        row = Deployment(id=uuid4(), version=1, **values)
        self._session.add(row)
        await self._session.flush()
        return await self.deployment(row.id, versions)

    async def update_deployment(self, deployment_id, values, versions):
        await self._session.execute(
            update(Deployment)
            .where(Deployment.id == deployment_id)
            .values(**values, version=Deployment.version + 1)
        )
        return await self.deployment(deployment_id, versions)


@asynccontextmanager
async def transaction(database: ModuleDatabase):
    try:
        async with database.transaction() as session:
            yield CatalogRepository(session)
    except IntegrityError:
        raise ServiceError(
            "catalog_conflict",
            "Catalog resource already exists or conflicts with its references",
            409,
        ) from None
    except SQLAlchemyError:
        raise ServiceError(
            "storage_unavailable", "Storage is temporarily unavailable", 503
        ) from None
