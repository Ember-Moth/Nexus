from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from nexus.http import credentials, cursor_value

from ..application.schemas import ExportRequest, ReportFilter


def create_router(get_service):
    router = APIRouter(prefix="/api/v1", tags=["reports"])
    Service = Annotated[object, Depends(get_service)]
    Token = Annotated[str, Depends(credentials)]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Cursor = Annotated[str | None, Query(max_length=512)]
    # FastAPI's flattened query models cannot be combined with separate controls.
    from pydantic import AwareDatetime

    from nexus.shared.errors import ServiceError

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

    def register(path, kind):
        @router.get("/organizations/{organization_id}/" + path, name="list_" + kind)
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

    for path, kind in [
        ("requests", "requests"),
        ("charges", "charges"),
        ("ledger", "ledger"),
        ("upstream-costs", "attempt_costs"),
    ]:
        register(path, kind)

    @router.get("/organizations/{organization_id}/requests/{request_id}")
    async def request(organization_id: UUID, request_id: UUID, token: Token, service: Service):
        return (
            await service.page(
                token, organization_id, "requests", ReportFilter(), 1, None, request_id
            )
        )["items"][0]

    @router.get("/organizations/{organization_id}/usage")
    async def usage(
        organization_id: UUID,
        token: Token,
        service: Service,
        filters: Filters,
        group_by: Literal["organization", "project", "key", "model", "day"] = "organization",
        include_cost: bool = False,
    ):
        return await service.usage(token, organization_id, filters, group_by, include_cost)

    @router.get("/audit-events")
    async def audits(
        token: Token,
        service: Service,
        organization_id: UUID | None = None,
        action: Annotated[str | None, Query(max_length=100)] = None,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.audit_page(token, organization_id, limit, cursor_value(cursor), action)

    @router.post("/organizations/{organization_id}/exports", status_code=202)
    async def enqueue(organization_id: UUID, body: ExportRequest, token: Token, service: Service):
        return await service.queue_export(token, organization_id, body)

    @router.get("/organizations/{organization_id}/exports/{export_id}")
    async def export(organization_id: UUID, export_id: UUID, token: Token, service: Service):
        return await service.export(token, organization_id, export_id)

    @router.get("/organizations/{organization_id}/exports/{export_id}/download")
    async def download(organization_id: UUID, export_id: UUID, token: Token, service: Service):
        content = await service.export(token, organization_id, export_id, download=True)
        return Response(
            content,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{export_id}.csv"'},
        )

    return router
