from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[UUID] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Versioned:
    version: Mapped[int] = mapped_column(default=1)


class Settings(Base):
    __tablename__ = "settings"
    __table_args__ = {"schema": "billing"}
    id: Mapped[int] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(String(3))


class Account(Record, Versioned, Base):
    __tablename__ = "accounts"
    __table_args__ = {"schema": "billing"}
    organization_id: Mapped[UUID]
    currency: Mapped[str] = mapped_column(String(3))
    balance: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal(0))
    held_amount: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal(0))
    entry_sequence: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(20), default="active")


class Inbox(Base):
    __tablename__ = "inbox"
    __table_args__ = {"schema": "billing"}
    consumer: Mapped[str] = mapped_column(String(80), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    result_id: Mapped[UUID]


class Outbox(Record, Base):
    __tablename__ = "outbox"
    __table_args__ = {"schema": "billing"}
    topic: Mapped[str] = mapped_column(String(80))
    schema_version: Mapped[int]
    organization_id: Mapped[UUID]
    aggregate_id: Mapped[UUID]
    payload: Mapped[dict] = mapped_column(JSONB)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReceivingAccount(Record, Versioned, Base):
    __tablename__ = "receiving_accounts"
    __table_args__ = {"schema": "billing"}
    name: Mapped[str] = mapped_column(String(200))
    reference: Mapped[str] = mapped_column(String(100))
    receipt_namespace: Mapped[str] = mapped_column(String(80))
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(20), default="active")


class Funding(Record, Versioned, Base):
    __tablename__ = "funding_records"
    __table_args__ = {"schema": "billing"}
    organization_id: Mapped[UUID]
    account_id: Mapped[UUID]
    receiving_account_id: Mapped[UUID]
    receipt_namespace: Mapped[str] = mapped_column(String(80))
    external_reference: Mapped[str] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    currency: Mapped[str] = mapped_column(String(3))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[str] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_by: Mapped[UUID]
    posted_by: Mapped[UUID | None]
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = {"schema": "billing"}
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    account_id: Mapped[UUID]
    sequence: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    signed_amount: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    balance_after: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    kind: Mapped[str] = mapped_column(String(40))
    source_type: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[UUID]
    reverses_entry_id: Mapped[UUID | None]
    actor_id: Mapped[UUID | None]
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class Adjustment(Record, Versioned, Base):
    __tablename__ = "adjustments"
    __table_args__ = {"schema": "billing"}
    organization_id: Mapped[UUID]
    account_id: Mapped[UUID]
    source_entry_id: Mapped[UUID]
    amount: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    currency: Mapped[str] = mapped_column(String(3))
    reason: Mapped[str] = mapped_column(String(2000))
    evidence: Mapped[str] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_by: Mapped[UUID]
    posted_by: Mapped[UUID | None]
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Idempotency(Record, Base):
    __tablename__ = "idempotency_records"
    __table_args__ = {"schema": "billing"}
    actor_id: Mapped[UUID]
    scope: Mapped[str] = mapped_column(String(160))
    key_digest: Mapped[str] = mapped_column(String(64))
    request_digest: Mapped[str] = mapped_column(String(64))
    resource_kind: Mapped[str] = mapped_column(String(40))
    resource_id: Mapped[UUID]


class Audit(Record, Base):
    __tablename__ = "audit_events"
    __table_args__ = {"schema": "billing"}
    organization_id: Mapped[UUID | None]
    actor_id: Mapped[UUID | None]
    action: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[UUID]
    details: Mapped[dict] = mapped_column(JSONB)


class PriceVersion(Record, Versioned, Base):
    __tablename__ = "price_versions"
    __table_args__ = {"schema": "billing"}
    kind: Mapped[str] = mapped_column(String(10))
    scope_id: Mapped[UUID]
    model_id: Mapped[UUID]
    currency: Mapped[str] = mapped_column(String(3))
    rates: Mapped[dict] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(20), default="draft")
    created_by: Mapped[UUID]


class DiscountVersion(Record, Versioned, Base):
    __tablename__ = "discount_versions"
    __table_args__ = {"schema": "billing"}
    organization_id: Mapped[UUID | None]
    factor: Mapped[Decimal] = mapped_column(Numeric(7, 6))
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(20), default="draft")
    created_by: Mapped[UUID | None]


class BudgetPolicy(Record, Versioned, Base):
    __tablename__ = "budget_policies"
    __table_args__ = {"schema": "billing"}
    organization_id: Mapped[UUID]
    layer: Mapped[str] = mapped_column(String(20))
    scope_type: Mapped[str] = mapped_column(String(20))
    scope_id: Mapped[UUID]
    project_id: Mapped[UUID | None]
    api_key_id: Mapped[UUID | None]
    currency: Mapped[str] = mapped_column(String(3))
    limit_amount: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    period: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="active")


class BudgetPeriod(Record, Base):
    __tablename__ = "budget_periods"
    __table_args__ = {"schema": "billing"}
    organization_id: Mapped[UUID]
    policy_id: Mapped[UUID]
    policy_version: Mapped[int]
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    limit_amount: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    spent: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal(0))
    reserved: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal(0))


class RequestRun(Record, Base):
    __tablename__ = "request_runs"
    __table_args__ = {"schema": "billing"}
    organization_id: Mapped[UUID]
    account_id: Mapped[UUID]
    project_id: Mapped[UUID]
    service_account_id: Mapped[UUID]
    api_key_id: Mapped[UUID]
    model_id: Mapped[UUID]
    request_digest: Mapped[str] = mapped_column(String(64))
    sale_price_version_id: Mapped[UUID]
    discount_version_id: Mapped[UUID]
    billing_policy_version: Mapped[str] = mapped_column(String(40))
    input_limit: Mapped[int] = mapped_column(BigInteger)
    output_limit: Mapped[int] = mapped_column(BigInteger)
    reserved_amount: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    charged_amount: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    refunded_amount: Mapped[Decimal] = mapped_column(Numeric(28, 12), default=Decimal(0))
    state: Mapped[str] = mapped_column(String(30), default="reserved")
    reservation_state: Mapped[str] = mapped_column(String(20), default="held")
    review_reason: Mapped[str | None] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settlement_digest: Mapped[str | None] = mapped_column(String(64))
    charge_details: Mapped[list | None] = mapped_column(JSONB)


class BudgetHold(Base):
    __tablename__ = "budget_holds"
    __table_args__ = {"schema": "billing"}
    request_id: Mapped[UUID] = mapped_column(primary_key=True)
    period_id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID]
    reserved: Mapped[Decimal] = mapped_column(Numeric(28, 12))
    spent: Mapped[Decimal] = mapped_column(Numeric(28, 12))


class Attempt(Record, Base):
    __tablename__ = "request_attempts"
    __table_args__ = {"schema": "billing"}
    request_id: Mapped[UUID]
    deployment_id: Mapped[UUID]
    cost_price_version_id: Mapped[UUID]
    state: Mapped[str] = mapped_column(String(20), default="started")
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(28, 12))
    cost_details: Mapped[list | None] = mapped_column(JSONB)
    usage_digest: Mapped[str | None] = mapped_column(String(64))
