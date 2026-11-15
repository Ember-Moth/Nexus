from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import AwareDatetime

from nexus.http import (
    business_errors,
    credentials,
    cursor_value,
    error_responses,
    expected_version,
)
from nexus.shared.errors import ServiceError

from ..application.schemas import (
    AttemptCostEntry,
    AuditEvent,
    CacheUsage,
    ChargeRecord,
    ExportJob,
    ExportRequest,
    LedgerRecord,
    Page,
    ReportFilter,
    ReportingPolicy,
    ReportingPolicyView,
    ReportPage,
    RequestRecord,
    UsageReport,
    VectorStorageDay,
)
from ..application.service import Reports


class CsvResponse(Response):
    media_type = "text/csv"


def create_router(get_service):
    router = APIRouter(prefix="/api/v1", tags=["reports"])
    Service = Annotated[Reports, Depends(get_service)]
    Token = Annotated[str, Depends(credentials)]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Cursor = Annotated[str | None, Query(max_length=512)]
    Version = Annotated[int, Depends(expected_version)]

    # FastAPI's flattened query models cannot be combined with separate controls.
    def filter_by(
        project_id: UUID | None = None,
        api_key_id: UUID | None = None,
        model_id: UUID | None = None,
        start: AwareDatetime | None = None,
        end: AwareDatetime | None = None,
        time_basis: Literal["occurred_at", "posted_at"] = "occurred_at",
    ):
        if start and end and start >= end:
            raise ServiceError("invalid_period", "end must follow start", 422)
        return ReportFilter(
            project_id=project_id,
            api_key_id=api_key_id,
            model_id=model_id,
            start=start,
            end=end,
            time_basis=time_basis,
        )

    Filters = Annotated[ReportFilter, Depends(filter_by)]

    def register(path, kind, Item):
        @router.get(
            "/organizations/{organization_id}/" + path,
            name="list_" + kind,
            response_model=ReportPage[Item],
        )
        async def page(
            organization_id: UUID,
            token: Token,
            service: Service,
            filters: Filters,
            limit: Limit = 50,
            cursor: Cursor = None,
        ):
            return await service.page(
                token, organization_id, kind, filters, limit, cursor_value(cursor)
            )

    for path, kind, Item in [
        ("requests", "requests", RequestRecord),
        ("vector-storage-usage", "vector_storage", VectorStorageDay),
        ("charges", "charges", ChargeRecord),
        ("ledger", "ledger", LedgerRecord),
        ("upstream-costs", "attempt_costs", AttemptCostEntry),
    ]:
        register(path, kind, Item)

    @router.get(
        "/organizations/{organization_id}/requests/{request_id}", response_model=RequestRecord
    )
    async def request(organization_id: UUID, request_id: UUID, token: Token, service: Service):
        return (
            await service.page(
                token, organization_id, "requests", ReportFilter(), 1, None, request_id
            )
        )["items"][0]

    @router.get(
        "/organizations/{organization_id}/usage",
        response_model=UsageReport,
        # Cost and margin fields appear only when the caller may read procurement cost.
        response_model_exclude_unset=True,
    )
    async def usage(
        organization_id: UUID,
        token: Token,
        service: Service,
        filters: Filters,
        group_by: Literal["organization", "project", "key", "model", "day"] = "organization",
        include_cost: bool = False,
    ):
        return await service.usage(token, organization_id, filters, group_by, include_cost)

    @router.get("/organizations/{organization_id}/cache-usage", response_model=list[CacheUsage])
    async def cache_usage(organization_id: UUID, token: Token, service: Service, filters: Filters):
        return await service.cache_usage(token, organization_id, filters)

    @router.get("/audit-events", response_model=Page[AuditEvent])
    async def audits(
        token: Token,
        service: Service,
        organization_id: UUID | None = None,
        action: Annotated[str | None, Query(max_length=100)] = None,
        trace_id: UUID | None = None,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.audit_page(
            token, organization_id, limit, cursor_value(cursor), action, trace_id
        )

    @router.post(
        "/organizations/{organization_id}/exports",
        status_code=202,
        response_model=ExportJob,
        responses=business_errors("export_rate_limited"),
    )
    async def enqueue(organization_id: UUID, body: ExportRequest, token: Token, service: Service):
        return await service.queue_export(token, organization_id, body)

    @router.get("/organizations/{organization_id}/exports", response_model=Page[ExportJob])
    async def list_exports(
        organization_id: UUID,
        token: Token,
        service: Service,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        """Export jobs the caller may read, newest first."""
        return await service.exports(token, organization_id, limit, cursor_value(cursor))

    @router.get("/organizations/{organization_id}/exports/{export_id}", response_model=ExportJob)
    async def export(organization_id: UUID, export_id: UUID, token: Token, service: Service):
        return await service.export(token, organization_id, export_id)

    @router.get(
        "/organizations/{organization_id}/exports/{export_id}/download",
        response_class=CsvResponse,
        responses={
            200: {
                "description": "UTF-8 CSV with a byte order mark",
                "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}},
            },
            **error_responses(409),
        },
    )
    async def download(organization_id: UUID, export_id: UUID, token: Token, service: Service):
        content = await service.export(token, organization_id, export_id, download=True)
        return Response(
            content,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{export_id}.csv"'},
        )

    @router.get("/settings/reporting", response_model=ReportingPolicyView)
    async def reporting_policy(service: Service, token: Token, response: Response):
        """Export limits; platform administrators only."""
        result = await service.policy(token)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.put(
        "/settings/reporting",
        response_model=ReportingPolicyView,
        responses=business_errors("version_conflict", "policy_managed_externally"),
    )
    async def update_reporting_policy(
        body: ReportingPolicy, service: Service, token: Token, version: Version, response: Response
    ):
        """Replace the export limits; new exports use them at once."""
        result = await service.update_policy(token, body, version)
        response.headers["ETag"] = f'"{result.version}"'
        return result

    return router
