import asyncio
import json
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nexus.bootstrap import Application
from nexus.commands import bootstrap
from nexus.shared.errors import BootstrapRejected
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
        assert admin.execute("SELECT count(*) FROM identity.users").fetchone()[0] == 0
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


async def test_bootstrap_is_atomic_concurrent_and_does_not_reset_password(runtime_settings, admin):
    password = "test-initial-password-at-least-16"
    results = await asyncio.gather(
        *(bootstrap(runtime_settings, "FIRST-ADMIN", password) for _ in range(2))
    )
    assert sum(result["created"] for result in results) == 1
    assert len({result["user_id"] for result in results}) == 1
    stored = admin.execute("SELECT login, password_hash FROM identity.users").fetchone()
    assert stored[0] == "first-admin"
    assert PasswordHasher().verify(stored[1], password)
    again = await bootstrap(runtime_settings, "first-admin", "a-different-password-at-least-16")
    assert again["created"] is False
    assert admin.execute("SELECT password_hash FROM identity.users").fetchone()[0] == stored[1]
    assert admin.execute("SELECT role FROM identity.platform_role_bindings").fetchall() == [
        ("platform_admin",)
    ]
    assert admin.execute("SELECT count(*) FROM identity.audit_events").fetchone()[0] == 1
    with pytest.raises(BootstrapRejected):
        await bootstrap(runtime_settings, "second-admin", password)


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
