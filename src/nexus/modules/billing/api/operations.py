from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from nexus.http import credentials, cursor_value, expected_version, idempotency_key

from ..application.operations import Operations
from ..application.review_schemas import (
    AccountUnfrozen,
    CaseResolution,
    Resolution,
    ReviewBasis,
    ReviewCase,
    ReviewCaseDetail,
    Statement,
    StatementRecorded,
)
from ..application.schemas import Page


def create_router(get_service):
    router = APIRouter(prefix="/api/v1", tags=["financial operations"])
    Service = Annotated[Operations, Depends(get_service)]
    Token = Annotated[str, Depends(credentials)]
    Key = Annotated[str, Depends(idempotency_key)]
    Version = Annotated[int, Depends(expected_version)]

    @router.post(
        "/reconciliation-cases/upstream-statements",
        status_code=201,
        response_model=StatementRecorded,
    )
    async def statement(body: Statement, token: Token, service: Service, key: Key):
        return await service.statement(token, body, key)

    @router.get("/reconciliation-cases", response_model=Page[ReviewCase])
    async def cases(
        token: Token,
        service: Service,
        organization_id: UUID | None = None,
        state: Literal["open", "resolved"] | None = "open",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ):
        return await service.list(token, organization_id, state, limit, cursor_value(cursor))

    @router.get(
        "/reconciliation-cases/{case_id}",
        response_model=ReviewCaseDetail,
        # Request evidence is present only for cases that concern a request.
        response_model_exclude_unset=True,
    )
    async def case(case_id: UUID, token: Token, service: Service):
        return await service.detail(token, case_id)

    @router.post(
        "/reconciliation-cases/{case_id}/resolve",
        response_model=CaseResolution,
        response_model_exclude_unset=True,
    )
    async def resolve(
        case_id: UUID, body: Resolution, token: Token, service: Service, key: Key, version: Version
    ):
        return await service.resolve(token, case_id, body, version, key)

    @router.post(
        "/organizations/{organization_id}/account/unfreeze", response_model=AccountUnfrozen
    )
    async def unfreeze(
        organization_id: UUID,
        body: ReviewBasis,
        token: Token,
        service: Service,
        key: Key,
        version: Version,
    ):
        return await service.unfreeze(
            token, organization_id, body.reason, body.evidence, version, key
        )

    return router
