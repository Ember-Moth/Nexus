from sqlalchemy import text


def create_identity_source(database):
    async def capture(orgs):
        async with database.transaction() as session:

            async def rows(query):
                return [
                    dict(r) for r in (await session.execute(text(query), {"orgs": orgs})).mappings()
                ]

            revision = (
                await session.execute(
                    text("SELECT revision FROM identity.source_revision WHERE id=1 FOR SHARE")
                )
            ).scalar_one()
            keys = await rows("""
                SELECT k.id, k.organization_id, k.project_id, k.service_account_id, k.digest,
                k.expires_at
                FROM identity.api_keys k JOIN identity.organizations o ON o.id=k.organization_id
                JOIN identity.projects p ON p.id=k.project_id JOIN identity.service_accounts s
                ON s.id=k.service_account_id
                WHERE o.id=ANY(CAST(:orgs AS uuid[])) AND o.status='active' AND p.status='active'
                AND s.status='active' AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR
                k.expires_at>clock_timestamp())
                ORDER BY k.id
            """)
            policies = await rows("""
                SELECT p.organization_id,p.layer,p.scope_type,p.scope_id,ARRAY(
                 SELECT m.model_id FROM identity.access_policy_models m WHERE m.policy_id=p.id
                 ORDER BY m.model_id
                ) AS allowed_models FROM identity.access_policies p
                WHERE p.organization_id=ANY(CAST(:orgs AS uuid[])) ORDER BY p.id
            """)
            active = await rows(
                "SELECT id FROM identity.organizations WHERE id=ANY(CAST(:orgs AS "
                "uuid[])) AND status='active'"
            )
            if set(orgs) != {r["id"] for r in active}:
                raise ValueError("Organization scope contains an inactive or missing organization")
            return revision, {"key_verifiers": keys, "access_policies": policies}

    return capture
