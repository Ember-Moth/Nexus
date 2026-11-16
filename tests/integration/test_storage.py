import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nexus.bootstrap import Application
from nexus.shared.storage import SCHEMA_REVISION, ModuleName

pytestmark = pytest.mark.integration


async def test_readiness_and_schema_mismatch(runtime_settings, admin):
    application = Application(runtime_settings)
    try:
        assert all(value == "ok" for value in (await application.readiness()).values())
        admin.execute("UPDATE nexus_meta.alembic_version SET version_num = 'incompatible'")
        try:
            assert (await application.readiness())["postgres"] == "unavailable"
        finally:
            admin.execute(
                "UPDATE nexus_meta.alembic_version SET version_num = %s", (SCHEMA_REVISION,)
            )
    finally:
        await application.close()


async def test_transaction_rolls_back_and_scope_cannot_read_other_schema(runtime_settings, admin):
    application = Application(runtime_settings)
    scope = application.database.for_module(ModuleName.IDENTITY)
    user_id = uuid4()
    try:
        with pytest.raises(RuntimeError):
            async with scope.transaction() as session:
                await session.execute(
                    text(
                        "INSERT INTO identity.users (id, login, password_hash) "
                        "VALUES (:id, :login, :hash)"
                    ),
                    {"id": user_id, "login": "rollback-user", "hash": "test-only-hash"},
                )
                raise RuntimeError("abort")
        assert admin.execute(
            "SELECT count(*) FROM identity.users WHERE id=%s", (user_id,)
        ).fetchone() == (0,)
        with pytest.raises(DBAPIError):
            async with application.database.for_module(ModuleName.BILLING).transaction() as session:
                await session.execute(text("SELECT id FROM identity.users"))
        with pytest.raises(DBAPIError):
            async with scope.transaction() as session:
                await session.execute(text("CREATE TABLE identity.forbidden (id integer)"))
        with pytest.raises(DBAPIError):
            async with scope.transaction() as session:
                await session.execute(text("DELETE FROM identity.audit_events"))
        # A failed transaction must not poison the pool or leak its SET LOCAL role.
        assert all(value == "ok" for value in (await application.readiness()).values())
    finally:
        await application.close()


async def test_redis_heartbeat_cleanup_is_atomic_and_owned(runtime_settings):
    application = Application(runtime_settings)
    try:
        await application.cache.write_heartbeat(
            "replacement-test", json.dumps({"instance_id": "new"}), 3
        )
        await application.cache.remove_heartbeat("replacement-test", "old")
        assert await application.cache.read_heartbeat("replacement-test") is not None
        await application.cache.remove_heartbeat("replacement-test", "new")
        assert await application.cache.read_heartbeat("replacement-test") is None
    finally:
        await application.close()
