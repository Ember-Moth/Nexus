from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, LargeBinary, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Resource:
    id: Mapped[UUID] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CatalogModel(Resource, Base):
    __tablename__ = "models"
    __table_args__ = {"schema": "catalog"}
    public_name: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str] = mapped_column(String(200))
    protocols: Mapped[list[str]] = mapped_column(JSONB)
    capabilities: Mapped[dict] = mapped_column(JSONB)
    usage_schema: Mapped[str] = mapped_column(String(32))
    token_dimensions: Mapped[list[str]] = mapped_column(JSONB)
    max_input_tokens: Mapped[int] = mapped_column()
    max_output_tokens: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(String(20), default="active")


class Upstream(Resource, Base):
    __tablename__ = "upstreams"
    __table_args__ = {"schema": "catalog"}
    name: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str] = mapped_column(String(2048))
    protocol_family: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default="active")


class Credential(Resource, Base):
    __tablename__ = "upstream_credentials"
    __table_args__ = {"schema": "catalog"}
    upstream_id: Mapped[UUID] = mapped_column()
    name: Mapped[str] = mapped_column(String(200))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    key_version: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(String(20), default="active")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Deployment(Resource, Base):
    __tablename__ = "deployments"
    __table_args__ = {"schema": "catalog"}
    name: Mapped[str] = mapped_column(String(200))
    model_id: Mapped[UUID] = mapped_column()
    upstream_id: Mapped[UUID] = mapped_column()
    credential_id: Mapped[UUID] = mapped_column()
    upstream_model: Mapped[str] = mapped_column(String(200))
    region: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="active")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = {"schema": "catalog"}
    id: Mapped[UUID] = mapped_column(primary_key=True)
    actor_id: Mapped[UUID] = mapped_column()
    action: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[UUID] = mapped_column()
    details: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
