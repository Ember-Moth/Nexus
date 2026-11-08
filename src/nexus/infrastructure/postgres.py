from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nexus.shared.errors import StorageUnavailable
from nexus.shared.storage import SCHEMA_REVISION, ModuleName


class ModuleDatabase:
    """A fixed role scope, handed only to a module's persistence construction."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], module: ModuleName) -> None:
        self._sessions = sessions
        self._module = module

    def require_module(self, expected: ModuleName) -> None:
        if self._module is not expected:
            raise ValueError("incorrect module database scope")

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        # Identifiers come exclusively from ModuleName, never from a request.
        role = f"nexus_{self._module.value}"
        async with self._sessions() as session, session.begin():
            await session.execute(text(f'SET LOCAL ROLE "{role}"'))
            await session.execute(text("SET LOCAL search_path TO pg_catalog"))
            yield session

    async def check_access(self) -> None:
        async with self.transaction() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT current_user, has_schema_privilege(current_user, :schema, 'USAGE')"
                    ),
                    {"schema": self._module.value},
                )
            ).one()
            if row[0] != f"nexus_{self._module.value}" or not row[1]:
                raise StorageUnavailable("module database role is unavailable")


class Database:
    def __init__(
        self, url: str, *, runtime_role: str, pool_size: int, max_overflow: int, timeout: float
    ) -> None:
        self._runtime_role = runtime_role
        self._engine = create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=timeout,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={
                "connect_timeout": max(1, int(timeout)),
                "options": "-c timezone=UTC -c statement_timeout=5000 -c lock_timeout=3000",
            },
        )
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    def for_module(self, module: ModuleName) -> ModuleDatabase:
        return ModuleDatabase(self._sessions, module)

    async def check_revision(self) -> None:
        async with self._engine.connect() as conn:
            role = (await conn.execute(text("SELECT current_user"))).scalar_one()
            revisions = (
                (await conn.execute(text("SELECT version_num FROM nexus_meta.alembic_version")))
                .scalars()
                .all()
            )
            if role != self._runtime_role or revisions != [SCHEMA_REVISION]:
                raise StorageUnavailable("database identity or schema revision mismatch")

    async def close(self) -> None:
        await self._engine.dispose()
