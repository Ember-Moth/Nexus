"""Read only the metadata shared by the modules' audit streams."""

from sqlalchemy import text


def audit_reader(database, module):
    database.require_module(module)

    async def read(org, limit, cursor, action):
        filters = []
        params = {"limit": limit}
        if org is not None:
            filters.append("organization_id=:org")
            params["org"] = org
        if cursor:
            filters.append("(created_at,id)>(:at,:id)")
            params.update(at=cursor[0], id=cursor[1])
        if action:
            filters.append("action=:action")
            params["action"] = action
        where = " WHERE " + " AND ".join(filters) if filters else ""
        async with database.transaction() as session:
            rows = (
                (
                    await session.execute(
                        text(f"""
              SELECT id,actor_id,organization_id,action,resource_id,created_at,
               CASE WHEN details->>'reason' IN ('organization_outside_access_scope','forbidden',
                'authorized_bound_exceeded','reservation_expired','unknown_outcome','invalid_final_usage',
                'missing_final_usage','cost_requires_review') THEN details->>'reason' END reason,
               CASE WHEN details->>'decision_id' ~ '^[0-9a-f-]{{36}}$' THEN
                details->>'decision_id' END decision_id
              FROM {module.value}.audit_events{where} ORDER BY created_at,id LIMIT :limit
            """),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            return [{**r, "module": module.value} for r in rows]

    return read
