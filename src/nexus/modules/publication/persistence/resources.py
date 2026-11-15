"""Upstream copies held for API keys, and owner requests to purge them.

Requested purges are rows of ``publication.upstream_purges`` like the automatic ones, so the
node purge loop deletes them, retries failures and audits each outcome. The table's unique
upstream locator keeps one purge per copy whoever queues it first.
"""

import json
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from nexus.shared.errors import ServiceError
from nexus.shared.reporting import next_cursor

STATE = """CASE {p}.state WHEN 'pending' THEN 'pending' WHEN 'done' THEN 'purged'
 WHEN 'abandoned' THEN 'failed' ELSE {fallback} END"""
PURGE = """{p}.id AS purge_id,{p}.state AS purge_state,{p}.reason,{p}.attempts,{p}.last_error,
 {p}.created_at AS purge_created_at,{p}.next_attempt,{p}.finished_at"""
# Bound copies with their purge, then copies whose handle was never saved (orphans).
RESOURCES = (
    """SELECT * FROM (
 SELECT r.kind,r.id,r.api_key_id,r.cluster_id,r.deployment_id,r.created_at,true AS bound,"""
    + STATE.format(p="p", fallback="CASE WHEN r.retained THEN 'retained' ELSE 'deleted' END")
    + " AS state,"
    + PURGE.format(p="p")
    + """
 FROM publication.upstream_resources r
 LEFT JOIN publication.upstream_purges p ON p.resource_id=r.id AND p.kind=r.kind
 WHERE {resource_filter}
 UNION ALL
 SELECT o.kind,o.id,o.api_key_id,o.cluster_id,o.deployment_id,o.created_at,false,"""
    + STATE.format(p="o", fallback="NULL")
    + ","
    + PURGE.format(p="o")
    + """
 FROM publication.upstream_purges o WHERE o.resource_id IS NULL AND {orphan_filter}
) x WHERE {outer_filter} ORDER BY x.created_at DESC,x.id DESC LIMIT :limit"""
)
PROGRESS = """SELECT q.id,q.organization_id,q.api_key_id,q.kind,q.resource_id,q.actor_id,
 q.created_at,q.parameters,count(p.id) total,
 count(p.id) FILTER (WHERE p.state='pending') pending,
 count(p.id) FILTER (WHERE p.state='done') purged,
 count(p.id) FILTER (WHERE p.state='abandoned') failed
 FROM publication.purge_requests q
 LEFT JOIN publication.purge_request_items i ON i.request_id=q.id
 LEFT JOIN publication.upstream_purges p ON p.id=i.purge_id
 WHERE {where} GROUP BY q.id"""
TARGET = "api_key_id=:kid AND (CAST(:rid AS uuid) IS NULL OR (kind=:kind AND {id}=:rid))"


class ResourceRepository:
    def __init__(self, session):
        self.session = session

    async def rows(self, sql, **params):
        return [dict(r) for r in (await self.session.execute(text(sql), params)).mappings()]

    async def resources(self, keys, kind, state, limit, cursor):
        """A page newest first; keys None means every key. cursor: (created_at, id)."""
        params = {"limit": limit + 1, "kind": kind, "state": state}
        resource, orphan = ["true"], ["true"]
        if keys is not None:
            resource.append("r.api_key_id=ANY(CAST(:keys AS uuid[]))")
            orphan.append("o.api_key_id=ANY(CAST(:keys AS uuid[]))")
            params["keys"] = list(keys)
        if kind:
            resource.append("r.kind=:kind")
            orphan.append("o.kind=:kind")
        outer = ["true"]
        if state:
            outer.append("x.state=:state")
        if cursor:
            outer.append("(x.created_at,x.id)<(:at,:after)")
            params.update(at=cursor[0], after=cursor[1])
        rows = await self.rows(
            RESOURCES.format(
                resource_filter=" AND ".join(resource),
                orphan_filter=" AND ".join(orphan),
                outer_filter=" AND ".join(outer),
            ),
            **params,
        )
        last = rows[limit - 1] if len(rows) > limit else None
        return rows[:limit], last and next_cursor(last["created_at"], last["id"])

    async def request(self, request_id, organization_id):
        rows = await self.rows(
            PROGRESS.format(where="q.id=:id AND q.organization_id=:org"),
            id=request_id,
            org=organization_id,
        )
        if not rows:
            raise ServiceError("not_found", "Resource not found", 404)
        return rows[0]

    async def request_purge(self, request_id, organization_id, actor, command, key, parameters):
        """Queue purges for the target and link every purge it covers; replays return the
        original request. Returns the request with its progress."""
        params = {
            "kid": command.api_key_id,
            "kind": command.kind,
            "rid": command.resource_id,
            "org": organization_id,
        }
        created = await self.rows(
            """INSERT INTO publication.purge_requests(id,organization_id,actor_id,api_key_id,kind,
            resource_id,key_digest,parameters) VALUES(:id,:org,:actor,:kid,:kind,:rid,:key,:params)
            ON CONFLICT(organization_id,actor_id,key_digest) DO NOTHING RETURNING created_at""",
            id=request_id,
            actor=actor,
            key=key,
            params=parameters,
            **params,
        )
        if not created:
            prior = await self.rows(
                PROGRESS.format(
                    where="q.organization_id=:org AND q.actor_id=:actor AND q.key_digest=:key"
                ),
                org=organization_id,
                actor=actor,
                key=key,
            )
            if prior[0]["parameters"] != parameters:
                raise ServiceError(
                    "idempotency_conflict",
                    "Idempotency-Key was used with different parameters",
                    409,
                )
            return prior[0]
        if command.resource_id and not await self.rows(
            "SELECT 1 FROM publication.upstream_resources WHERE " + TARGET.format(id="id"),
            **params,
        ):
            raise ServiceError("not_found", "Resource not found", 404)
        # Insert in locator order, as the schedule does, so a concurrent schedule waits on the
        # first shared copy instead of deadlocking. A copy it already queued is left as is.
        queued = await self.rows(
            """INSERT INTO publication.upstream_purges(cluster_id,kind,resource_id,api_key_id,
            organization_id,deployment_id,credential_id,credential_version,endpoint,provider_id,
            reason)
            SELECT cluster_id,kind,id,api_key_id,:org,deployment_id,credential_id,
             credential_version,endpoint,provider_id,'owner_request'
            FROM publication.upstream_resources WHERE retained AND """
            + TARGET.format(id="id")
            + """ ORDER BY cluster_id,kind,deployment_id,provider_id
            ON CONFLICT(cluster_id,kind,deployment_id,provider_id) DO NOTHING RETURNING id""",
            **params,
        )
        # A purge that gave up is retried from scratch.
        retried = await self.rows(
            """UPDATE publication.upstream_purges SET state='pending',attempts=0,
            next_attempt=clock_timestamp(),finished_at=NULL
            WHERE state='abandoned' AND """
            + TARGET.format(id="resource_id")
            + " RETURNING id",
            **params,
        )
        # One resource: its purge in any state. A key: every purge still pending, including
        # copies with no saved handle, and any that finished while this request ran.
        await self.session.execute(
            text(
                """INSERT INTO publication.purge_request_items(request_id,purge_id)
                SELECT :id,id FROM publication.upstream_purges WHERE """
                + TARGET.format(id="resource_id")
                + """ AND (CAST(:rid AS uuid) IS NOT NULL OR state='pending'
                 OR finished_at>=:started)"""
            ),
            {**params, "id": request_id, "started": created[0]["created_at"]},
        )
        result = await self.request(request_id, organization_id)
        await self.session.execute(
            text(
                """INSERT INTO publication.audit_events(organization_id,actor_id,action,
                resource_id,details) VALUES(:org,:actor,'upstream_resource.purge_requested',
                :resource,cast(:details AS jsonb))"""
            ),
            {
                "org": organization_id,
                "actor": actor,
                "resource": command.resource_id or command.api_key_id,
                "details": json.dumps(
                    {
                        "request_id": str(request_id),
                        "api_key_id": str(command.api_key_id),
                        "kind": command.kind,
                        "queued": len(queued),
                        "retried": len(retried),
                        "covered": result["total"],
                    }
                ),
            },
        )
        return result


@asynccontextmanager
async def transaction(database):
    try:
        async with database.transaction() as session:
            yield ResourceRepository(session)
    except SQLAlchemyError:
        raise ServiceError(
            "storage_unavailable", "Publication storage is unavailable", 503
        ) from None
