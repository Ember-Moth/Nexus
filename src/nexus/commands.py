import json
from pathlib import Path

from alembic import command
from alembic.config import Config

from nexus.config import MigrationSettings, RuntimeSettings
from nexus.shared.errors import StorageUnavailable

from .bootstrap import Application


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


async def bootstrap(settings: RuntimeSettings, login: str, password: str) -> dict:
    application = Application(settings)
    try:
        await application.database.check_revision()
        result = await application.identity.bootstrap_admin(login, password)
        return {"user_id": str(result.user_id), "created": result.created}
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
