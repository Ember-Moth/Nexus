"""Export files in object storage: upload, presigned download, takeover and deletion at expiry."""

from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from botocore.exceptions import EndpointConnectionError
from exports import BUCKET, download, keys, object_store
from test_api_contract import exact
from test_billing import setup_wallet
from test_identity_api import auth

from nexus.bootstrap import Application
from nexus.config import RuntimeSettings
from nexus.modules.reporting.application.schemas import ExportDownload

pytestmark = pytest.mark.integration


class Crash(BaseException):
    """The Worker process dies: nothing after this point runs, not even exception handlers."""


async def queued_export(http, kind="ledger"):
    client, root, _, _ = http
    org, _, _ = await setup_wallet(http)
    queued = await client.post(
        f"/api/v1/organizations/{org['id']}/exports", headers=auth(root), json={"kind": kind}
    )
    assert queued.status_code == 202, queued.text
    job = queued.json()
    return org, job, f"/api/v1/organizations/{org['id']}/exports/{job['id']}"


def stored(admin, export_id):
    return admin.execute(
        "SELECT state,fence,object_key,size_bytes,purge_at IS NOT NULL "
        "FROM reporting.export_jobs WHERE id=%s",
        (export_id,),
    ).fetchone()


@pytest.fixture
def store(service_directory):
    return object_store(service_directory)


@pytest.fixture
async def second_worker(service_directory):
    """Another Worker process: its own settings, database role, pools and object store client."""
    settings = RuntimeSettings(
        _env_file=service_directory / ".env.worker",
        environment="test",
        process_role="worker",
        redis_namespace="nexus:test:" + uuid4().hex,
    )
    application = Application(settings)
    try:
        yield application
    finally:
        await application.close()


async def test_export_is_uploaded_and_downloaded_through_a_presigned_url(http, admin, store):
    client, root, app, _ = http
    org, job, path = await queued_export(http)
    prefix = f"exports/{org['id']}/{job['id']}/"
    assert (await client.get(path + "/download", headers=auth(root))).status_code == 409
    assert await app.state.reports.process_export() == 1
    key = prefix + "1.csv"
    state, fence, object_key, size, purge = stored(admin, job["id"])
    assert (state, fence, object_key, purge) == ("ready", 1, key, False)
    assert keys(store, prefix) == [key]
    file = store.get_object(Bucket=BUCKET, Key=key)
    assert file["ContentLength"] == size and file["ContentType"] == "text/csv; charset=utf-8"

    body, response = await download(client, path, auth(root))
    issued = exact(ExportDownload, body)
    assert response.content == file["Body"].read()
    assert response.content.startswith(b"\xef\xbb\xbfid,sequence,")
    assert b"funding_credit" in response.content
    assert response.headers["content-disposition"] == (
        f'attachment; filename="nexus-ledger-{job["id"]}.csv"'
    )
    query = parse_qs(urlsplit(issued.url).query)
    assert query["X-Amz-Expires"] == ["300"]
    now = admin.execute("SELECT clock_timestamp()").fetchone()[0]
    lifetime = datetime.fromisoformat(issued.expires_at) - now
    assert timedelta(seconds=290) < lifetime <= timedelta(seconds=300)
    assert admin.execute(
        "SELECT count(*) FROM reporting.audit_events WHERE action='export.downloaded' "
        "AND resource_id=%s",
        (job["id"],),
    ).fetchone() == (1,)

    # A URL never outlives its export.
    admin.execute(
        "UPDATE reporting.export_jobs SET expires_at=clock_timestamp()+interval '42 seconds'"
    )
    body, _ = await download(client, path, auth(root))
    assert int(parse_qs(urlsplit(body["url"]).query)["X-Amz-Expires"][0]) <= 42


async def test_expired_export_has_no_url_and_its_objects_are_deleted(
    http, admin, store, monkeypatch
):
    client, root, app, _ = http
    reports = app.state.reports
    org, job, path = await queued_export(http)
    prefix = f"exports/{org['id']}/{job['id']}/"
    await reports.process_export()
    assert keys(store, prefix) == [prefix + "1.csv"]
    admin.execute(
        "UPDATE reporting.export_jobs SET expires_at=clock_timestamp()-interval '1 second'"
    )
    # Expired before cleanup ran: no URL, though the object is still there.
    refused = await client.get(path + "/download", headers=auth(root))
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "export_unavailable"
    assert (await client.get(path, headers=auth(root))).json()["state"] == "expired"
    assert await reports.process_export() == 0
    assert stored(admin, job["id"])[0::4] == ("expired", True)

    # Object storage is unreachable: expiry stands, the deletion waits for its lease to lapse.
    async def unreachable(prefix):
        raise EndpointConnectionError(endpoint_url="http://127.0.0.1:1")

    with monkeypatch.context() as patch:
        patch.setattr(reports.files, "delete_prefix", unreachable)
        assert await reports.purge_exports() == 0
    retry_in = admin.execute(
        "SELECT purge_at-clock_timestamp() FROM reporting.export_jobs WHERE id=%s", (job["id"],)
    ).fetchone()[0]
    assert timedelta(minutes=4) < retry_in <= timedelta(minutes=5)
    assert await reports.purge_exports() == 0
    assert keys(store, prefix) == [prefix + "1.csv"]
    assert (await client.get(path + "/download", headers=auth(root))).status_code == 409

    admin.execute("UPDATE reporting.export_jobs SET purge_at=clock_timestamp()")
    assert await reports.purge_exports() == 1
    assert keys(store, prefix) == []
    assert stored(admin, job["id"])[0::2] == ("expired", None, False)
    assert await reports.purge_exports() == 0


async def test_crash_after_upload_is_taken_over_by_another_worker(
    http, admin, store, monkeypatch, second_worker
):
    client, root, app, _ = http
    reports = app.state.reports
    org, job, path = await queued_export(http)
    prefix = f"exports/{org['id']}/{job['id']}/"
    upload = reports.files.put

    async def upload_then_crash(key, data, content_type):
        await upload(key, data, content_type)
        raise Crash

    with monkeypatch.context() as patch:
        patch.setattr(reports.files, "put", upload_then_crash)
        with pytest.raises(Crash):
            await reports.process_export()
    # Uploaded, never committed: the job is not ready and offers no URL.
    assert stored(admin, job["id"])[:3] == ("running", 1, None)
    assert keys(store, prefix) == [prefix + "1.csv"]
    assert (await client.get(path + "/download", headers=auth(root))).status_code == 409
    # Another Worker waits for the lease, takes the job over and commits its own upload.
    assert await second_worker.reports.process_export() == 0
    admin.execute(
        "UPDATE reporting.export_jobs SET lease_until=clock_timestamp()-interval '1 second'"
    )
    assert await second_worker.reports.process_export() == 1
    state, fence, key, size, _ = stored(admin, job["id"])
    assert (state, fence, key) == ("ready", 2, prefix + "2.csv")
    assert keys(store, prefix) == [prefix + "1.csv", prefix + "2.csv"]
    _, response = await download(client, path, auth(root))
    assert len(response.content) == size
    assert response.content == store.get_object(Bucket=BUCKET, Key=key)["Body"].read()

    # The first Worker comes back: its commit loses the fence and changes nothing.
    first = {"id": job["id"], "organization_id": org["id"], "kind": "ledger", "fence": 1}
    async with reports.tx() as repo:
        assert not await repo.finish_export(first | {"attempts": 1}, (prefix + "1.csv", size), 1)
    assert stored(admin, job["id"])[:3] == ("ready", 2, prefix + "2.csv")
    await download(client, path, auth(root))

    # Expiry deletes every attempt's upload, the orphan included.
    admin.execute(
        "UPDATE reporting.export_jobs SET expires_at=clock_timestamp()-interval '1 second'"
    )
    await second_worker.reports.process_export()
    assert await second_worker.reports.purge_exports() == 1
    assert keys(store, prefix) == []


async def test_upload_that_lands_after_deletion_is_deleted_again(http, admin, store):
    _, _, app, _ = http
    reports = app.state.reports
    org, job, _ = await queued_export(http)
    key = f"exports/{org['id']}/{job['id']}/1.csv"
    async with reports.tx() as repo:
        claimed = await repo.claim()
    assert claimed["fence"] == 1
    admin.execute(
        "UPDATE reporting.export_jobs SET expires_at=clock_timestamp()-interval '1 second'"
    )
    await reports.process_export()
    assert await reports.purge_exports() == 1
    assert stored(admin, job["id"])[0::4] == ("expired", False)
    # The claimed attempt was slow: its upload lands after the job's objects were deleted.
    await reports.files.put(key, b"late", "text/csv; charset=utf-8")
    async with reports.tx() as repo:
        assert not await repo.finish_export(claimed, (key, 4), 0)
    assert stored(admin, job["id"])[0::4] == ("expired", True)
    assert await reports.purge_exports() == 1
    assert keys(store, f"exports/{org['id']}/") == []
