import asyncio
import csv
import io
import json
import logging
import re
from datetime import timedelta
from uuid import UUID

from nexus.shared.errors import ServiceError
from nexus.shared.policy import ImportPlan, plan
from nexus.shared.reporting import next_cursor
from nexus.shared.telemetry import event

from .schemas import ReportFilter, ReportingPolicy

logger = logging.getLogger("nexus.reporting")

GROUP = "reporting.export_policy"
# Audit resource of changes to the singleton export policy.
POLICY_RESOURCE = UUID(int=0)
# Largest export file; a larger one fails as export_too_large.
MAX_EXPORT_BYTES = 16 * 1024 * 1024
CSV_CONTENT_TYPE = "text/csv; charset=utf-8"
# Lifetime of a download URL, cut short by the export's own expiry.
DOWNLOAD_URL_SECONDS = 300
# Expired exports whose objects one pass deletes, and when a failed deletion is retried.
PURGE_BATCH = 20
PURGE_RETRY_SECONDS = 300


def export_prefix(organization_id, export_id):
    """Every object of one export: one per attempt that uploaded, named by its fence."""
    return f"exports/{organization_id}/{export_id}/"


def policy_changes(before, after):
    old, new = before.exports.model_dump(), after.exports.model_dump()
    return {f"exports.{k}": [old[k], new[k]] for k in new if old[k] != new[k]}


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
    def __init__(self, transactions, access, source, audits, administrator, files):
        self.tx, self.access, self.source, self.audits = transactions, access, source, audits
        self.administrator, self.files = administrator, files

    async def policy(self, token):
        await self.administrator(token)
        async with self.tx() as repo:
            return await repo.policy()

    async def update_policy(self, token, body, version):
        actor = await self.administrator(token)
        async with self.tx() as repo:
            current = await repo.policy(lock=True)
            if current.managed is not None:
                raise ServiceError(
                    "policy_managed_externally",
                    "The export policy is managed by a policy file",
                    409,
                )
            await self._write_policy(repo, current, body, version)
            await repo.audit(
                actor,
                "reporting_policy.updated",
                POLICY_RESOURCE,
                None,
                policy_changes(current, body),
            )
            return await repo.policy()

    async def plan_import(self, mode, values, file_sha256):
        """Plan the ``reporting.export_policy`` group of a policy file; nothing is written yet."""
        async with self.tx() as repo:
            current = await repo.policy()
        target, found, skipped = plan(
            GROUP,
            mode,
            current.model_dump(mode="json", exclude={"version", "managed"}),
            current.version,
            current.managed is not None,
            values,
            ReportingPolicy,
        )
        body = ReportingPolicy.model_validate(target)

        async def write(details):
            async with self.tx() as repo:
                locked = await repo.policy(lock=True)
                await self._write_policy(
                    repo,
                    locked,
                    body,
                    current.version,
                    managed=file_sha256 if mode == "managed" else None,
                )
                await repo.audit(
                    None, "reporting_policy.updated", POLICY_RESOURCE, None, {**details, **found}
                )

        return ImportPlan(GROUP, mode, mode == "managed", found, skipped, write if found else None)

    @staticmethod
    async def _write_policy(repo, current, body, version, **lock):
        if current.version != version or not await repo.set_policy(body, version, **lock):
            raise ServiceError("version_conflict", "Reporting policy has changed", 412)

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
            "currency": await self.source.currency(),
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
            return await repo.enqueue(org, actor, body)

    async def exports(self, token, org, limit, cursor):
        # export() admits the requester or a platform role, and cost jobs only to platform finance.
        # Job filters were validated when queued and projects and keys are never deleted.
        actor, _, platform, finance = await self.access(token, org, operation="export")
        async with self.tx() as repo:
            rows = await repo.jobs(org, None if platform else actor, finance, limit + 1, cursor)
        return {
            "items": rows[:limit],
            "next_cursor": next_cursor(rows[limit - 1]["created_at"], rows[limit - 1]["id"])
            if len(rows) > limit
            else None,
        }

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
            stored = await repo.download(org, id)
            await repo.audit(actor, "export.downloaded", id, org, {"kind": job["kind"]})
        seconds = max(1, min(DOWNLOAD_URL_SECONDS, int(stored["remaining"])))
        url = await self.files.download_url(
            stored["object_key"], filename=f"nexus-{job['kind']}-{id}.csv", seconds=seconds
        )
        return {"url": url, "expires_at": stored["now"] + timedelta(seconds=seconds)}

    async def process_export(self):
        async with self.tx() as repo:
            await repo.expire()
            job = await repo.claim()
            max_rows = (await repo.policy()).exports.max_rows
        if job is None:
            return 0
        data = count = error = stored = None
        try:
            page = await self._page(
                job["organization_id"],
                None,
                job["kind"],
                ReportFilter.model_validate(job["filters"]),
                max_rows,
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
            if len(data) > MAX_EXPORT_BYTES:
                raise ValueError("export_too_large")
            count = len(page["items"])
        except ValueError:
            error, data = "export_too_large", None
        except Exception:
            error, data = "source_unavailable", None
        if data is not None:
            # Each attempt writes a key of its own, so a stale attempt can never replace the file
            # of the attempt that committed. Uploading before the commit means a ready job always
            # names an object that exists.
            key = f"{export_prefix(job['organization_id'], job['id'])}{job['fence']}.csv"
            try:
                await self.files.put(key, data, CSV_CONTENT_TYPE)
                stored = (key, len(data))
            except Exception as failure:
                error = "storage_unavailable"
                event(
                    logger,
                    logging.WARNING,
                    "reporting.export_upload_failed",
                    job_id=str(job["id"]),
                    attempt=job["attempts"],
                    error_type=type(failure).__name__,
                )
        async with self.tx() as repo:
            await repo.finish_export(job, stored, count, error)
        return 1

    async def purge_exports(self):
        """Delete the objects of expired exports; a failed deletion is retried later.

        A failure never holds up expiry itself: the job is already expired and unavailable.
        """
        async with self.tx() as repo:
            due = await repo.purge_due(PURGE_BATCH, PURGE_RETRY_SECONDS)
        done = []
        for job in due:
            try:
                await self.files.delete_prefix(export_prefix(job["organization_id"], job["id"]))
            except Exception as failure:
                event(
                    logger,
                    logging.WARNING,
                    "reporting.export_purge_failed",
                    job_id=str(job["id"]),
                    error_type=type(failure).__name__,
                )
                # The store is probably unreachable: the rest of the batch waits for its lease
                # instead of each request timing out in turn.
                break
            done.append(job)
        if done:
            async with self.tx() as repo:
                for job in done:
                    await repo.purged(job)
        return len(done)
