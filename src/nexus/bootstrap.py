"""Wire resources and implemented use cases; never passed to business services."""

import asyncio
import base64

from nexus.config import RuntimeSettings
from nexus.infrastructure.auth_limits import RedisAuthLimits
from nexus.infrastructure.cache import Cache
from nexus.infrastructure.postgres import Database
from nexus.modules import identity
from nexus.shared.storage import ModuleName


class Application:
    def __init__(self, settings: RuntimeSettings) -> None:
        self._timeout = settings.dependency_timeout
        self.database = Database(
            settings.database_url.get_secret_value(),
            runtime_role=f"nexus_{settings.process_role}",
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            timeout=settings.dependency_timeout,
        )
        self.cache = Cache(
            settings.redis_url.get_secret_value(),
            timeout=settings.dependency_timeout,
            namespace=settings.redis_namespace,
        )
        self._storage = {name.value: self.database.for_module(name) for name in ModuleName}
        self.identity = identity.create_service(
            self._storage[ModuleName.IDENTITY.value],
            RedisAuthLimits(
                self.cache,
                base64.urlsafe_b64decode(settings.encryption_key.get_secret_value()),
                account_limit=settings.login_per_account,
                ip_limit=settings.login_per_ip,
                window=settings.login_window_seconds,
            ),
            session_ttl=settings.session_ttl_seconds,
            setup_ttl=settings.password_setup_ttl_seconds,
        )

    async def readiness(self) -> dict[str, str]:
        checks = {
            "postgres": self.database.check_revision,
            "redis": self.cache.check,
            **{name: scope.check_access for name, scope in self._storage.items()},
        }

        async def probe(name, check) -> tuple[str, str]:
            try:
                async with asyncio.timeout(self._timeout):
                    await check()
            except Exception:
                return name, "unavailable"
            return name, "ok"

        return dict(await asyncio.gather(*(probe(name, check) for name, check in checks.items())))

    async def close(self) -> None:
        await asyncio.gather(self.database.close(), self.cache.close())
