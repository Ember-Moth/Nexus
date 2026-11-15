from sqlalchemy import text

from nexus.shared.errors import ServiceError
from nexus.shared.reporting import next_cursor, predicates, window

from ..application.schemas import RequestView


def public_request(payload):
    fields = set(RequestView.model_fields) | {
        "occurred_at",
        "posted_at",
        "currency",
        "waived_amount",
        "gross_amount",
    }
    return {k: payload[k] for k in fields}


class BillingReports:
    def __init__(self, database):
        self.database = database

    async def currency(self):
        async with self.database.transaction() as session:
            return (
                await session.execute(text("SELECT currency FROM billing.settings WHERE id=1"))
            ).scalar_one()

    async def fetch_changes(self, limit=128):
        async with self.database.transaction() as session:
            return [
                dict(row)
                for row in (
                    await session.execute(
                        text("""
                SELECT f.* FROM (SELECT request_id FROM billing.report_changes
                  WHERE generation>copied_generation ORDER BY changed_at,request_id LIMIT :limit) q
                JOIN billing.report_source f ON f.id=q.request_id
            """),
                        {"limit": limit},
                    )
                ).mappings()
            ]

    async def acknowledge(self, rows):
        async with self.database.transaction() as session:
            await session.execute(
                text("""UPDATE billing.report_changes
                SET copied_generation=greatest(copied_generation, :generation) WHERE
                request_id=:id"""),
                [{"id": r["id"], "generation": r["generation"]} for r in rows],
            )

    async def scope_backlog(self, org, projects, filters):
        conditions, values = predicates(org, projects, filters)
        async with self.database.transaction() as session:
            return (
                await session.execute(
                    text(
                        """SELECT count(*) FROM billing.report_changes c
                JOIN billing.request_runs r ON r.id=c.request_id WHERE
                generation>copied_generation AND """
                        + " AND ".join(conditions)
                    ),
                    values,
                )
            ).scalar_one()

    async def requests(self, org, projects, filters, limit, cursor, request_id=None):
        conditions, values = predicates(org, projects, filters)
        field = "r.posted_at" if filters["time_basis"] == "posted_at" else "r.occurred_at"
        window(conditions, values, field, filters, cursor)
        if request_id:
            conditions.append("r.id=:rid")
            values["rid"] = request_id
        async with self.database.transaction() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"""SELECT r.id,r.payload,{field} sort_time
                FROM billing.report_source r WHERE """
                            + " AND ".join(conditions)
                            + f" ORDER BY {field},r.id LIMIT :limit"
                        ),
                        {**values, "limit": limit + 1},
                    )
                )
                .mappings()
                .all()
            )
        if request_id and not rows:
            raise ServiceError("not_found", "Request not found", 404)
        items = [public_request(r["payload"]) for r in rows[:limit]]
        return {
            "items": items,
            "next_cursor": next_cursor(rows[limit - 1]["sort_time"], rows[limit - 1]["id"])
            if len(rows) > limit
            else None,
            "time_basis": filters["time_basis"],
        }

    async def ledger(self, org, filters, limit, cursor):
        # Attribute adjustments through the original signed entry. Funding has organization scope.
        conditions = ["l.organization_id=:org"]
        values = {"org": org, "limit": limit + 1}
        for key in ("project_id", "api_key_id", "model_id"):
            if filters.get(key):
                conditions.append(f"r.{key}=:{key}")
                values[key] = filters[key]
        occurred = (
            "coalesce(adj.created_at,f.received_at,"
            "(e.envelope->>'occurred_at')::timestamptz,r.created_at,l.posted_at)"
        )
        field = "l.posted_at" if filters["time_basis"] == "posted_at" else occurred
        window(conditions, values, field, filters, cursor, "l.id")
        async with self.database.transaction() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"""
             SELECT l.id,l.organization_id,l.account_id,l.sequence,l.currency,l.signed_amount::text,
              l.balance_after::text,l.kind,l.source_type,l.source_id,l.reverses_entry_id,l.actor_id,l.posted_at,
              {occurred} occurred_at,{field} sort_time,r.project_id,r.api_key_id,r.model_id
             FROM billing.ledger_entries l LEFT JOIN billing.ledger_entries
                orig ON orig.id=l.reverses_entry_id
             LEFT JOIN billing.adjustments adj ON l.source_type='adjustment' AND adj.id=l.source_id
             LEFT JOIN billing.funding_records f ON l.source_type='funding' AND f.id=l.source_id
             LEFT JOIN billing.request_runs r ON (l.source_type='request' AND r.id=l.source_id)
               OR (orig.source_type='request' AND r.id=orig.source_id)
             LEFT JOIN billing.usage_events e ON e.request_id=r.id AND e.kind='request.closed'
             WHERE """
                            + " AND ".join(conditions)
                            + f" ORDER BY {field},l.id LIMIT :limit"
                        ),
                        values,
                    )
                )
                .mappings()
                .all()
            )
        return {
            "items": [{k: v for k, v in row.items() if k != "sort_time"} for row in rows[:limit]],
            "next_cursor": next_cursor(rows[limit - 1]["sort_time"], rows[limit - 1]["id"])
            if len(rows) > limit
            else None,
            "time_basis": filters["time_basis"],
        }

    async def detail_page(self, kind, org, projects, filters, limit, cursor):
        conditions, values = predicates(org, projects, filters)
        at = filters["time_basis"]
        if kind == "attempt_costs":
            src = """SELECT r.*,coalesce(b->>'id',c->>'attempt_id')::uuid entry_id,
              (c-'bookings') || jsonb_build_object('kind',coalesce(b->>'kind','pending'),
                'effective_cost',c->>'cost_amount','cost_amount',b->>'amount') item,
              coalesce(b->>'occurred_at',c->>'occurred_at')::timestamptz event_time,
              (b->>'posted_at')::timestamptz book_time FROM selected r,
              jsonb_array_elements(r.payload->'costs') c LEFT JOIN LATERAL
              jsonb_array_elements(c->'bookings') b ON true"""
        else:
            src = """
                SELECT r.*,r.id entry_id,jsonb_build_object('kind','charge','amount',
              payload->>'charged_amount',
               'sale_price_version_id', payload->'sale_price_version_id', 'discount_version_id',
               payload->'discount_version_id',
               'charge_lines', payload->'charge_details', 'waived_amount',
               payload->'waived_amount', 'gross_amount', payload->'gross_amount', 'currency',
               payload->'currency') AS item,
               occurred_at event_time, posted_at book_time FROM selected r WHERE r.posted_at IS
               NOT NULL
               UNION ALL SELECT r.*, (f->>'id')::uuid entry_id, f || jsonb_build_object('kind',
               'refund',
               'currency',payload->'currency','amount',(-((f->>'amount')::numeric))::text),
               (f->>'occurred_at')::timestamptz,(f->>'posted_at')::timestamptz FROM selected r,
               jsonb_array_elements(r.payload->'refunds') f"""
        field = "book_time" if at == "posted_at" else "event_time"
        limits = []
        window(limits, values, field, filters, cursor, "entry_id")
        async with self.database.transaction() as session:
            rows = (
                (
                    await session.execute(
                        text(f"""
                WITH selected AS (SELECT * FROM billing.report_source r WHERE
                {" AND ".join(conditions)}),
                entries AS ({src}) SELECT entry_id,id,organization_id,
                project_id,api_key_id,model_id,item,
                  event_time AS occurred_at,book_time AS posted_at,{field} AS sort_time
                FROM entries WHERE {" AND ".join(limits)} ORDER BY {field},entry_id LIMIT :limit
            """),
                        {**values, "limit": limit + 1},
                    )
                )
                .mappings()
                .all()
            )
        return {
            "items": [
                {
                    **r["item"],
                    "id": r["entry_id"],
                    "request_id": r["id"],
                    **{
                        k: r[k]
                        for k in (
                            "organization_id",
                            "project_id",
                            "api_key_id",
                            "model_id",
                            "occurred_at",
                            "posted_at",
                        )
                    },
                }
                for r in rows[:limit]
            ],
            "next_cursor": next_cursor(rows[limit - 1]["sort_time"], rows[limit - 1]["entry_id"])
            if len(rows) > limit
            else None,
            "time_basis": at,
        }

    async def vector_storage(self, org, projects, filters, limit, cursor):
        conditions, values = predicates(org, projects, filters, alias="c")
        at = (
            "r.settled_at"
            if filters["time_basis"] == "posted_at"
            else "(d.day::timestamp AT TIME ZONE 'UTC')"
        )
        window(conditions, values, at, filters, cursor, "d.id")
        async with self.database.transaction() as session:
            rows = (
                (
                    await session.execute(
                        text(f"""
                SELECT d.id, 'vs_nx_'||d.store_id::text AS vector_store_id,c.project_id,
                  c.api_key_id,c.model_id,d.day,d.byte_seconds::text AS observed_byte_seconds,
                  d.uncertain,d.covered_until,r.id AS request_id,r.settled_at AS posted_at,
                  r.charged_amount::text AS charged_amount,p.currency,
                  c.sale_id AS sale_price_version_id,
                  c.discount_id AS discount_version_id,coalesce(r.review_reason,d.reason) AS reason,
                  CASE WHEN r.reservation_state IN ('captured','released') THEN 'settled'
                    WHEN d.state='review' OR r.state='review' THEN 'review'
                    WHEN d.state='open' THEN 'open' ELSE 'pending' END AS state,
                  {at} AS sort_time
                FROM billing.vector_storage_days d
                JOIN billing.vector_storage_contracts c ON c.id=d.store_id
                JOIN billing.price_versions p ON p.id=c.sale_id
                LEFT JOIN billing.request_runs r ON r.id=d.id
                WHERE {" AND ".join(conditions)} ORDER BY {at},d.id LIMIT :limit
            """),
                        {**values, "limit": limit + 1},
                    )
                )
                .mappings()
                .all()
            )
        return {
            "items": [{k: v for k, v in row.items() if k != "sort_time"} for row in rows[:limit]],
            "next_cursor": next_cursor(rows[limit - 1]["sort_time"], rows[limit - 1]["id"])
            if len(rows) > limit
            else None,
            "time_basis": filters["time_basis"],
        }

    async def cache_usage(self, org, projects, filters):
        conditions, values = predicates(org, projects, filters)
        time = "r.settled_at" if filters["time_basis"] == "posted_at" else "a.created_at"
        window(conditions, values, time, filters)
        # One normalized fact per completed attempt; count unmappable, pending and
        # absent receipts separately from measured misses. Corrections are not cache evidence.
        sql = (
            """
            WITH attempts AS (
                SELECT a.deployment_id,r.model_id,n.quantities q
                FROM billing.request_runs r JOIN billing.request_attempts a ON a.request_id=r.id
                LEFT JOIN billing.normalized_usage n ON n.attempt_id=a.id
                AND n.review_reason IS NULL
                WHERE a.expected_usage_format IN
                ('openai.responses.v1','openai.chat.v1',
                 'anthropic.messages.v1','aws.bedrock.converse.v1') AND
            """
            + " AND ".join(conditions)
            + """
            ), counts AS (
                SELECT *, coalesce((q->>'input_cache_read_tokens')::bigint,0) AS reads,
                  coalesce((q->>'input_uncached_tokens')::bigint,0) AS uncached,
                  coalesce((q->>'input_cache_write_5m_tokens')::bigint,0)
                  + coalesce((q->>'input_cache_write_1h_tokens')::bigint,0)
                  + coalesce((q->>'input_cache_write_30m_tokens')::bigint,0)
                  + coalesce((q->>'input_cache_write_12h_tokens')::bigint,0) AS writes
                FROM attempts
            )
            SELECT deployment_id,model_id,count(*) AS attempts,count(q) AS metered_attempts,
                   count(*) FILTER (WHERE reads>0) AS cache_hit_attempts,
                   sum(uncached+reads+writes) AS input_tokens,sum(reads) AS cache_read_tokens,
                   sum(writes) AS cache_write_tokens
            FROM counts GROUP BY deployment_id,model_id ORDER BY model_id,deployment_id
        """
        )
        async with self.database.transaction() as session:
            return (await session.execute(text(sql), values)).mappings().all()

    async def metrics(self):
        async with self.database.transaction() as session:
            return dict(
                (
                    await session.execute(
                        text("""
             SELECT (SELECT count(*) FROM billing.usage_jobs WHERE state='ready') usage_backlog,
              (SELECT count(*) FROM billing.usage_jobs WHERE state='waiting') waiting_for_facts,
              (SELECT count(*) FROM billing.usage_jobs WHERE state='review') usage_review,
              (SELECT count(*) FROM billing.request_runs WHERE reservation_state='held' AND
              expires_at<=clock_timestamp()) overdue_holds,
              (SELECT count(*) FROM billing.audit_events WHERE action='usage.conflict')
              conflicting_events,
              (SELECT coalesce(sum(total),0) FROM billing.ingest_counters) AS duplicate_events,
              (SELECT avg(query_elapsed_ms) FROM billing.payment_observations
               WHERE created_at>=clock_timestamp()-interval '1 day') payment_query_ms,
              (SELECT avg(extract(epoch FROM
              created_at-(verified_fields->>'occurred_at')::timestamptz))
               FROM billing.payment_events WHERE created_at>=clock_timestamp()-interval '1 day')
               notice_receive_seconds,
              (SELECT count(*) FROM billing.review_cases WHERE state='open') consumption_cases,
              (SELECT count(*) FROM billing.payment_cases WHERE status='open') payment_cases,
              (SELECT coalesce(sum(differences), 0) FROM billing.account_checks)
              reconciliation_differences,
              (SELECT avg(extract(epoch FROM received_at-(envelope->>'occurred_at')::timestamptz))
               FROM billing.usage_events WHERE received_at>=clock_timestamp()-interval '1 day')
               event_receive_seconds,
              (SELECT avg(greatest(0,extract(epoch FROM settled_at-last_event)))
               FROM billing.request_runs r JOIN LATERAL (SELECT max(received_at) last_event FROM
               billing.usage_events e
                WHERE e.request_id=r.id AND e.received_at<=r.settled_at) x ON x.last_event IS
                NOT NULL
               WHERE r.settled_at>=clock_timestamp()-interval '1 day') settlement_seconds,
              (SELECT avg(extract(epoch FROM o.credited_at-p.confirmed_at))
               FROM billing.payment_orders o JOIN billing.funding_records f ON
               f.id=o.funding_record_id
               JOIN billing.payment_receipts p ON p.id=f.payment_receipt_id
               WHERE o.credited_at>=clock_timestamp()-interval '1 day')
               payment_confirmation_to_credit_seconds,
              (SELECT avg(extract(epoch FROM o.credited_at-v.created_at))
               FROM billing.payment_orders o JOIN billing.funding_records f ON
               f.id=o.funding_record_id
               JOIN billing.payment_receipts p ON p.id=f.payment_receipt_id
               JOIN billing.payment_observations v ON v.id=p.observation_id
               WHERE o.credited_at>=clock_timestamp()-interval '1 day') payment_posting_seconds
            """)
                    )
                )
                .mappings()
                .one()
            )
