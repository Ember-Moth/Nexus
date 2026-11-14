"""Payment value objects: no ORM rows, HTTP framework objects or adapter imports."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .schemas import Currency, Money, PositiveMoney, Schema

PaymentState = Literal[
    "pending",
    "processing",
    "succeeded",
    "failed",
    "cancelled",
    "partially_reversed",
    "reversed",
    "unknown",
]
Environment = Literal["test", "live"]


class GatewayCapabilities(Schema):
    currencies: dict[str, int]
    supports_query: bool
    idempotency: Literal["native", "merchant_order_unique", "none"]
    idempotency_seconds: int = Field(ge=0)
    supports_close: bool = False
    checkout_hosts: frozenset[str]
    checkout_kinds: frozenset[Literal["redirect", "qr_code"]] = frozenset({"redirect"})
    qr_code_prefixes: tuple[str, ...] = ()


class NotificationStyle(Schema):
    methods: frozenset[str]
    mode: Literal["raw_json", "form"]
    unique_parameters: bool = True
    max_bytes: int = Field(default=65536, ge=1, le=1048576)


class CheckoutData(Schema):
    kind: Literal["redirect", "qr_code"]
    destination: str = Field(min_length=1, max_length=4096)
    expires_at: AwareDatetime


class PaymentExpectation(Schema):
    order_id: UUID
    organization_id: UUID
    account_id: UUID
    channel_id: UUID
    config_version_id: UUID
    merchant_order_ref: str
    stable_merchant_id: str
    environment: Environment
    receipt_namespace: str
    amount: Money
    currency: Currency
    callback_url: str
    return_url: str
    expires_at: AwareDatetime


class CheckoutResult(Schema):
    provider_payment_ref: str | None = Field(default=None, min_length=1, max_length=255)
    checkout: CheckoutData | None = None
    state: PaymentState = "pending"


class VerifiedNotice(Schema):
    event_id: str | None = Field(default=None, max_length=255)
    merchant_order_ref: str | None = Field(default=None, max_length=255)
    merchant_id: str = Field(min_length=1, max_length=255)
    environment: Environment
    state: PaymentState = "unknown"
    provider_payment_ref: str | None = Field(default=None, max_length=255)
    transaction_id: str | None = Field(default=None, max_length=255)
    amount: Money | None = None
    currency: Currency | None = None
    organization_id: UUID | None = None
    account_id: UUID | None = None
    order_id: UUID | None = None
    reversed_amount: Money = Decimal(0)
    disputed_amount: Money = Decimal(0)
    reversal_state: Literal["none", "pending", "succeeded", "failed"] = "none"
    dispute_state: Literal["none", "open", "won", "lost"] = "none"
    reversal_reference: str | None = Field(default=None, max_length=255)
    dispute_reference: str | None = Field(default=None, max_length=255)
    occurred_at: AwareDatetime | None = None
    relevant: bool = True

    @model_validator(mode="after")
    def amounts(self):
        if any(
            value is not None and value < 0
            for value in (self.amount, self.reversed_amount, self.disputed_amount)
        ):
            raise ValueError("negative payment amounts are invalid")
        return self


class PaymentObservation(VerifiedNotice):
    lookup_result: Literal["found", "not_found", "unconfirmed", "awaiting_customer"] = "found"
    observed_at: AwareDatetime
    received_at: AwareDatetime | None = None
    fee_amount: Money | None = None
    net_amount: Money | None = None
    checkout: CheckoutData | None = None


@dataclass(frozen=True)
class NotificationEnvelope:
    method: str
    path: str
    query: bytes
    headers: tuple[tuple[str, str], ...]
    body: bytes
    form: tuple[tuple[str, str], ...] = ()


NotificationDisposition = Literal[
    "accepted", "ignored", "rejected", "temporary_failure", "rate_limited"
]


@dataclass(frozen=True)
class NotificationResponse:
    status: int
    body: bytes
    content_type: str


class ChannelCreate(Schema):
    name: str = Field(min_length=1, max_length=200)
    adapter_type: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    stable_merchant_id: str = Field(min_length=1, max_length=100)
    environment: Environment
    currency: Currency
    min_amount: PositiveMoney
    max_amount: PositiveMoney
    configuration: dict = Field(repr=False)

    @model_validator(mode="after")
    def bounds(self):
        if self.min_amount > self.max_amount:
            raise ValueError("minimum amount exceeds maximum amount")
        return self


class ChannelPatch(Schema):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "disabled"] | None = None
    min_amount: PositiveMoney | None = None
    max_amount: PositiveMoney | None = None
    configuration: dict | None = Field(default=None, repr=False)
    verification_grace_seconds: int = Field(default=3600, ge=0, le=86400, strict=True)

    @model_validator(mode="after")
    def changes(self):
        changes = self.model_fields_set - {"verification_grace_seconds"}
        if not changes or any(getattr(self, name) is None for name in changes):
            raise ValueError("supply non-null changes")
        if "verification_grace_seconds" in self.model_fields_set and self.configuration is None:
            raise ValueError("verification grace belongs to configuration rotation")
        return self


class ChannelView(Schema):
    id: UUID
    name: str
    adapter_type: str
    stable_merchant_id: str
    environment: Environment
    currency: str
    min_amount: Money
    max_amount: Money
    status: str
    active_config_id: UUID
    version: int
    created_at: AwareDatetime


class ChannelVersionView(Schema):
    id: UUID
    channel_id: UUID
    version: int
    key_version: int
    public_configuration: dict
    capabilities: GatewayCapabilities
    verification_valid_until: AwareDatetime | None
    revoked_at: AwareDatetime | None
    created_at: AwareDatetime


class PaymentMethod(Schema):
    channel_id: UUID
    name: str
    currency: str
    min_amount: Money
    max_amount: Money
    decimal_places: int
    environment: Environment


class PaymentCreate(Schema):
    channel_id: UUID
    amount: PositiveMoney
    currency: Currency


class PaymentOrderView(Schema):
    id: UUID
    organization_id: UUID
    channel_id: UUID
    merchant_order_ref: str
    amount: Money
    collection_amount: Money
    collection_currency: Currency
    exchange_rate: Money
    credit_amount: Money
    currency: str
    checkout_state: str
    payment_state: PaymentState
    credit_state: Literal["uncredited", "credited"]
    review_required: bool
    review_reason: str | None
    funding_record_id: UUID | None
    ledger_entry_id: UUID | None
    expires_at: AwareDatetime
    created_at: AwareDatetime
    credited_at: AwareDatetime | None
    checkout: CheckoutData | None
    version: int


class PaymentCaseView(Schema):
    id: UUID
    organization_id: UUID
    order_id: UUID
    reason: str
    status: str
    amount_at_risk: Money
    evidence: dict
    created_at: AwareDatetime


@dataclass(frozen=True)
class PaymentClaim:
    id: UUID
    order_id: UUID
    fencing_token: int


@dataclass(frozen=True)
class PaymentExecution:
    claim: PaymentClaim
    expectation: PaymentExpectation
    adapter_type: str
    credential_version_id: UUID
    credential_key_version: int
    credential_ciphertext: bytes = field(repr=False)
    operation_kind: str
    idempotency_key: str
    provider_payment_ref: str | None
    order_version: int
    checkout_generation: int
    remote_started_at: datetime | None
    recovery: bool
    retry_observation_id: UUID | None = None


class PaymentObservationView(Schema):
    id: UUID
    order_id: UUID
    config_version_id: UUID
    source: str
    observed_at: AwareDatetime
    created_at: AwareDatetime
    disposition: str
    facts: dict


class PaymentReceiptView(Schema):
    id: UUID
    organization_id: UUID
    order_id: UUID
    transaction_id: str
    provider_payment_ref: str
    amount: Money
    currency: str
    observation_id: UUID
    confirmed_at: AwareDatetime
    created_at: AwareDatetime
