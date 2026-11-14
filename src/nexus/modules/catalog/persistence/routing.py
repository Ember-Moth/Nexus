import json
from uuid import uuid4

from sqlalchemy import text

from nexus.shared.errors import ServiceError


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

    async def save_quota(self, org, body, version, scope, actor):
        async with self._database.transaction() as session:
            params = {
                "id": uuid4(),
                "org": org,
                "layer": body.layer,
                "scope": body.scope_type,
                "sid": scope,
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

    async def list_routes(self):
        async with self._database.transaction() as session:
            return [
                dict(r)
                for r in (
                    await session.execute(
                        text(
                            "SELECT * FROM catalog.route_policies ORDER BY organization_id,model_id"
                        )
                    )
                ).mappings()
            ]

    async def list_quotas(self, org):
        async with self._database.transaction() as session:
            return [
                dict(r)
                for r in (
                    await session.execute(
                        text(
                            "SELECT * FROM catalog.quota_policies WHERE "
                            "organization_id=:org ORDER BY id"
                        ),
                        {"org": org},
                    )
                ).mappings()
            ]
