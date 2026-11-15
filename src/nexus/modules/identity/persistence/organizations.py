from sqlalchemy import text


def create_organization_source(database):
    """Active organizations for rule-based gateway scopes: identifiers and personal flags only.

    Every organization insert or status change advances identity.source_revision, so a reader
    can tell whether the set may have changed without calling this again.
    """

    async def capture():
        async with database.transaction() as session:
            rows = await session.execute(
                text(
                    "SELECT id, personal_owner_id IS NOT NULL AS personal "
                    "FROM identity.organizations WHERE status='active' ORDER BY id"
                )
            )
            return [{"id": row.id, "personal": row.personal} for row in rows]

    return capture
