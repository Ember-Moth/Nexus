import hashlib
import json
from pathlib import Path

import yaml
from alembic import command
from alembic.config import Config

from nexus.config import MigrationSettings, ProvisionSettings, RuntimeSettings
from nexus.shared.errors import StorageUnavailable
from nexus.shared.policy import IMPORT_ACTOR, MODES, PolicyRejected

from .bootstrap import Application


def provision_database(settings: ProvisionSettings) -> None:
    import psycopg
    from sqlalchemy.engine import make_url

    from nexus.infrastructure.provisioning import provision

    url = make_url(settings.database_url.get_secret_value())
    # libpq options such as sslmode and sslrootcert travel in the query string.
    base = url.set(drivername="postgresql").render_as_string(hide_password=False)

    def connect(database: str) -> psycopg.Connection:
        target = make_url(base).set(database=database).render_as_string(hide_password=False)
        return psycopg.connect(target, autocommit=True, connect_timeout=10)

    provision(
        connect,
        settings.database,
        settings.passwords(),
        maintenance_database=url.database,
    )


def migrate(settings: MigrationSettings, *, revision: str = "head") -> None:
    package_config = Path(__file__).resolve().parent / "alembic.ini"
    config_path = (
        package_config
        if package_config.exists()
        else Path(__file__).resolve().parents[2] / "alembic.ini"
    )
    config = Config(str(config_path))
    config.attributes["settings"] = settings
    command.upgrade(config, revision)


async def check(settings: RuntimeSettings) -> dict[str, str]:
    application = Application(settings)
    try:
        return await application.readiness()
    finally:
        await application.close()


async def reset_setup(settings: RuntimeSettings) -> dict:
    """Restore the built-in administrator's defaults and lock the management API again."""
    application = Application(settings)
    try:
        await application.database.check_revision()
        return await application.identity.reset_setup()
    finally:
        await application.close()


async def worker_health(settings: RuntimeSettings) -> bool:
    application = Application(settings)
    try:
        raw = await application.cache.read_heartbeat(settings.worker_id)
        if raw is None:
            return False
        return json.loads(raw).get("ready") is True
    except Exception:
        raise StorageUnavailable("worker heartbeat unavailable") from None
    finally:
        await application.close()


# Policy groups a policy file can address, and the planner of each one.
POLICY_GROUPS = {
    "identity.auth_policy": lambda app: app.identity.plan_import,
    "identity.site_policy": lambda app: app.identity.plan_site_import,
    "billing.policy": lambda app: app.billing_settings.plan_import,
    "reporting.export_policy": lambda app: app.reports.plan_import,
    "catalog.quota_defaults": lambda app: app.routing.plan_import,
    "publication.cluster_defaults": lambda app: app.publication.plan_import,
}


def read_policy_file(path: str) -> tuple[dict, str]:
    """Parse a YAML or JSON policy file and check its outline; returns it with its SHA-256."""
    file = Path(path)
    raw = file.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    suffix = file.suffix.lower()
    if suffix not in {".json", ".yaml", ".yml"}:
        raise PolicyRejected("file", "use a .yaml, .yml or .json policy file")
    try:
        document = json.loads(raw) if suffix == ".json" else yaml.safe_load(raw)
    except ValueError, yaml.YAMLError:
        raise PolicyRejected("file", "the policy file is not valid YAML or JSON") from None
    if not isinstance(document, dict) or set(document) - {"version", "groups"}:
        raise PolicyRejected("file", "expected only version and groups at the top level")
    if document.get("version") != 1:
        raise PolicyRejected("file", "version must be 1")
    groups = document.get("groups") or {}
    if not isinstance(groups, dict):
        raise PolicyRejected("file", "groups must map group names to entries")
    for name, entry in groups.items():
        if name not in POLICY_GROUPS:
            raise PolicyRejected(str(name), "unknown policy group")
        if not isinstance(entry, dict) or set(entry) - {"mode", "values"}:
            raise PolicyRejected(name, "expected only mode and values")
        if entry.get("mode") not in MODES:
            raise PolicyRejected(name, "mode must be initial or managed")
        if not isinstance(entry.get("values", {}), dict):
            raise PolicyRejected(name, "values must be a mapping")
    return groups, digest


async def import_policies(settings: RuntimeSettings, path: str, *, dry_run: bool = False) -> dict:
    """Apply a policy file: plan every group, then write the groups that change.

    Nothing is written unless every group is valid. Each written group gets a new version and an
    audit record in its own module with ``details.actor = "config-import"`` and the file digest.
    """
    groups, digest = read_policy_file(path)
    application = Application(settings)
    try:
        await application.database.check_revision()
        plans = [
            await POLICY_GROUPS[name](application)(entry["mode"], entry.get("values") or {}, digest)
            for name, entry in groups.items()
        ]
        if not dry_run:
            for plan in plans:
                if plan.write:
                    await plan.write(
                        {"actor": IMPORT_ACTOR, "file_sha256": digest, "mode": plan.mode}
                    )
        return {
            "file_sha256": digest,
            "dry_run": dry_run,
            "groups": {plan.group: plan.summary() for plan in plans},
        }
    finally:
        await application.close()
