import asyncio

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from nexus.config import MigrationSettings


def run_migrations(connection) -> None:
    context.configure(connection=connection, version_table_schema="nexus_meta")
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
                await connection.execute(text("CREATE SCHEMA IF NOT EXISTS nexus_meta"))
                await connection.execute(text("REVOKE ALL ON SCHEMA nexus_meta FROM PUBLIC"))
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
