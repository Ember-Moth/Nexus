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


def test_business_policies_are_not_runtime_settings(settings_factory):
    # Policies live in the database; no environment variable can set or override them.
    settings = settings_factory()
    for name in (
        "login_per_ip",
        "mail_global",
        "session_ttl_seconds",
        "reserved_logins",
        "billing_currency",
        "export_max_rows",
        "auto_publish_quiet_seconds",
        "vector_storage_lapse_policy",
    ):
        assert not hasattr(settings, name)


def test_mail_budgets_keep_the_recipient_total_bound():
    from nexus.modules.identity.application.schemas import MailBudgets

    values = dict(
        window_seconds=3600,
        per_recipient=3,
        per_recipient_total=10,
        per_account=10,
        per_ip=20,
        global_limit=1000,
        queue_ttl_seconds=3600,
    )
    assert MailBudgets(**values).per_recipient == 3
    with pytest.raises(ValidationError):
        MailBudgets(**{**values, "per_recipient": 11})


@pytest.mark.parametrize(
    ("name", "content", "reason"),
    [
        ("policies.toml", "x = 1", "use a .yaml"),
        ("policies.yaml", "version: 1\ngroups: [1]", "groups must map"),
        ("policies.yaml", "version: 2\ngroups: {}", "version must be 1"),
        ("policies.json", "{not json", "not valid YAML or JSON"),
        ("policies.yaml", "version: 1\nextra: true", "only version and groups"),
        ("policies.yaml", "version: 1\ngroups:\n  identity.sessions: {mode: managed}", "unknown"),
        ("policies.yml", "version: 1\ngroups:\n  billing.policy: {mode: sync}", "mode must"),
        (
            "policies.yml",
            "version: 1\ngroups:\n  billing.policy: {mode: managed, value: {}}",
            "only mode and values",
        ),
    ],
)
def test_policy_files_are_checked_before_any_database_access(tmp_path, name, content, reason):
    from nexus.commands import read_policy_file
    from nexus.shared.policy import PolicyRejected

    path = tmp_path / name
    path.write_text(content)
    with pytest.raises(PolicyRejected, match=reason):
        read_policy_file(str(path))


def test_policy_files_parse_yaml_and_json_with_a_digest(tmp_path):
    import hashlib
    import json

    from nexus.commands import read_policy_file

    document = {"version": 1, "groups": {"reporting.export_policy": {"mode": "initial"}}}
    path = tmp_path / "policies.json"
    path.write_text(json.dumps(document))
    groups, digest = read_policy_file(str(path))
    assert groups == document["groups"]
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    yaml_path = tmp_path / "policies.yaml"
    yaml_path.write_text("version: 1\ngroups:\n  reporting.export_policy: {mode: initial}\n")
    assert read_policy_file(str(yaml_path))[0] == groups


def test_import_plans_validate_both_modes_and_only_fill_pristine_groups():
    from nexus.modules.reporting.application.schemas import ReportingPolicy
    from nexus.shared.policy import PolicyRejected, plan

    current = {"exports": {"per_org_minute": 10, "ttl_seconds": 86400, "max_rows": 10000}}
    change = {"exports": {"max_rows": 5}}
    target, found, skipped = plan("g", "initial", current, 1, False, change, ReportingPolicy)
    assert found == {"exports.max_rows": [10000, 5]} and not skipped
    target, found, skipped = plan("g", "initial", current, 2, False, change, ReportingPolicy)
    assert target == current and found == {} and skipped
    target, found, skipped = plan("g", "initial", current, 4, True, change, ReportingPolicy)
    assert found == {"managed": [True, False]} and skipped
    target, found, skipped = plan("g", "managed", current, 4, False, change, ReportingPolicy)
    assert found == {"exports.max_rows": [10000, 5], "managed": [False, True]}
    for mode in ("initial", "managed"):
        with pytest.raises(PolicyRejected, match="exports.max_rows"):
            plan("g", mode, current, 3, False, {"exports": {"max_rows": 0}}, ReportingPolicy)
        with pytest.raises(PolicyRejected, match="exports.typo"):
            plan("g", mode, current, 3, False, {"exports": {"typo": 1}}, ReportingPolicy)


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
