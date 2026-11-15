import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import monotonic
from uuid import UUID

import psycopg
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nexus.shared.errors import StorageUnavailable
from nexus.shared.storage import SCHEMA_REVISION, ModuleName
from nexus.shared.telemetry import request_id

from .metrics import ProcessMetrics


class ModuleDatabase:
    """A fixed role scope, handed only to a module's persistence construction."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], module: ModuleName, metrics=None
    ) -> None:
        self._sessions = sessions
        self._module = module
        self._metrics = metrics

    def require_module(self, expected: ModuleName) -> None:
        if self._module is not expected:
            raise ValueError("incorrect module database scope")

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        # Identifiers come exclusively from ModuleName, never from a request.
        role = f"nexus_{self._module.value}"
        async with self._sessions() as session, session.begin():
            if self._metrics:
                start = monotonic()
                await session.connection()
                self._metrics.connection(monotonic() - start)
            await session.execute(text(f'SET LOCAL ROLE "{role}"'))
            await session.execute(text("SET LOCAL search_path TO pg_catalog"))
            if trace := request_id.get():
                # SET does not acquire a snapshot. Reporting can still establish
                # REPEATABLE READ before its first query. Only a UUID enters SQL.
                await session.execute(text(f"SET LOCAL nexus.request_id = '{UUID(str(trace))}'"))
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


class Notifications:
    """A dedicated LISTEN session that wakes a waiter when its channel is notified.

    Notifications are hints: a lost session reconnects and reports a wake-up, because
    anything sent while it was away was not observed. Callers keep a polling fallback.
    """

    def __init__(self, conninfo: str, channel: str, timeout: float) -> None:
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", channel):
            raise ValueError("invalid notification channel")
        self._conninfo = conninfo
        self._channel = channel
        self._timeout = timeout
        self._connection: psycopg.AsyncConnection | None = None

    async def __call__(self, seconds: float) -> bool:
        if self._connection is None or self._connection.closed:
            try:
                self._connection = await psycopg.AsyncConnection.connect(
                    self._conninfo, autocommit=True, connect_timeout=max(1, int(self._timeout))
                )
                await self._connection.execute(f"LISTEN {self._channel}")
            except psycopg.Error:
                await self.close()
                await asyncio.sleep(min(seconds, 1.0))
            return True
        try:
            woken = False
            async for _ in self._connection.notifies(timeout=seconds, stop_after=1):
                woken = True
            if woken:
                # Coalesce a burst of commits into one wake-up.
                async for _ in self._connection.notifies(timeout=0.05):
                    pass
            return woken
        except psycopg.Error:
            await self.close()
            return True

    async def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                await connection.close()
            except psycopg.Error:
                pass


class Database:
    def __init__(
        self, url: str, *, runtime_role: str, pool_size: int, max_overflow: int, timeout: float
    ) -> None:
        self._url = url
        self._timeout = timeout
        self._runtime_role = runtime_role
        self.metrics = ProcessMetrics()
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
        return ModuleDatabase(self._sessions, module, self.metrics)

    def notifications(self, channel: str) -> Notifications:
        conninfo = make_url(self._url).set(drivername="postgresql")
        return Notifications(conninfo.render_as_string(hide_password=False), channel, self._timeout)

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

    async def operational_metrics(self):
        async with self._engine.connect() as conn:
            locks = (
                await conn.execute(
                    text("""SELECT count(*) FROM pg_locks WHERE NOT granted
                AND database=(SELECT oid FROM pg_database WHERE datname=current_database())""")
                )
            ).scalar_one()
        return {
            **self.metrics.snapshot(),
            "waiting_locks": locks,
            "pool_checked_out": self._engine.pool.checkedout(),
        }

    async def close(self) -> None:
        await self._engine.dispose()
