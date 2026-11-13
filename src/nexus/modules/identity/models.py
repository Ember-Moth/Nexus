from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "identity"}
    id: Mapped[UUID] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlatformRole(Base):
    __tablename__ = "platform_role_bindings"
    __table_args__ = {"schema": "identity"}
    user_id: Mapped[UUID] = mapped_column(ForeignKey("identity.users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), primary_key=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = {"schema": "identity"}
    id: Mapped[UUID] = mapped_column(primary_key=True)
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[UUID | None] = mapped_column()
    organization_id: Mapped[UUID | None] = mapped_column()
    action: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[UUID] = mapped_column()
    details: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    __table_args__ = {"schema": "identity"}
    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("identity.users.id"))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasswordToken(Base):
    __tablename__ = "password_setup_tokens"
    __table_args__ = {"schema": "identity"}
    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("identity.users.id"))
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = {"schema": "identity"}
    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="provisioning")
    resume_status: Mapped[str | None] = mapped_column(String(20))
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = {"schema": "identity"}
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("identity.organizations.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("identity.users.id"))
    role: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default="active")
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutboxEvent(Base):
    __tablename__ = "outbox"
    __table_args__ = {"schema": "identity"}
    id: Mapped[UUID] = mapped_column(primary_key=True)
    topic: Mapped[str] = mapped_column(String(100))
    schema_version: Mapped[int] = mapped_column()
    aggregate_id: Mapped[UUID] = mapped_column()
    aggregate_version: Mapped[int] = mapped_column()
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScopedResource:
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID] = mapped_column()
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Project(ScopedResource, Base):
    __tablename__ = "projects"
    __table_args__ = {"schema": "identity"}
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="active")


class ProjectGrant(ScopedResource, Base):
    __tablename__ = "project_grants"
    __table_args__ = {"schema": "identity"}
    project_id: Mapped[UUID] = mapped_column()
    membership_id: Mapped[UUID] = mapped_column()
    status: Mapped[str] = mapped_column(String(20), default="active")


class ServiceAccount(ScopedResource, Base):
    __tablename__ = "service_accounts"
    __table_args__ = {"schema": "identity"}
    project_id: Mapped[UUID] = mapped_column()
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="active")


class ApiKey(ScopedResource, Base):
    __tablename__ = "api_keys"
    __table_args__ = {"schema": "identity"}
    project_id: Mapped[UUID] = mapped_column()
    service_account_id: Mapped[UUID] = mapped_column()
    name: Mapped[str] = mapped_column(String(200))
    digest: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rotated_from_id: Mapped[UUID | None] = mapped_column()


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = {"schema": "identity"}
    id: Mapped[UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[UUID] = mapped_column()
    actor_id: Mapped[UUID] = mapped_column()
    operation: Mapped[str] = mapped_column(String(40))
    key_digest: Mapped[str] = mapped_column(String(64))
    request_digest: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[UUID] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AccessPolicy(ScopedResource, Base):
    __tablename__ = "access_policies"
    __table_args__ = {"schema": "identity"}
    layer: Mapped[str] = mapped_column(String(20))
    scope_type: Mapped[str] = mapped_column(String(20))
    scope_id: Mapped[UUID] = mapped_column()
    project_id: Mapped[UUID | None] = mapped_column()
    api_key_id: Mapped[UUID | None] = mapped_column()


class AccessPolicyModel(Base):
    __tablename__ = "access_policy_models"
    __table_args__ = {"schema": "identity"}
    policy_id: Mapped[UUID] = mapped_column(primary_key=True)
    model_id: Mapped[UUID] = mapped_column(primary_key=True)
