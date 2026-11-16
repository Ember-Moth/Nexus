import json
from uuid import UUID, uuid4

from sqlalchemy import text

from nexus.shared.errors import ServiceError
from nexus.shared.reporting import key_cursor
from nexus.shared.views import managed_source

from ..application.routing_schemas import ResourceQuotaDefaultsView

COUNTS = (
    "resources_per_key",
    "resources_per_organization",
    "vector_stores_per_key",
    "vector_stores_per_organization",
)
# Audit resource of changes to the singleton resource quota defaults.
DEFAULTS_RESOURCE = UUID(int=0)
# Sentinel: an administrator's write leaves the policy lock as it is (it is refused while set).
KEEP = object()


class RoutingRepository:
    def __init__(self, database):
        self._database = database

    async def _edit(self, session, query, params, expected):
        await session.execute(text("SELECT pg_advisory_xact_lock(336170374485)"))
        actual = (await session.execute(text(query), params)).scalar_one_or_none()
        if actual is not None and expected is None:
            raise ServiceError("precondition_required", "Updating a policy requires If-Match", 428)
        if actual != expected:
            raise ServiceError("version_conflict", "Policy version changed", 412)

    async def save_route(self, body, version, ids, actor):
        async with self._database.transaction() as session:
            await self._edit(
                session,
                "SELECT version FROM catalog.route_policies WHERE "
                "organization_id=:org AND model_id=:model",
                {"org": body.organization_id, "model": body.model_id},
                version,
            )
            valid = (
                (
                    await session.execute(
                        text(
                            "SELECT id FROM catalog.deployments WHERE "
                            "id=ANY(CAST(:ids AS uuid[])) AND model_id=:model AND "
                            "status='active' FOR SHARE"
                        ),
                        {"ids": ids, "model": body.model_id},
                    )
                )
                .scalars()
                .all()
            )
            if set(valid) != set(ids):
                raise ServiceError("invalid_route", "Invalid route candidate", 422)
            await session.execute(
                text("""
                INSERT INTO catalog.route_policies(organization_id, model_id, candidates)
                VALUES(:org, :model, cast(:c AS jsonb))
                ON CONFLICT(organization_id, model_id) DO UPDATE SET
                candidates=excluded.candidates, version=catalog.route_policies.version+1
            """),
                {
                    "org": body.organization_id,
                    "model": body.model_id,
                    "c": json.dumps(body.model_dump(mode="json")["candidates"]),
                },
            )
            await session.execute(
                text("""
                INSERT INTO catalog.audit_events(id,actor_id,action,resource_id,
              organization_id,details)
                VALUES(:id,:actor,'route.configured',:model,:org,cast(:details AS jsonb))"""),
                {
                    "id": uuid4(),
                    "actor": actor.actor_id,
                    "org": actor.organization_id,
                    "model": body.model_id,
                    "details": body.model_dump_json(),
                },
            )

    async def save_quota(self, org, body, version, actor):
        async with self._database.transaction() as session:
            params = {
                "id": uuid4(),
                "org": org,
                "layer": body.layer,
                "scope": body.scope_type,
                "sid": body.scope_id,
                "metric": body.metric,
                "value": body.limit_value,
            }
            await self._edit(
                session,
                """SELECT version FROM catalog.quota_policies WHERE
                organization_id=:org AND layer=:layer AND scope_type=:scope AND scope_id=:sid
                AND metric=:metric""",
                params,
                version,
            )
            result = await session.execute(
                text("""
             INSERT INTO catalog.quota_policies(id, organization_id, layer, scope_type,
             scope_id, metric, limit_value)
             VALUES(:id, :org, :layer, :scope, :sid, :metric, :value) ON
             CONFLICT(organization_id, layer, scope_type, scope_id, metric)
             DO UPDATE SET limit_value=excluded.limit_value,
             version=catalog.quota_policies.version+1 RETURNING id
            """),
                params,
            )
            policy_id = result.scalar_one()
            await session.execute(
                text("""
                INSERT INTO catalog.audit_events(id,actor_id,action,resource_id,
              organization_id,details)
                VALUES(:id,:actor,'quota.configured',:policy,:org,cast(:details AS jsonb))"""),
                {
                    "id": uuid4(),
                    "actor": actor.actor_id,
                    "org": actor.organization_id,
                    "policy": policy_id,
                    "details": body.model_dump_json(),
                },
            )
        return policy_id

    async def resource_defaults(self):
        async with self._database.transaction() as session:
            return await self._defaults(session)

    @staticmethod
    async def _defaults(session, lock=False):
        row = (
            (
                await session.execute(
                    text(
                        f"SELECT {','.join(COUNTS)},version,managed_sha256,managed_at "
                        "FROM catalog.resource_quota_defaults" + (" FOR UPDATE" if lock else "")
                    )
                )
            )
            .mappings()
            .one()
        )
        return ResourceQuotaDefaultsView(
            **{name: row[name] for name in (*COUNTS, "version")}, managed=managed_source(row)
        )

    async def save_resource_defaults(self, body, version, actor, *, managed=KEEP, details=None):
        """Replace the defaults at ``version``; an administrator may not touch a locked row.

        ``managed`` is the SHA-256 of the locking policy file, or None to unlock (imports only).
        """
        async with self._database.transaction() as session:
            before = await self._defaults(session, lock=True)
            if managed is KEEP and before.managed is not None:
                raise ServiceError(
                    "policy_managed_externally",
                    "The resource quota defaults are managed by a policy file",
                    409,
                )
            if before.version != version:
                raise ServiceError("version_conflict", "Resource quota defaults changed", 412)
            assignments = [f"{name}=:{name}" for name in COUNTS]
            params = body.model_dump()
            if managed is not KEEP:
                assignments.append(
                    "managed_sha256=:managed,managed_at=CASE WHEN CAST(:managed AS text) IS NULL "
                    "THEN NULL ELSE clock_timestamp() END"
                )
                params["managed"] = managed
            await session.execute(
                text(
                    "UPDATE catalog.resource_quota_defaults SET "
                    + ",".join(assignments)
                    + ",version=version+1"
                ),
                params,
            )
            after = await self._defaults(session)
            if details is None:
                details = {
                    name: [getattr(before, name), getattr(after, name)]
                    for name in COUNTS
                    if getattr(before, name) != getattr(after, name)
                }
            await session.execute(
                text("""
                INSERT INTO catalog.audit_events(id,actor_id,action,resource_id,details)
                VALUES(:id,:actor,'resource_quotas.updated',:resource,cast(:details AS jsonb))"""),
                {
                    "id": uuid4(),
                    "actor": actor,
                    "resource": DEFAULTS_RESOURCE,
                    "details": json.dumps(details),
                },
            )
        return after

    async def list_routes(self, limit, cursor):
        after = "WHERE (organization_id,model_id)>(:org,:model) " if cursor else ""
        rows = await self._page(
            "SELECT * FROM catalog.route_policies "
            + after
            + "ORDER BY organization_id,model_id LIMIT :limit",
            {"org": cursor and cursor[0], "model": cursor and cursor[1], "limit": limit + 1},
        )
        last = rows[limit - 1] if len(rows) > limit else None
        return rows[:limit], last and key_cursor(last["organization_id"], last["model_id"])

    async def list_quotas(self, org, limit, cursor):
        rows = await self._page(
            "SELECT * FROM catalog.quota_policies WHERE organization_id=:org "
            + ("AND id>:after " if cursor else "")
            + "ORDER BY id LIMIT :limit",
            {"org": org, "after": cursor and cursor[0], "limit": limit + 1},
        )
        last = rows[limit - 1] if len(rows) > limit else None
        return rows[:limit], last and key_cursor(last["id"])

    async def _page(self, query, params):
        async with self._database.transaction() as session:
            return [dict(r) for r in (await session.execute(text(query), params)).mappings()]
