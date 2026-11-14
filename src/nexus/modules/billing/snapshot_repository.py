from sqlalchemy import text


def create_price_source(database):
    async def capture(models, deployments):
        async with database.transaction() as session:
            revision = (
                await session.execute(
                    text("SELECT revision FROM billing.source_revision WHERE id=1 FOR SHARE")
                )
            ).scalar_one()
            rows = (
                (
                    await session.execute(
                        text("""
                SELECT DISTINCT ON (kind,scope_id) kind,scope_id,id,rates,enabled
                FROM billing.price_versions WHERE state='published' AND
                effective_at<=clock_timestamp()
                AND ((kind='sale' AND scope_id=ANY(CAST(:models AS uuid[])))
                OR (kind='cost' AND scope_id=ANY(CAST(:deps AS uuid[]))))
                ORDER BY kind,scope_id,effective_at DESC
            """),
                        {"models": models, "deps": deployments},
                    )
                )
                .mappings()
                .all()
            )
            return revision, {
                (row["kind"], row["scope_id"]): (str(row["id"]), set(row["rates"]))
                for row in rows
                if row["enabled"]
            }

    return capture
