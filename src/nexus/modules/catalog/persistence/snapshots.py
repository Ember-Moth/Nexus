import json

from nacl.public import PublicKey, SealedBox
from sqlalchemy import text


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
