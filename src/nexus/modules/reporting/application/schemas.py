from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from nexus.shared.views import DecimalString, Timestamp, View

# Plain aliases: unlike ``type`` statements they do not become named OpenAPI components.
TimeBasis = Literal["occurred_at", "posted_at"]
ExportKind = Literal["requests", "charges", "ledger", "attempt_costs"]


class ReportFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: UUID | None = None
    api_key_id: UUID | None = None
    model_id: UUID | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    time_basis: TimeBasis = "occurred_at"

    @model_validator(mode="after")
    def ordered(self):
        if self.start and self.end and self.end <= self.start:
            raise ValueError("end must follow start")
        return self


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: ExportKind
    filters: ReportFilter = ReportFilter()


class Page[T](View):
    items: list[T]
    next_cursor: str | None


class ReportPage[T](Page[T]):
    time_basis: TimeBasis


class ChargeLine(View):
    dimension: str
    quantity: int
    unit_price: DecimalString
    unit_quantity: int
    discount_factor: DecimalString
    unrounded_amount: DecimalString


class RequestRecord(View):
    id: UUID
    organization_id: UUID
    project_id: UUID
    service_account_id: UUID
    api_key_id: UUID
    model_id: UUID
    sale_price_version_id: UUID
    discount_version_id: UUID
    billing_policy_version: str
    input_limit: int
    output_limit: int
    currency: str
    reserved_amount: DecimalString
    charged_amount: DecimalString | None
    refunded_amount: DecimalString
    gross_amount: DecimalString | None
    waived_amount: DecimalString
    state: str
    reservation_state: str
    review_reason: str | None
    charge_details: list[ChargeLine] | None
    created_at: Timestamp
    expires_at: Timestamp
    settled_at: Timestamp | None
    occurred_at: Timestamp
    posted_at: Timestamp | None


class ChargeEntry(View):
    kind: Literal["charge"]
    id: UUID
    request_id: UUID
    organization_id: UUID
    project_id: UUID
    api_key_id: UUID
    model_id: UUID
    currency: str
    amount: DecimalString | None
    gross_amount: DecimalString | None
    waived_amount: DecimalString
    sale_price_version_id: UUID
    discount_version_id: UUID
    charge_lines: list[ChargeLine] | None
    occurred_at: Timestamp
    posted_at: Timestamp


class RefundEntry(View):
    kind: Literal["refund"]
    id: UUID
    ledger_entry_id: UUID
    request_id: UUID
    organization_id: UUID
    project_id: UUID
    api_key_id: UUID
    model_id: UUID
    currency: str
    amount: DecimalString
    occurred_at: Timestamp
    posted_at: Timestamp


type ChargeRecord = Annotated[ChargeEntry | RefundEntry, Field(discriminator="kind")]


class AttemptCostEntry(View):
    kind: Literal["cost", "cost_correction", "pending"]
    id: UUID
    request_id: UUID
    attempt_id: UUID
    organization_id: UUID
    project_id: UUID
    api_key_id: UUID
    model_id: UUID
    deployment_id: UUID
    cost_price_version_id: UUID
    cost_amount: DecimalString | None
    effective_cost: DecimalString | None
    original_cost: DecimalString | None
    cost_lines: list[ChargeLine] | None
    correction_id: UUID | None
    corrected_quantities: dict[str, int] | None
    occurred_at: Timestamp
    posted_at: Timestamp | None


class LedgerRecord(View):
    id: UUID
    organization_id: UUID
    account_id: UUID
    sequence: int
    currency: str
    signed_amount: DecimalString
    balance_after: DecimalString
    kind: str
    source_type: str
    source_id: UUID
    reverses_entry_id: UUID | None
    actor_id: UUID | None
    posted_at: Timestamp
    occurred_at: Timestamp
    project_id: UUID | None
    api_key_id: UUID | None
    model_id: UUID | None


class UsageBucket(View):
    group: str
    requests: int
    held_requests: int
    quantities: dict[str, int]
    held_amount: DecimalString
    charged_amount: DecimalString
    refund_amount: DecimalString
    net_charge: DecimalString
    # Present only when the report includes procurement cost.
    confirmed_cost: DecimalString | None = None
    margin: DecimalString | None = None
    provisional: bool | None = None


class UsageWatermark(View):
    latest_source_at: Timestamp | None
    projected_at: Timestamp | None
    pending_requests: int


class UsageReport(View):
    items: list[UsageBucket]
    watermark: UsageWatermark
    organization_id: UUID
    group_by: Literal["organization", "project", "key", "model", "day"]
    filters: ReportFilter
    currency: str


class AuditEvent(View):
    trace_id: UUID | None = None
    id: UUID
    actor_id: UUID | None
    organization_id: UUID | None
    action: str
    resource_id: UUID
    created_at: Timestamp
    reason: str | None
    decision_id: UUID | None
    module: Literal["identity", "catalog", "billing", "publication", "reporting"]


class ExportJob(View):
    id: UUID
    organization_id: UUID
    actor_id: UUID
    kind: ExportKind
    filters: ReportFilter
    state: Literal["queued", "running", "ready", "failed", "expired"]
    row_count: int | None
    error_code: str | None
    created_at: Timestamp
    completed_at: Timestamp | None
    expires_at: Timestamp
