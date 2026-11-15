"""Query primitives shared by bounded, read-only sources."""

import base64
import json


def next_cursor(at, row_id):
    return base64.urlsafe_b64encode(json.dumps([at.isoformat(), str(row_id)]).encode()).decode()


def key_cursor(*keys):
    """Cursor for lists ordered by their identifying key columns instead of creation time."""
    return base64.urlsafe_b64encode(json.dumps([str(key) for key in keys]).encode()).decode()


def predicates(org, projects, filters, *, alias="r"):
    expressions = [f"{alias}.organization_id=:org"]
    values = {"org": org}
    if projects is not None:
        expressions.append(f"{alias}.project_id=ANY(CAST(:projects AS uuid[]))")
        values["projects"] = projects
    for field in ("project_id", "api_key_id", "model_id"):
        if filters.get(field):
            expressions.append(f"{alias}.{field}=:{field}")
            values[field] = filters[field]
    return expressions, values


def window(expressions, values, field, filters, cursor=None, id_field="r.id"):
    expressions.append(field + " IS NOT NULL")
    for key, operator in (("start", ">="), ("end", "<")):
        if filters.get(key):
            expressions.append(f"{field}{operator}:{key}")
            values[key] = filters[key]
    if cursor:
        expressions.append(f"({field},{id_field})>(:cursor_at,:cursor_id)")
        values.update(cursor_at=cursor[0], cursor_id=cursor[1])
