from datetime import datetime
from types import TracebackType
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from nexus.infrastructure.postgres import ModuleDatabase


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "identity"}
    id: Mapped[UUID] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(512))
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
    action: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[UUID] = mapped_column()
    details: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdministratorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_bootstrap(self) -> None:
        await self._session.execute(text("SELECT pg_advisory_xact_lock(336170374483)"))

    async def first_admin(self) -> tuple[UUID, str] | None:
        row = (
            await self._session.execute(
                select(User.id, User.login)
                .join(PlatformRole, PlatformRole.user_id == User.id)
                .where(PlatformRole.role == "platform_admin")
                .limit(1)
            )
        ).first()
        return (row[0], row[1]) if row else None

    async def login_exists(self, login: str) -> bool:
        return (
            await self._session.execute(select(User.id).where(User.login == login))
        ).first() is not None

    async def create(self, user_id: UUID, login: str, password_hash: str) -> None:
        self._session.add(User(id=user_id, login=login, password_hash=password_hash))
        await self._session.flush()
        self._session.add(PlatformRole(user_id=user_id, role="platform_admin"))


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_bootstrap(self, user_id: UUID) -> None:
        self._session.add(
            AuditEvent(
                id=uuid4(),
                actor_type="system",
                action="administrator.bootstrapped",
                resource_id=user_id,
                details={"source": "bootstrap_cli"},
            )
        )


class IdentityUnitOfWork:
    def __init__(self, database: ModuleDatabase) -> None:
        self._context = database.transaction()

    async def __aenter__(self) -> IdentityUnitOfWork:
        session = await self._context.__aenter__()
        self.administrators = AdministratorRepository(session)
        self.audit = AuditRepository(session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return await self._context.__aexit__(exc_type, exc, traceback)
