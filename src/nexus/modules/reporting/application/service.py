import asyncio
import csv
import io
import json
import re

from nexus.shared.errors import ServiceError
from nexus.shared.reporting import next_cursor

from .schemas import ReportFilter

CSV_FIELDS = {
    "requests": (
        "id",
        "organization_id",
        "project_id",
        "api_key_id",
        "model_id",
        "occurred_at",
        "posted_at",
        "state",
        "reservation_state",
        "currency",
        "reserved_amount",
        "charged_amount",
        "refunded_amount",
    ),
    "charges": (
        "id",
        "request_id",
        "project_id",
        "api_key_id",
        "model_id",
        "occurred_at",
        "posted_at",
        "kind",
        "currency",
        "amount",
        "sale_price_version_id",
        "discount_version_id",
        "charge_lines",
        "gross_amount",
        "waived_amount",
    ),
    "ledger": (
        "id",
        "sequence",
        "project_id",
        "api_key_id",
        "model_id",
        "occurred_at",
        "posted_at",
        "kind",
        "source_id",
        "reverses_entry_id",
        "currency",
        "signed_amount",
        "balance_after",
    ),
    "attempt_costs": (
        "id",
        "request_id",
        "project_id",
        "api_key_id",
        "model_id",
        "kind",
        "effective_cost",
        "deployment_id",
        "cost_price_version_id",
        "occurred_at",
        "posted_at",
        "original_cost",
        "cost_amount",
        "cost_lines",
        "correction_id",
        "corrected_quantities",
    ),
}


def csv_field(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    value = str(value)
    # Refunds and debit entries must remain machine-readable signed decimal amounts.
    if re.fullmatch(r"-[0-9]+(?:\.[0-9]+)?", value):
        return value
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


class Reports:
    def __init__(self, transactions, access, source, audits, *, ttl, rate, max_rows):
        self.tx, self.access, self.source, self.audits = transactions, access, source, audits
        self.ttl, self.rate, self.max_rows = ttl, rate, max_rows

    async def scope(self, token, org, filters, operation):
        return await self.access(
            token,
            org,
            operation=operation,
            project_id=filters.project_id,
            key_id=filters.api_key_id,
        )

    async def page(self, token, org, kind, filters, limit, cursor, resource_id=None):
        operation = {
            "requests": "usage",
            "vector_storage": "usage",
            "charges": "usage",
            "ledger": "financial",
            "attempt_costs": "finance",
        }[kind]
        _, projects, _, _ = await self.scope(token, org, filters, operation)
        return await self._page(org, projects, kind, filters, limit, cursor, resource_id)

    async def _page(self, org, projects, kind, filters, limit, cursor=None, resource_id=None):
        params = filters.model_dump()
        if kind == "requests":
            return await self.source.requests(org, projects, params, limit, cursor, resource_id)
        if kind == "vector_storage":
            return await self.source.vector_storage(org, projects, params, limit, cursor)
        if kind == "ledger":
            return await self.source.ledger(org, params, limit, cursor)
        return await self.source.detail_page(kind, org, projects, params, limit, cursor)

    async def usage(self, token, org, filters, group_by, include_cost):
        _, projects, _, _ = await self.scope(
            token, org, filters, "finance" if include_cost else "usage"
        )
        async with self.tx() as repo:
            result = await repo.usage(org, projects, filters.model_dump(), group_by, include_cost)
        result["watermark"]["pending_requests"] = await self.source.scope_backlog(
            org, projects, filters.model_dump()
        )
        return {
            **result,
            "organization_id": org,
            "group_by": group_by,
            "filters": filters.model_dump(mode="json"),
            "currency": self.source.currency,
        }

    async def cache_usage(self, token, org, filters):
        _, projects, _, _ = await self.scope(token, org, filters, "finance")
        return await self.source.cache_usage(org, projects, filters.model_dump())

    async def project(self):
        batch = sorted(await self.source.fetch_changes(), key=lambda row: row["id"])
        if batch:
            async with self.tx() as repo:
                await repo.project(batch)
            await self.source.acknowledge(batch)
        return len(batch)

    async def audit_page(self, token, org, limit, cursor, action, trace_id=None):
        await self.access(token, org, operation="audit")
        pages = await asyncio.gather(
            *(source(org, limit + 1, cursor, action, trace_id) for source in self.audits)
        )
        rows = sorted((r for page in pages for r in page), key=lambda r: (r["created_at"], r["id"]))
        return {
            "items": rows[:limit],
            "next_cursor": next_cursor(rows[limit - 1]["created_at"], rows[limit - 1]["id"])
            if len(rows) > limit
            else None,
        }

    async def queue_export(self, token, org, body):
        actor, *_ = await self.scope(
            token, org, body.filters, "finance" if body.kind == "attempt_costs" else "export"
        )
        async with self.tx() as repo:
            return await repo.enqueue(org, actor, body, self.ttl, self.rate)

    async def export(self, token, org, id, download=False):
        actor, _, platform, _ = await self.access(token, org, operation="export")
        async with self.tx() as repo:
            job = await repo.job(org, id)
        if job["actor_id"] != actor and not platform:
            raise ServiceError("not_found", "Export not found", 404)
        await self.scope(
            token,
            org,
            ReportFilter.model_validate(job["filters"]),
            "finance" if job["kind"] == "attempt_costs" else "export",
        )
        if not download:
            return job
        async with self.tx() as repo:
            data = await repo.download(org, id)
            await repo.audit(actor, "export.downloaded", id, org, {"kind": job["kind"]})
            return data

    async def process_export(self):
        async with self.tx() as repo:
            job = await repo.claim()
        if job is None:
            return 0
        data = None
        count = None
        error = None
        try:
            page = await self._page(
                job["organization_id"],
                None,
                job["kind"],
                ReportFilter.model_validate(job["filters"]),
                self.max_rows,
            )
            if page["next_cursor"]:
                raise ValueError("export_too_large")
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer)
            fields = CSV_FIELDS[job["kind"]]
            writer.writerow(fields)
            for row in page["items"]:
                writer.writerow([csv_field(row.get(k)) for k in fields])
            data = buffer.getvalue().encode("utf-8-sig")
            if len(data) > 16 * 1024 * 1024:
                raise ValueError("export_too_large")
            count = len(page["items"])
        except ValueError:
            error = "export_too_large"
            data = None
        except Exception:
            error = "source_unavailable"
        async with self.tx() as repo:
            await repo.finish_export(job, data, count, error)
        return 1
