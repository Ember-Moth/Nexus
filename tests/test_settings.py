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


def test_proxy_peers_require_a_strong_shared_secret(settings_factory):
    peers = {"auth_proxy_peers": ["127.0.0.1"]}
    with pytest.raises(ValidationError, match="auth proxy secret"):
        settings_factory(**peers)
    with pytest.raises(ValidationError, match="32 characters"):
        settings_factory(**peers, auth_proxy_secret="short")
    settings = settings_factory(**peers, auth_proxy_secret="s" * 32)
    assert "s" * 32 not in repr(settings)


def test_security_defaults(settings_factory):
    settings = settings_factory()
    assert settings.platform_email_recovery is False
    assert {"admin", "root", "system"} <= set(settings.reserved_logins)
    assert settings.mail_per_recipient <= settings.mail_per_recipient_total
    with pytest.raises(ValidationError):
        settings_factory(mail_per_recipient=11, mail_per_recipient_total=10)


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
