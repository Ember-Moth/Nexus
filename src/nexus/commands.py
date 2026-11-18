import hashlib
import json
import os
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


# The identity policy groups depend on each other (a switch needs its provider), so the identity
# module plans and writes the ones a file names together.
IDENTITY_GROUPS = (
    "identity.site_policy",
    "identity.auth_policy",
    "identity.mail_policy",
    "identity.turnstile_policy",
)
# The other policy groups a policy file can address, and the planner of each one.
MODULE_GROUPS = {
    "billing.policy": lambda app: app.billing_settings.plan_import,
    "reporting.export_policy": lambda app: app.reports.plan_import,
    "catalog.quota_defaults": lambda app: app.routing.plan_import,
    "publication.cluster_defaults": lambda app: app.publication.plan_import,
}
POLICY_GROUPS = (*IDENTITY_GROUPS, *MODULE_GROUPS)
# Longest secret a reference may name; provider keys and passwords are far shorter.
SECRET_BYTES = 4096


def resolve_secret(group: str, path: str, reference) -> str:
    """The secret a policy file names by reference, read on the host running the import.

    A reference is ``{"env": "NAME"}`` (an environment variable) or ``{"file": "/path"}`` (a
    file, such as a mounted Kubernetes Secret; one trailing newline is dropped). Secrets in clear
    are refused, so a policy file can be kept in Git. Errors name the field, never a value.
    """
    if not isinstance(reference, dict) or len(reference) != 1:
        raise PolicyRejected(
            group, f"{path} must be a reference {{env: NAME}} or {{file: PATH}}, not a secret"
        )
    ((kind, name),) = reference.items()
    if kind not in {"env", "file"} or not isinstance(name, str) or not name:
        raise PolicyRejected(group, f"{path} must be a reference {{env: NAME}} or {{file: PATH}}")
    if kind == "env":
        value = os.environ.get(name)
        if value is None:
            raise PolicyRejected(group, f"{path}: environment variable {name} is not set")
    else:
        try:
            with open(name, "rb") as handle:
                raw = handle.read(SECRET_BYTES + 1)
            if len(raw) > SECRET_BYTES:
                raise PolicyRejected(group, f"{path}: {name} is larger than a secret can be")
            value = raw.decode()
        except UnicodeDecodeError:
            raise PolicyRejected(group, f"{path}: {name} is not UTF-8 text") from None
        except OSError as error:
            raise PolicyRejected(
                group, f"{path}: cannot read {name} ({type(error).__name__})"
            ) from None
        value = value.removesuffix("\n").removesuffix("\r")
    if not value:
        raise PolicyRejected(group, f"{path}: the referenced secret is empty")
    return value


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

    Nothing is written unless every group is valid. Secret references are resolved here, on the
    host running the import. Each written group gets a new version and an
    audit record in its own module with ``details.actor = "config-import"`` and the file digest.
    """
    groups, digest = read_policy_file(path)
    application = Application(settings)
    try:
        await application.database.check_revision()
        identity = {name: entry for name, entry in groups.items() if name in IDENTITY_GROUPS}
        plans = (
            await application.identity.plan_imports(identity, digest, resolve_secret)
            if identity
            else []
        )
        plans += [
            await MODULE_GROUPS[name](application)(entry["mode"], entry.get("values") or {}, digest)
            for name, entry in groups.items()
            if name in MODULE_GROUPS
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
