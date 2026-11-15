import asyncio

from alembic import context
from alembic.runtime.migration import MigrationContext
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from nexus.config import MigrationSettings

VERSION_SCHEMA = "nexus_meta"


def upgrading(connection) -> bool:
    """True only when the requested destination is at or above every applied revision."""
    current = MigrationContext.configure(
        connection, opts={"version_table_schema": VERSION_SCHEMA}
    ).get_current_heads()
    try:
        destination = context.get_revision_argument()
    except Exception:
        return False
    if not current:
        return True
    if not isinstance(destination, str) or destination.startswith(("-", "+")):
        return False
    script = context.script
    below = set()
    pending = [destination]
    while pending:
        revision = script.get_revision(pending.pop())
        if revision is None:
            continue
        parents = revision.down_revision
        for parent in parents if isinstance(parents, tuple) else (parents,):
            if parent and parent not in below:
                below.add(parent)
                pending.append(parent)
    return all(head == destination or head in below for head in current)


def run_migrations(connection) -> None:
    # Upgrades commit each revision on its own: the locks a revision takes are released before
    # the next one starts, and a revision may leave its transaction for CREATE INDEX
    # CONCURRENTLY (see nexus.infrastructure.migrations). Downgrades stay in one transaction, so
    # a revision that refuses to drop retained data rolls back the whole request instead of
    # leaving the revisions above it already dropped.
    per_revision = upgrading(connection)
    connection.commit()
    context.configure(
        connection=connection,
        version_table_schema=VERSION_SCHEMA,
        transaction_per_migration=per_revision,
    )
    with context.begin_transaction():
        context.run_migrations()


async def migrate() -> None:
    settings = context.config.attributes.get("settings") or MigrationSettings()
    engine = create_async_engine(
        settings.migration_database_url.get_secret_value(),
        poolclass=pool.NullPool,
        hide_parameters=True,
        connect_args={"connect_timeout": 5, "options": "-c timezone=UTC"},
    )
    try:
        async with engine.connect() as connection:
            # Serialize all migration invocations, including the first metadata schema creation.
            await connection.execute(text("SELECT pg_advisory_lock(336170374484)"))
            try:
                role = (await connection.execute(text("SELECT current_user"))).scalar_one()
                if role != "nexus_migrator":
                    raise RuntimeError("incorrect migration database role")
                await connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {VERSION_SCHEMA}"))
                await connection.execute(text("REVOKE ALL ON SCHEMA nexus_meta FROM PUBLIC"))
                await connection.commit()
                # Session settings, applied after the advisory lock so that waiting for another
                # migrator is not cut short. A statement that waits for a table lock longer than
                # lock_timeout fails instead of queueing every later reader and writer behind it.
                for name, value in (
                    ("lock_timeout", settings.migration_lock_timeout_ms),
                    ("statement_timeout", settings.migration_statement_timeout_ms),
                ):
                    await connection.execute(
                        text("SELECT set_config(:name, :value, false)"),
                        {"name": name, "value": str(value)},
                    )
                await connection.commit()
                await connection.run_sync(run_migrations)
                await connection.commit()
                await connection.execute(
                    text("GRANT USAGE ON SCHEMA nexus_meta TO nexus_api, nexus_worker")
                )
                await connection.execute(
                    text("GRANT SELECT ON nexus_meta.alembic_version TO nexus_api, nexus_worker")
                )
                await connection.commit()
            finally:
                await connection.rollback()
                await connection.execute(text("SELECT pg_advisory_unlock(336170374484)"))
                await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("M1 migrations require a PostgreSQL connection")
else:
    asyncio.run(migrate())
