from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import Field, model_validator

from nexus.http import credentials, cursor_value, expected_version, idempotency_key

from .money import DIMENSIONS
from .schemas import Nonblank, NonnegativeMoney, Schema


class ReviewBasis(Schema):
    reason: Nonblank
    evidence: Nonblank


class Resolution(ReviewBasis):
    action: Literal[
        "confirm_no_charge", "supplement_usage", "retry", "waive", "verify_payment", "confirm_cost"
    ]
    attempt_id: UUID | None = None
    quantities: dict[str, Annotated[int, Field(ge=0, le=1_000_000_000, strict=True)]] | None = None
    normalization_version: str | None = Field(default=None, min_length=1, max_length=80)
    waiver: NonnegativeMoney | None = None
    observation_id: UUID | None = None
    statement_id: UUID | None = None

    @model_validator(mode="after")
    def required_evidence(self):
        usage = self.action in {"waive", "supplement_usage"}
        fields = {k for k in self.model_fields_set if k not in {"action", "reason", "evidence"}}
        expected = {"attempt_id", "quantities", "normalization_version"} if usage else set()
        if self.action == "waive":
            expected.add("waiver")
        if self.action == "confirm_cost":
            expected.add("statement_id")
        if self.action == "waive" and (self.waiver is None or self.waiver <= 0):
            raise ValueError("Waiver must be positive")
        if self.action == "verify_payment":
            expected.add("observation_id")
        if fields != expected or any(getattr(self, k) is None for k in expected):
            raise ValueError("Supply the fields required for the selected resolution")
        if usage and (not self.quantities or not self.quantities.keys() <= DIMENSIONS):
            raise ValueError("Unsupported metering dimension")
        return self


class Statement(ReviewBasis):
    organization_id: UUID
    attempt_id: UUID
    external_reference: str = Field(min_length=1, max_length=255)
    amount: NonnegativeMoney
    currency: str = Field(pattern=r"^[A-Z]{3}$")


def create_router(get_service):
    router = APIRouter(prefix="/api/v1", tags=["financial operations"])
    Service = Annotated[object, Depends(get_service)]
    Token = Annotated[str, Depends(credentials)]
    Key = Annotated[str, Depends(idempotency_key)]
    Version = Annotated[int, Depends(expected_version)]

    @router.post("/reconciliation-cases/upstream-statements", status_code=201)
    async def statement(body: Statement, token: Token, service: Service, key: Key):
        return await service.statement(token, body, key)

    @router.get("/reconciliation-cases")
    async def cases(
        token: Token,
        service: Service,
        organization_id: UUID | None = None,
        state: Literal["open", "resolved"] | None = "open",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ):
        return await service.list(token, organization_id, state, limit, cursor_value(cursor))

    @router.get("/reconciliation-cases/{case_id}")
    async def case(case_id: UUID, token: Token, service: Service):
        return await service.detail(token, case_id)

    @router.post("/reconciliation-cases/{case_id}/resolve")
    async def resolve(
        case_id: UUID, body: Resolution, token: Token, service: Service, key: Key, version: Version
    ):
        return await service.resolve(token, case_id, body, version, key)

    @router.post("/organizations/{organization_id}/account/unfreeze")
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
