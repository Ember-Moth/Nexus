from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from .money import DIMENSIONS, Factor, Money


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


class Rate(Schema):
    unit_price: NonnegativeMoney
    unit_quantity: Literal[1, 1000, 1000000]

    @field_validator("unit_quantity", mode="before")
    @classmethod
    def exact_integer(cls, value):
        if type(value) is not int:
            raise ValueError("price unit must be an integer")
        return value


class Prices(Schema):
    currency: Currency
    rates: dict[str, Rate] = Field(min_length=2, max_length=5)
    enabled: bool = Field(default=True, strict=True)

    @field_validator("rates")
    @classmethod
    def dimensions(cls, rates):
        if not {"input_uncached_tokens", "output_tokens"} <= set(rates) <= DIMENSIONS:
            raise ValueError("unsupported or missing price dimensions")
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
    quantities: dict[str, Annotated[int, Field(ge=0, le=1_000_000_000, strict=True)]] | None = None
    evidence_reference: str = Field(min_length=1, max_length=255)
    normalization_version: str = Field(min_length=1, max_length=80)
