import pytest
from pydantic import ValidationError


def test_credentials_are_masked(settings_factory):
    settings = settings_factory()
    assert "test-password" not in repr(settings)
    assert "test-password" not in settings.model_dump_json()
    with pytest.raises(ValidationError):
        settings.port = 9000


@pytest.mark.parametrize(
    "overrides",
    [
        {"database_url": "postgresql+psycopg://nexus_migrator:private@localhost/nexus"},
        {"database_url": "postgresql+psycopg://nexus_api:private@localhost/nexus?user=postgres"},
        {"encryption_key": "not-a-key"},
        {"redis_url": "redis://localhost/0"},
        {"process_role": "worker"},
        {"environment": "production"},
        {"worker_heartbeat_ttl": 2},
    ],
)
def test_unsafe_configuration_is_rejected(settings_factory, overrides):
    with pytest.raises(ValidationError):
        settings_factory(**overrides)


def test_migration_timeouts_are_configurable():
    from nexus.config import MigrationSettings

    url = "postgresql+psycopg://nexus_migrator:secret@127.0.0.1/nexus"
    settings = MigrationSettings(migration_database_url=url)
    assert (settings.migration_lock_timeout_ms, settings.migration_statement_timeout_ms) == (
        5000,
        0,
    )
    tuned = MigrationSettings(
        migration_database_url=url,
        migration_lock_timeout_ms=200,
        migration_statement_timeout_ms=60000,
    )
    assert tuned.migration_lock_timeout_ms == 200
