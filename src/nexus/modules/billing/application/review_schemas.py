from typing import Literal
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from nexus.shared.views import DecimalString, Timestamp, View

from ..domain.money import DIMENSIONS
from .schemas import Nonblank, NonnegativeMoney, Quantities, Schema

ResolutionAction = Literal[
    "confirm_no_charge", "supplement_usage", "retry", "waive", "verify_payment", "confirm_cost"
]


class ReviewBasis(Schema):
    reason: Nonblank
    evidence: Nonblank


class Resolution(ReviewBasis):
    action: ResolutionAction
    attempt_id: UUID | None = None
    quantities: Quantities | None = None
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


class StatementRecorded(View):
    statement_id: UUID


class ReviewCase(View):
    id: UUID
    organization_id: UUID
    source_type: Literal["request", "attempt", "account", "payment"]
    source_id: UUID
    request_id: UUID | None
    reason: str
    state: Literal["open", "resolved"]
    version: int
    evidence: JsonValue
    created_at: Timestamp
    resolved_at: Timestamp | None
    resolution_id: UUID | None
    amount_at_risk: DecimalString | None


class CaseDecision(View):
    id: UUID
    actor_id: UUID
    action: str
    reason: str
    evidence: str
    parameters: dict[str, JsonValue]
    result: dict[str, JsonValue]
    created_at: Timestamp


class UsageEvidence(View):
    id: UUID
    envelope: dict[str, JsonValue]
    received_at: Timestamp
    rule_version: str | None
    quantities: dict[str, JsonValue] | None
    review_reason: str | None


class ReviewCaseDetail(ReviewCase):
    decisions: list[CaseDecision]
    # Present only for cases that concern a request.
    request: dict[str, JsonValue] | None = None
    usage_evidence: list[UsageEvidence] | None = None


class CaseResolution(View):
    """The decision outcome; the fields after `action` depend on the case source."""

    decision_id: UUID
    case_id: UUID
    action: ResolutionAction
    processing_state: Literal["queued"] | None = None
    account_id: UUID | None = None
    gross_amount: DecimalString | None = None
    waived_amount: DecimalString | None = None
    request_id: UUID | None = None
    charged_amount: DecimalString | None = None
    correction_id: UUID | None = None
    cost_amount: DecimalString | None = None
    order_id: UUID | None = None
    observation_id: UUID | None = None
    adjusted_amount: DecimalString | None = None


class AccountUnfrozen(View):
    account_id: UUID
    status: Literal["active"]
