import json
from uuid import uuid4

from nacl.public import PublicKey, SealedBox
from sqlalchemy import text

from nexus.shared.errors import ServiceError


def create_catalog_source(database, cipher):
    async def capture(orgs, deployments, public_key, cluster_id, key_version):
        async with database.transaction() as session:

            async def rows(query):
                return [
                    dict(row)
                    for row in (
                        await session.execute(text(query), {"orgs": orgs, "deps": deployments})
                    ).mappings()
                ]

            revision = (
                await session.execute(
                    text("SELECT revision FROM catalog.source_revision WHERE id=1 FOR SHARE")
                )
            ).scalar_one()
            data = await rows("""
                SELECT d.id, d.version, d.model_id, d.upstream_id, d.upstream_model, d.region,
                d.credential_id,
                u.version upstream_version, u.base_url, u.protocol_family, c.version
                credential_version, c.key_version, c.ciphertext
                FROM catalog.deployments d JOIN catalog.upstreams u ON u.id=d.upstream_id
                JOIN catalog.upstream_credentials c ON c.id=d.credential_id JOIN catalog.models
                m ON m.id=d.model_id
                WHERE d.id=ANY(CAST(:deps AS uuid[])) AND d.status='active' AND
                u.status='active' AND c.status='active'
                AND m.status='active' AND m.protocols ? u.protocol_family ORDER BY d.id
            """)
            if set(deployments) != {d["id"] for d in data}:
                raise ValueError("Deployment scope contains an unavailable deployment")
            models = await rows("""
                SELECT DISTINCT m.id, m.version, m.public_name, m.protocols, m.capabilities,
                m.usage_schema, m.token_dimensions,
                m.max_input_tokens, m.max_output_tokens FROM catalog.deployments d JOIN
                catalog.models m ON m.id=d.model_id
                WHERE d.id=ANY(CAST(:deps AS uuid[])) ORDER BY m.id
            """)
            box = SealedBox(PublicKey(public_key))
            envelopes = {}
            for d in data:
                cid = d["credential_id"]
                material = d.pop("ciphertext")
                root = d.pop("key_version")
                if cid not in envelopes:
                    secret = cipher.decrypt(material, d["upstream_id"], cid, root)
                    bound = {
                        "cluster_id": str(cluster_id),
                        "credential_id": str(cid),
                        "credential_version": d["credential_version"],
                        "key_version": key_version,
                        "secret": secret,
                    }
                    envelopes[cid] = (
                        d["credential_version"],
                        box.encrypt(json.dumps(bound).encode()),
                    )
            routes = await rows(
                "SELECT organization_id,model_id,candidates FROM "
                "catalog.route_policies WHERE organization_id=ANY(CAST(:orgs AS "
                "uuid[])) ORDER BY organization_id,model_id"
            )
            allowed = {str(v) for v in deployments}
            for route in routes:
                route["candidates"] = [
                    c for c in route["candidates"] if c["deployment_id"] in allowed
                ]
            quotas = await rows(
                "SELECT id,organization_id,layer,scope_type,scope_id,metric,"
                "limit_value FROM catalog.quota_policies WHERE "
                "organization_id=ANY(CAST(:orgs AS uuid[])) ORDER BY id"
            )
            return (
                revision,
                {
                    "models": models,
                    "deployments": data,
                    "routes": [r for r in routes if r["candidates"]],
                    "quota_policies": quotas,
                    "credential_refs": [
                        {
                            "credential_id": k,
                            "credential_version": v[0],
                            "key_version": key_version,
                            "algorithm": "libsodium-sealed-box",
                        }
                        for k, v in envelopes.items()
                    ],
                },
                envelopes,
            )

    return capture


class RoutingPolicies:
    def __init__(self, database, access):
        self._database = database
        self._access = access

    async def _edit(self, session, query, params, expected):
        await session.execute(text("SELECT pg_advisory_xact_lock(336170374485)"))
        actual = (await session.execute(text(query), params)).scalar_one_or_none()
        if actual is not None and expected is None:
            raise ServiceError("precondition_required", "Updating a policy requires If-Match", 428)
        if actual != expected:
            raise ServiceError("version_conflict", "Policy version changed", 412)

    async def route(self, token, body, version):
        actor = await self._access(token, body.organization_id, operation="pricing")
        ids = [c.deployment_id for c in body.candidates]
        if len(set(ids)) != len(ids):
            raise ServiceError("duplicate_candidate", "Duplicate route candidate", 422)
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
                text("""INSERT INTO catalog.audit_events(id,actor_id,action,resource_id,details)
                VALUES(:id,:actor,'route.configured',:model,cast(:details AS jsonb))"""),
                {
                    "id": uuid4(),
                    "actor": actor.actor_id,
                    "model": body.model_id,
                    "details": body.model_dump_json(),
                },
            )
        return {**body.model_dump(), "version": (version or 0) + 1}

    async def quota(self, token, org, body, version):
        actor = await self._access(
            token,
            org,
            operation="budget",
            layer=body.layer,
            scope_type=body.scope_type,
            scope_id=body.scope_id,
        )
        scope = body.scope_id or org
        if body.scope_type != "organization" and body.scope_id is None:
            raise ServiceError("invalid_scope", "Scope identifier is required", 422)
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
                text("""INSERT INTO catalog.audit_events(id,actor_id,action,resource_id,details)
                VALUES(:id,:actor,'quota.configured',:policy,cast(:details AS jsonb))"""),
                {
                    "id": uuid4(),
                    "actor": actor.actor_id,
                    "policy": policy_id,
                    "details": body.model_dump_json(),
                },
            )
        return {**body.model_dump(), "id": policy_id, "version": (version or 0) + 1}

    async def list_routes(self, token):
        await self._access(token, operation="pricing")
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

    async def list_quotas(self, token, org):
        await self._access(token, org)
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
