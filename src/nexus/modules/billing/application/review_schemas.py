from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from ..domain.money import DIMENSIONS
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
