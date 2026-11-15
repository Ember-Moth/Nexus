from sqlalchemy import text


def create_organization_scope(database):
    """Resolve rule-based cluster scopes through identity's read-only SQL contract."""

    async def resolve(personal):
        async with database.transaction() as session:
            rows = await session.execute(
                text("SELECT identity.v1_scope_organizations(:personal)"), {"personal": personal}
            )
            return list(rows.scalars())

    return resolve
