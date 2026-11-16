"""Export files in object storage, with an in-memory store and repository."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nexus.config import RuntimeSettings
from nexus.modules.reporting.application.service import Reports, export_prefix
from nexus.shared.errors import ServiceError

ORG, EXPORT, ACTOR = uuid4(), uuid4(), uuid4()


class MemoryFiles:
    def __init__(self):
        self.objects, self.signed, self.fail = {}, [], set()

    async def put(self, key, data, content_type):
        if "put" in self.fail:
            raise ConnectionError("store unavailable")
        self.objects[key] = (data, content_type)

    async def delete_prefix(self, prefix):
        if "delete" in self.fail:
            raise ConnectionError("store unavailable")
        found = [key for key in self.objects if key.startswith(prefix)]
        for key in found:
            del self.objects[key]
        return len(found)

    async def download_url(self, key, *, filename, seconds):
        self.signed.append((key, filename, seconds))
        return f"https://store.test/{key}?expires={seconds}"


class MemoryRepository:
    def __init__(self):
        self.queued = None
        self.finished = []
        self.due = []
        self.cleared = []
        self.audits = []
        self.remaining = Decimal(86400)
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    async def expire(self):
        pass

    async def claim(self):
        job, self.queued = self.queued, None
        return job

    async def policy(self):
        return SimpleNamespace(exports=SimpleNamespace(max_rows=10))

    async def finish_export(self, job, stored, count, error=None):
        self.finished.append((stored, count, error))
        return True

    async def job(self, org, id):
        return {"actor_id": ACTOR, "kind": "ledger", "filters": {}}

    async def download(self, org, id):
        key = f"{export_prefix(org, id)}3.csv"
        return {"object_key": key, "now": self.now, "remaining": self.remaining}

    async def audit(self, *args):
        self.audits.append(args)

    async def purge_due(self, limit, retry_seconds):
        return self.due

    async def purged(self, job):
        self.cleared.append(job["id"])


class Ledger:
    async def ledger(self, org, params, limit, cursor):
        return {"items": [{"id": "1", "kind": "funding_credit"}], "next_cursor": None}


@pytest.fixture
def reports():
    repository, files = MemoryRepository(), MemoryFiles()

    @asynccontextmanager
    async def transaction():
        yield repository

    async def access(token, org, **_):
        return ACTOR, None, False, False

    service = Reports(transaction, access, Ledger(), [], None, files)
    return service, repository, files


def claimed(fence=2, attempts=2):
    return {
        "id": EXPORT,
        "organization_id": ORG,
        "kind": "ledger",
        "filters": {},
        "actor_id": ACTOR,
        "fence": fence,
        "attempts": attempts,
    }


async def test_each_attempt_uploads_to_a_key_of_its_own_before_committing(reports):
    service, repository, files = reports
    repository.queued = claimed(fence=2)
    assert await service.process_export() == 1
    key = f"exports/{ORG}/{EXPORT}/2.csv"
    data, content_type = files.objects[key]
    assert data.startswith(b"\xef\xbb\xbfid,sequence,") and b"funding_credit" in data
    assert content_type == "text/csv; charset=utf-8"
    assert repository.finished == [((key, len(data)), 1, None)]


async def test_a_failed_upload_retries_the_job_without_a_file(reports):
    service, repository, files = reports
    files.fail.add("put")
    repository.queued = claimed()
    assert await service.process_export() == 1
    assert files.objects == {}
    assert repository.finished == [(None, 1, "storage_unavailable")]


async def test_download_urls_last_minutes_and_never_outlive_the_export(reports):
    service, repository, files = reports
    issued = await service.export("token", ORG, EXPORT, download=True)
    key = f"exports/{ORG}/{EXPORT}/3.csv"
    assert files.signed == [(key, f"nexus-ledger-{EXPORT}.csv", 300)]
    assert issued == {
        "url": f"https://store.test/{key}?expires=300",
        "expires_at": repository.now + timedelta(seconds=300),
    }
    assert [audit[1] for audit in repository.audits] == ["export.downloaded"]
    repository.remaining = Decimal("41.7")
    assert (await service.export("token", ORG, EXPORT, download=True))["expires_at"] == (
        repository.now + timedelta(seconds=41)
    )
    repository.remaining = Decimal("0.2")
    await service.export("token", ORG, EXPORT, download=True)
    assert files.signed[-1][2] == 1


async def test_download_of_an_unavailable_export_signs_nothing(reports):
    service, repository, files = reports

    async def unavailable(org, id):
        raise ServiceError("export_unavailable", "Export is unavailable", 409)

    repository.download = unavailable
    with pytest.raises(ServiceError) as refused:
        await service.export("token", ORG, EXPORT, download=True)
    assert refused.value.code == "export_unavailable" and files.signed == []


async def test_purge_deletes_every_attempt_and_stops_at_the_first_failure(reports):
    service, repository, files = reports
    other = uuid4()
    for key in (f"{export_prefix(ORG, EXPORT)}1.csv", f"{export_prefix(ORG, EXPORT)}2.csv"):
        files.objects[key] = (b"", "text/csv")
    kept = f"{export_prefix(ORG, other)}1.csv"
    files.objects[kept] = (b"", "text/csv")
    repository.due = [
        {"id": EXPORT, "organization_id": ORG, "purge_at": None},
        {"id": other, "organization_id": ORG, "purge_at": None},
    ]
    files.fail.add("delete")
    assert await service.purge_exports() == 0
    assert repository.cleared == [] and len(files.objects) == 3
    files.fail.clear()
    repository.due = repository.due[:1]
    assert await service.purge_exports() == 1
    assert repository.cleared == [EXPORT] and list(files.objects) == [kept]


@pytest.mark.parametrize(
    "overrides",
    [
        {"object_storage_endpoint_url": "ftp://127.0.0.1:9000"},
        {"object_storage_endpoint_url": "https://user:secret@r2.example.com"},
        {"object_storage_endpoint_url": "https://r2.example.com/nexus-app"},
        {"object_storage_endpoint_url": "https://r2.example.com?x=1"},
        {"object_storage_endpoint_url": "https://r2.example.com:99999"},
        {"object_storage_bucket": "Nexus_App"},
        {"object_storage_region": "Auto Region"},
        {"object_storage_secret_access_key": " "},
    ],
)
def test_object_storage_settings_are_validated(settings_factory, overrides):
    with pytest.raises(ValidationError):
        settings_factory(**overrides)


@pytest.mark.parametrize(
    "missing",
    [
        "object_storage_endpoint_url",
        "object_storage_bucket",
        "object_storage_region",
        "object_storage_access_key_id",
        "object_storage_secret_access_key",
    ],
)
def test_object_storage_is_required(settings_factory, monkeypatch, missing):
    # No fallback to database storage: an API or Worker without object storage does not start.
    monkeypatch.delenv("NEXUS_" + missing.upper(), raising=False)
    settings = settings_factory()
    values = {name: getattr(settings, name) for name in RuntimeSettings.model_fields}
    del values[missing]
    with pytest.raises(ValidationError, match=missing):
        RuntimeSettings(**values)


def test_object_storage_credentials_are_masked(settings_factory):
    settings = settings_factory(object_storage_endpoint_url="https://abc.r2.cloudflarestorage.com/")
    assert settings.object_storage_endpoint_url == "https://abc.r2.cloudflarestorage.com"
    assert "test-object-secret" not in repr(settings)
    assert "test-access-key" not in settings.model_dump_json()
