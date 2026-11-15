import json
from contextlib import asynccontextmanager
from decimal import localcontext
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from nexus.shared.errors import ServiceError
from nexus.shared.reporting import predicates
from nexus.shared.views import managed_source

from ..application.schemas import ReportingPolicyView

# Sentinel: an administrator's write leaves the policy lock as it is (it is refused while set).
KEEP = object()
# The public view of an export job; a copy past its expiry reads as expired before cleanup.
JOB = """id,organization_id,actor_id,kind,filters,
    CASE WHEN expires_at<=clock_timestamp() THEN 'expired' ELSE state END state,
    row_count,error_code,created_at,completed_at,expires_at"""


@asynccontextmanager
async def transaction(database):
    try:
        async with database.transaction() as session:
            yield ReportingRepository(session)
    except SQLAlchemyError:
        raise ServiceError("storage_unavailable", "Reporting storage is unavailable", 503) from None


class ReportingRepository:
    def __init__(self, session):
        self.session = session

    async def rows(self, query, **values):
        return (await self.session.execute(text(query), values)).mappings().all()

    async def execute(self, query, **values):
        return await self.session.execute(text(query), values)

    async def now(self):
        return (await self.execute("SELECT clock_timestamp()")).scalar_one()

    async def project(self, batch):
        if batch:
            await self.session.execute(
                text("""
                INSERT INTO reporting.request_facts(
                    id,organization_id,project_id,api_key_id,model_id,
                    occurred_at,posted_at,generation,source_at,payload)
                VALUES(:id,:organization_id,:project_id,:api_key_id,:model_id,
                    :occurred_at,:posted_at,:generation,:source_at,cast(:payload AS jsonb))
                ON CONFLICT(id) DO UPDATE SET occurred_at=excluded.occurred_at,
                    posted_at=excluded.posted_at,generation=excluded.generation,
                    source_at=excluded.source_at,copied_at=clock_timestamp(),payload=excluded.payload
                WHERE excluded.generation>reporting.request_facts.generation
            """),
                [{**row, "payload": json.dumps(row["payload"])} for row in batch],
            )

    async def usage(self, org, projects, filters, group_by, costs):
        # Each branch dates a different business fact. A late refund belongs to its posting
        # period, including when the original charge falls outside the selected interval.
        await self.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conditions, values = predicates(org, projects, filters)
        where = " AND ".join(conditions)
        at = "posted_at" if filters["time_basis"] == "posted_at" else "occurred_at"
        grouping = {
            "organization": "organization_id",
            "project": "project_id",
            "key": "api_key_id",
            "model": "model_id",
            "day": "date_trunc('day',at AT TIME ZONE 'UTC')",
        }[group_by]
        times = []
        for key, op in (("start", ">="), ("end", "<")):
            if filters.get(key):
                times.append(f"at{op}:{key}")
                values[key] = filters[key]
        temporal = " AND ".join(["at IS NOT NULL", *times])
        if not costs:
            temporal += " AND kind<>'cost'"
        cte = f"""
         WITH selected AS (SELECT * FROM reporting.request_facts r WHERE {where}),
         facts AS (
          SELECT r.*,r.{at} at, 'request' kind, (payload->>'charged_amount')::numeric amount,
           NULL::jsonb detail FROM selected r
          UNION ALL SELECT r.*,(f->>'{at}')::timestamptz,'refund',(f->>'amount')::numeric,NULL
           FROM selected r,jsonb_array_elements(r.payload->'refunds') f
          UNION ALL SELECT r.*,(b->>'{at}')::timestamptz,'cost',(b->>'amount')::numeric,c
           FROM selected r,jsonb_array_elements(r.payload->'costs') c,
           jsonb_array_elements(c->'bookings') b
          UNION ALL SELECT r.*,(c->>'{at}')::timestamptz,'cost',NULL::numeric,c
           FROM selected r,jsonb_array_elements(r.payload->'costs') c WHERE
                c->>'cost_amount' IS NULL
         )
        """
        rows = await self.rows(
            cte
            + f"""
         SELECT {grouping} AS bucket,count(*) FILTER(WHERE kind='request') request_count,
          count(*) FILTER(WHERE kind='request' AND payload->>'reservation_state'='held') held_count,
          coalesce(sum((payload->>'reserved_amount')::numeric) FILTER(WHERE kind='request'
            AND payload->>'reservation_state'='held'),0) held_amount,
          coalesce(sum(amount) FILTER(WHERE kind='request'),0) charged,
          coalesce(sum(amount) FILTER(WHERE kind='refund'),0) refunded,
          coalesce(sum(amount) FILTER(WHERE kind='cost'),0) confirmed_cost,
          count(*) FILTER(WHERE kind='cost' AND amount IS NULL) pending_costs,
          count(*) FILTER(WHERE kind='request' AND EXISTS (SELECT FROM
           jsonb_array_elements(payload->'costs') cost WHERE
                cost->>'cost_amount' IS NULL)) uncertain_requests
         FROM facts WHERE {temporal} GROUP BY 1 ORDER BY 1
        """,
            **values,
        )
        quantities = await self.rows(
            cte
            + f"""
         SELECT {grouping} AS bucket,line->>'dimension' dimension,
                sum((line->>'quantity')::numeric) quantity

                FROM facts,jsonb_array_elements(coalesce(nullif(
              payload->'charge_details','null'::jsonb),'[]'::jsonb)) line
         WHERE kind='request' AND {temporal} GROUP BY 1,2
        """,
            **values,
        )
        by_bucket = {}
        for row in quantities:
            by_bucket.setdefault(row["bucket"], {})[row["dimension"]] = int(row["quantity"])
        items = []
        with localcontext() as ctx:
            ctx.prec = 60
            for row in rows:
                net = row["charged"] - row["refunded"]
                item = {
                    "group": str(row["bucket"]),
                    "requests": row["request_count"],
                    "held_requests": row["held_count"],
                    "quantities": by_bucket.get(row["bucket"], {}),
                    "held_amount": format(row["held_amount"], ".12f"),
                    "charged_amount": format(row["charged"], ".12f"),
                    "refund_amount": format(row["refunded"], ".12f"),
                    "net_charge": format(net, ".12f"),
                }
                if costs:
                    item.update(
                        confirmed_cost=format(row["confirmed_cost"], ".12f"),
                        margin=format(net - row["confirmed_cost"], ".12f"),
                        provisional=bool(
                            row["pending_costs"] or row["uncertain_requests"] or row["held_count"]
                        ),
                    )
                items.append(item)
        watermark = (
            await self.rows(
                "SELECT max(source_at) latest_source_at,max(copied_at) projected_at"
                f" FROM reporting.request_facts r WHERE {where}",
                **values,
            )
        )[0]
        return {"items": items, "watermark": dict(watermark)}

    async def audit(self, actor, action, resource, org, details):
        await self.execute(
            """
                INSERT INTO reporting.audit_events(actor_id,action,
                resource_id,organization_id,details)
            VALUES(:actor,:action,:resource,:org,cast(:details AS jsonb))""",
            actor=actor,
            action=action,
            resource=resource,
            org=org,
            details=json.dumps(details),
        )

    async def policy(self, *, lock=False):
        row = (
            await self.rows(
                "SELECT per_org_minute,ttl_seconds,max_rows,version,managed_sha256,managed_at "
                "FROM reporting.export_policy" + (" FOR UPDATE" if lock else "")
            )
        )[0]
        return ReportingPolicyView(
            exports={k: row[k] for k in ("per_org_minute", "ttl_seconds", "max_rows")},
            managed=managed_source(row),
            version=row["version"],
        )

    async def set_policy(self, policy, version, *, managed=KEEP):
        """``managed`` is the SHA-256 of the locking policy file, or None to unlock."""
        lock = (
            ""
            if managed is KEEP
            else ",managed_sha256=:managed,managed_at=CASE WHEN CAST(:managed AS text) IS NULL "
            "THEN NULL ELSE clock_timestamp() END"
        )
        extra = {} if managed is KEEP else {"managed": managed}
        result = await self.execute(
            f"""UPDATE reporting.export_policy SET per_org_minute=:per_org_minute,
                ttl_seconds=:ttl_seconds,max_rows=:max_rows,version=version+1{lock}
             WHERE version=:version RETURNING version""",
            **policy.exports.model_dump(),
            version=version,
            **extra,
        )
        return result.scalar_one_or_none() is not None

    async def enqueue(self, org, actor, body):
        policy = (await self.policy()).exports
        ttl, rate = policy.ttl_seconds, policy.per_org_minute
        await self.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(CAST(:org AS text),95631))", org=str(org)
        )
        count = (
            await self.execute(
                """SELECT count(*) FROM reporting.export_jobs WHERE organization_id=:org
                     AND created_at>clock_timestamp()-interval '1 minute'""",
                org=org,
            )
        ).scalar_one()
        if count >= rate:
            raise ServiceError(
                "export_rate_limited", "Export creation limit reached", 429, retry_after=60
            )
        id = uuid4()
        await self.execute(
            """
                INSERT INTO reporting.export_jobs(id,organization_id,actor_id,
                kind,filters,expires_at)
             VALUES(:id, :org, :actor, :kind, cast(:filters AS jsonb),
             clock_timestamp()+make_interval(secs=>:ttl))""",
            id=id,
            org=org,
            actor=actor,
            kind=body.kind,
            filters=body.filters.model_dump_json(),
            ttl=ttl,
        )
        await self.audit(actor, "export.queued", id, org, {"kind": body.kind})
        return await self.job(org, id)

    async def job(self, org, id):
        rows = await self.rows(
            f"SELECT {JOB} FROM reporting.export_jobs WHERE organization_id=:org AND id=:id",
            org=org,
            id=id,
        )
        if not rows:
            raise ServiceError("not_found", "Export not found", 404)
        return dict(rows[0])

    async def jobs(self, org, actor, finance, limit, cursor):
        """Newest jobs first; `actor` limits them to one requester's, `finance` admits costs."""
        conditions, values = ["organization_id=:org"], {"org": org, "limit": limit}
        if actor is not None:
            conditions.append("actor_id=:actor")
            values["actor"] = actor
        if not finance:
            conditions.append("kind<>'attempt_costs'")
        if cursor:
            conditions.append("(created_at,id)<(:at,:rid)")
            values.update(at=cursor[0], rid=cursor[1])
        rows = await self.rows(
            f"SELECT {JOB} FROM reporting.export_jobs WHERE "
            + " AND ".join(conditions)
            + " ORDER BY created_at DESC,id DESC LIMIT :limit",
            **values,
        )
        return [dict(row) for row in rows]

    async def download(self, org, id):
        result = await self.execute(
            """SELECT content FROM reporting.export_jobs WHERE organization_id=:org AND id=:id
          AND state='ready' AND expires_at>clock_timestamp()""",
            org=org,
            id=id,
        )
        data = result.scalar_one_or_none()
        if data is None:
            raise ServiceError("export_unavailable", "Export is unavailable", 409)
        return bytes(data)

    async def claim(self):
        await self.execute("""
                UPDATE reporting.export_jobs SET state='expired',content=NULL,lease_until=NULL
           WHERE expires_at<=clock_timestamp() AND state<>'expired'""")
        jobs = await self.rows("""
                WITH due AS (SELECT id FROM reporting.export_jobs WHERE expires_at>clock_timestamp()
         AND ((state='queued' AND available_at<=clock_timestamp()) OR (state='running' AND
         lease_until<=clock_timestamp()))
         ORDER BY available_at,id LIMIT 1 FOR UPDATE SKIP LOCKED)
         UPDATE reporting.export_jobs j SET state='running',fence=fence+1,attempts=attempts+1,
         lease_until=clock_timestamp()+interval '2 minutes' FROM due WHERE j.id=due.id
         RETURNING j.id,j.organization_id,j.kind,j.filters,j.actor_id,j.fence,j.attempts""")
        return dict(jobs[0]) if jobs else None

    async def finish_export(self, job, data, count, error=None):
        final = (
            "ready"
            if data is not None
            else "failed"
            if error == "export_too_large" or job["attempts"] >= 5
            else "queued"
        )
        result = await self.execute(
            """UPDATE reporting.export_jobs SET state=:state,content=:data,row_count=:count,
         completed_at=CASE WHEN :state='ready' THEN clock_timestamp() ELSE NULL END,
         available_at=clock_timestamp()+interval '2 seconds',lease_until=NULL,error_code=:error
         WHERE id=:id AND state='running' AND fence=:fence AND expires_at>clock_timestamp()
         AND lease_until>clock_timestamp() RETURNING id""",
            state=final,
            data=data,
            count=count,
            error=error,
            id=job["id"],
            fence=job["fence"],
        )
        if result.scalar_one_or_none():
            await self.audit(
                None, "export." + final, job["id"], job["organization_id"], {"kind": job["kind"]}
            )
