from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from nexus.shared.views import ByteSeconds, DecimalString, ManagedSource, Timestamp, View

from ..domain.money import DIMENSIONS, Factor, Money, valid_quantity


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    @field_validator("*", mode="after")
    @classmethod
    def utc_times(cls, value):
        if isinstance(value, datetime):
            try:
                return value.astimezone(UTC)
            except OverflowError, ValueError:
                raise ValueError("datetime is outside the supported UTC range") from None
        return value


type Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
type Nonblank = Annotated[str, Field(min_length=1, max_length=2000)]
type PositiveMoney = Annotated[Money, Field(gt=0)]
type NonnegativeMoney = Annotated[Money, Field(ge=0)]


class Page[T](Schema):
    items: list[T]
    next_cursor: str | None = None


class AccountView(Schema):
    id: UUID
    organization_id: UUID
    currency: str
    balance: Money
    held_amount: Money
    available_balance: Money
    status: str
    version: int
    as_of: datetime


class ReconciliationIssue(View):
    """One difference between projections and ledger facts; references depend on `kind`."""

    kind: str
    projection: DecimalString | None = None
    facts: DecimalString | None = None
    entry_id: UUID | None = None
    record_id: UUID | None = None
    request_id: UUID | None = None
    period_id: UUID | None = None


class ReconciliationRun(View):
    id: UUID
    organization_id: UUID
    consistent: bool
    issues: list[ReconciliationIssue]
    as_of: Timestamp


class ReceivingCreate(Schema):
    name: str = Field(min_length=1, max_length=200)
    reference: str = Field(min_length=1, max_length=70, pattern=r"^[A-Za-z0-9_.:-]+$")
    currency: Currency


class ReceivingPatch(Schema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "disabled"] | None = None

    @model_validator(mode="after")
    def changes(self):
        if not self.model_fields_set or any(
            getattr(self, k) is None for k in self.model_fields_set
        ):
            raise ValueError("supply non-null changes")
        return self


class ReceivingView(ReceivingCreate):
    id: UUID
    receipt_namespace: str
    status: str
    version: int
    created_at: datetime


class FundingCreate(Schema):
    receiving_account_id: UUID
    external_reference: str = Field(min_length=1, max_length=255)
    amount: PositiveMoney
    currency: Currency
    received_at: AwareDatetime
    evidence: Nonblank


class FundingView(FundingCreate):
    receiving_account_id: UUID | None
    source_type: str
    system_actor: str | None
    payment_order_id: UUID | None
    payment_receipt_id: UUID | None
    id: UUID
    organization_id: UUID
    status: str
    version: int
    created_at: datetime
    created_by: UUID | None
    posted_by: UUID | None
    posted_at: datetime | None


class LedgerView(Schema):
    id: UUID
    organization_id: UUID
    account_id: UUID
    sequence: int
    currency: str
    signed_amount: Money
    balance_after: Money
    kind: str
    source_type: str
    source_id: UUID
    reverses_entry_id: UUID | None
    actor_id: UUID | None
    posted_at: datetime


class AdjustmentCreate(Schema):
    source_entry_id: UUID
    amount: PositiveMoney
    currency: Currency
    reason: Nonblank
    evidence: Nonblank


class AdjustmentView(AdjustmentCreate):
    id: UUID
    organization_id: UUID
    status: str
    version: int
    created_at: datetime
    created_by: UUID
    posted_by: UUID | None
    posted_at: datetime | None


def checked_quantities(values: dict[str, int]):
    if any(not valid_quantity(k, v) for k, v in values.items()):
        raise ValueError("usage quantity exceeds the dimension limit")
    return values


def read_quantities(value):
    if isinstance(value, dict) and isinstance(
        number := value.get("vector_storage_byte_seconds"), str
    ):
        if not 1 <= len(number) <= 24 or not number.isascii() or not number.isdigit():
            raise ValueError("invalid storage byte-seconds")
        return value | {"vector_storage_byte_seconds": int(number)}
    return value


type Quantities = Annotated[
    dict[str, Annotated[int, Field(ge=0, strict=True)]],
    BeforeValidator(read_quantities, json_schema_input_type=dict[str, int | str]),
    AfterValidator(checked_quantities),
]


class Rate(Schema):
    unit_price: NonnegativeMoney
    unit_quantity: Literal[1, 20, 1000, 3600, 1000000, 92771293593600]

    @field_validator("unit_quantity", mode="before")
    @classmethod
    def exact_integer(cls, value):
        if type(value) is not int:
            raise ValueError("price unit must be an integer")
        return value


class Prices(Schema):
    currency: Currency
    rates: dict[str, Rate] = Field(min_length=2, max_length=21)
    enabled: bool = Field(default=True, strict=True)

    @field_validator("rates")
    @classmethod
    def dimensions(cls, rates):
        if not {"input_uncached_tokens", "output_tokens"} <= set(rates) <= DIMENSIONS:
            raise ValueError("unsupported or missing price dimensions")
        for name, rate in rates.items():
            if name.startswith("guardrail_") and rate.unit_quantity not in {1, 1000}:
                raise ValueError("Guardrail filters are priced per unit or thousand units")
            if (
                name.startswith("container_")
                and rate.unit_quantity not in {1, 20}
                or rate.unit_quantity == 20
                and not name.startswith("container_")
            ):
                raise ValueError("Container rates are per billed minute or twenty minutes")
            if (name == "code_execution_seconds" and rate.unit_quantity not in {1, 3600}) or (
                rate.unit_quantity == 3600 and name != "code_execution_seconds"
            ):
                raise ValueError("Execution time is priced per second or hour")
            if (name == "vector_storage_byte_seconds") != (rate.unit_quantity == 92771293593600):
                raise ValueError("Storage is priced per GiB-day")
        return rates


class SaleCreate(Prices):
    model_id: UUID


class CostCreate(Prices):
    deployment_id: UUID


class PriceView(Prices):
    id: UUID
    kind: str
    scope_id: UUID
    model_id: UUID
    state: str
    version: int
    effective_at: datetime | None
    published_at: datetime | None
    created_at: datetime
    created_by: UUID


class DiscountedRate(Rate):
    discounted_unit_price: str


class EffectivePricing(Schema):
    model_id: UUID
    currency: Currency
    sale_price_version_id: UUID
    discount_version_id: UUID
    sale_effective_at: datetime
    discount_effective_at: datetime
    discount_factor: Factor
    rates: dict[str, DiscountedRate]
    as_of: datetime


class Publish(Schema):
    effective_at: AwareDatetime | None = None


class DiscountCreate(Schema):
    factor: Factor = Field(gt=0, le=1)


class DiscountView(DiscountCreate):
    id: UUID
    organization_id: UUID | None
    state: str
    version: int
    effective_at: datetime | None
    published_at: datetime | None
    created_at: datetime
    created_by: UUID | None


class BudgetCreate(Schema):
    layer: Literal["platform", "organization"] = "organization"
    scope_type: Literal["organization", "project", "key"] = "organization"
    scope_id: UUID | None = None
    currency: Currency
    limit_amount: NonnegativeMoney
    period: Literal["day", "month"]

    @model_validator(mode="after")
    def scope(self):
        if self.scope_type != "organization" and self.scope_id is None:
            raise ValueError("scope_id is required")
        return self


class BudgetPatch(Schema):
    limit_amount: NonnegativeMoney | None = None
    status: Literal["active", "disabled"] | None = None

    @model_validator(mode="after")
    def changes(self):
        if not self.model_fields_set or any(
            getattr(self, k) is None for k in self.model_fields_set
        ):
            raise ValueError("supply non-null changes")
        return self


class BudgetView(BudgetCreate):
    id: UUID
    organization_id: UUID
    status: str
    version: int
    created_at: datetime


class BudgetPeriodView(Schema):
    id: UUID
    policy_id: UUID
    policy_version: int
    period_start: datetime
    period_end: datetime
    limit_amount: Money
    spent: Money
    reserved: Money
    over_limit: bool


class RequestView(Schema):
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
    web_search_limit: int
    file_search_limit: int
    guardrail_unit_limit: int
    execution_seconds_limit: int
    execution_mode: str
    container_origin: UUID | None
    container_handle: str | None
    container_unit: str | None
    container_minutes_limit: int
    container_tariff: str | None
    storage_byte_seconds: ByteSeconds
    reserved_amount: Money
    charged_amount: Money | None
    refunded_amount: Money
    state: str
    reservation_state: str
    review_reason: str | None
    created_at: datetime
    expires_at: datetime
    settled_at: datetime | None
    charge_details: list[dict] | None


@dataclass(frozen=True)
class AccountReady:
    event_id: UUID
    organization_id: UUID
    account_id: UUID
    schema_version: int


class Admission(Schema):
    """Internal authorization facts. Never accepted from the management HTTP API.

    The M4 gateway boundary authenticates identity and validates protocol bounds
    before calling the funds primitive in its transaction.
    """

    request_id: UUID
    organization_id: UUID
    project_id: UUID
    service_account_id: UUID
    api_key_id: UUID
    model_id: UUID
    input_limit: int = Field(ge=0, le=1_000_000_000, strict=True)
    output_limit: int = Field(ge=0, le=1_000_000_000, strict=True)
    ttl_seconds: int = Field(default=900, ge=1, le=86400, strict=True)


class Settlement(Schema):
    """Trusted final facts produced by the usage processor."""

    outcome: Literal["success", "customer_cancelled", "upstream_failed", "not_started", "unknown"]
    delivered_attempt_id: UUID | None = None
    quantities: Quantities | None = None
    evidence_reference: str = Field(min_length=1, max_length=255)
    normalization_version: str = Field(min_length=1, max_length=80)


class VectorStoragePolicy(Schema):
    prepaid_days: int = Field(
        ge=1, le=31, description="Days of storage held ahead and renewed by the worker"
    )
    lapse_policy: Literal["delete", "suspend"] = Field(
        description="What happens to a store once renewal failed and its prepaid days ran out"
    )


class PaymentLimits(Schema):
    requests_per_merchant_per_minute: int = Field(
        ge=1, le=10000, description="Worker calls to one merchant account per minute"
    )
    notifications_per_channel_per_minute: int = Field(ge=1, le=100000)
    notifications_per_ip_per_minute: int = Field(ge=1, le=10000)


class BillingPolicy(Schema):
    currency: Currency = Field(
        description="Every account, price, budget and channel uses it; it cannot change once "
        "any of them exists (currency_locked)"
    )
    vector_storage: VectorStoragePolicy
    payments: PaymentLimits


class BillingPolicyView(BillingPolicy):
    managed: ManagedSource | None = Field(
        description="The policy file that locked this policy; writes are refused while set"
    )
    version: int
