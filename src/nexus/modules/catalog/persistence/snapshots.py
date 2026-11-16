import json

from nacl.public import PublicKey, SealedBox
from sqlalchemy import text

from .routing import COUNTS

# Quotas the Relay enforces through Redis; they travel in the snapshot's quota_policies.
RATES = "('rpm','tpm','concurrency','admission_concurrency')"


async def resource_limits(rows):
    """Platform resource counts and the effective counts of every overridden organization."""
    defaults = (await rows(f"SELECT {','.join(COUNTS)} FROM catalog.resource_quota_defaults"))[0]
    overrides = await rows(
        "SELECT organization_id,metric,limit_value FROM catalog.quota_policies "
        f"WHERE organization_id=ANY(CAST(:orgs AS uuid[])) AND metric NOT IN {RATES} "
        "ORDER BY organization_id,metric"
    )
    organizations = {}
    for row in overrides:
        values = organizations.setdefault(row["organization_id"], dict(defaults))
        values[row["metric"]] = row["limit_value"]
    return {
        "default": defaults,
        "organizations": [{"organization_id": org, **v} for org, v in organizations.items()],
    }


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
                d.credential_id, d.relay_profile,
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
                m.usage_schema, m.token_dimensions, m.tool_dimensions,
                m.max_input_tokens, m.max_output_tokens FROM catalog.deployments d JOIN
                catalog.models m ON m.id=d.model_id
                WHERE d.id=ANY(CAST(:deps AS uuid[])) ORDER BY m.id
            """)
            box = SealedBox(PublicKey(public_key))
            envelopes = {}
            for d in data:
                profile = d["relay_profile"]
                if profile is not None:
                    d["usage_format"] = {
                        "openai_responses": "openai.responses.v1",
                        "openai_chat_completions": "openai.chat.v1",
                        "bedrock_converse": "aws.bedrock.converse.v1",
                        "anthropic_messages": "anthropic.messages.v1",
                    }[profile["endpoint"]]
                    if profile.get("bedrock_invoke"):
                        d["usage_format"] = "anthropic.messages.v1"
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
                f"organization_id=ANY(CAST(:orgs AS uuid[])) AND metric IN {RATES} ORDER BY id"
            )
            return (
                revision,
                {
                    "resource_limits": await resource_limits(rows),
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
