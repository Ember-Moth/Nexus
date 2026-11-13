from unittest.mock import AsyncMock

from nexus import bootstrap
from nexus.modules.identity import IdentityService
from nexus.shared.storage import ModuleName


class Scope:
    def __init__(self, module, seen):
        self.module = module
        self.seen = seen

    def require_module(self, expected):
        assert self.module is expected

    async def check_access(self):
        self.seen.append(self.module)


class FakeDatabase:
    def __init__(self, *args, **kwargs):
        self.seen = []
        self.closed = False

    def for_module(self, module):
        return Scope(module, self.seen)

    async def check_revision(self):
        pass

    async def close(self):
        self.closed = True


class FakeCache:
    def __init__(self, *args, **kwargs):
        self.closed = False

    async def check(self):
        pass

    async def close(self):
        self.closed = True


async def test_application_wires_identity_and_all_storage_scopes(monkeypatch, settings_factory):
    monkeypatch.setattr(bootstrap, "Database", FakeDatabase)
    monkeypatch.setattr(bootstrap, "Cache", FakeCache)
    application = bootstrap.Application(settings_factory())
    assert isinstance(application.identity, IdentityService)
    monkeypatch.setattr(application.billing, "check", AsyncMock())
    checks = await application.readiness()
    assert checks == dict.fromkeys(["postgres", "redis", "billing_currency", *ModuleName], "ok")
    assert set(application.database.seen) == set(ModuleName)
    await application.close()
    assert application.database.closed and application.cache.closed
