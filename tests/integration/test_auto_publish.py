"""Automatic publication: worker wake-up, debounce, system actor, rollback pause and scopes."""

import asyncio
import base64
import os
import sys
from contextlib import asynccontextmanager
from time import monotonic
from uuid import uuid4

import psycopg
import pytest
from nacl.exceptions import CryptoError
from nacl.public import PrivateKey
from test_billing import funding, headers, post_funding, publish_price, receiving
from test_catalog import make_deployment, make_policy
from test_identity_api import auth, create_org
from test_plane import provision_plane, register_node
from test_project_credentials import account, key, project

from nexus.gateway_simulator import GatewaySimulator
from nexus.modules.publication.application.automation import AutoPublisher

pytestmark = pytest.mark.integration


@pytest.fixture
async def plane(http, admin):
    """A listed cluster with release 1, created with auto_publish off like before."""
    async with provision_plane(http, admin) as values:
        yield values


async def instant(seconds):
    """A wake-up source that never reports a change; ticks are driven by the test."""
    return False


def publisher(app, quiet=0.0, max_delay=30.0):
    service = app.state.publication
    return AutoPublisher(service, service.tx, instant, quiet=quiet, max_delay=max_delay, poll=30)


async def cluster(client, root, cid):
    listed = (await client.get("/api/v1/gateway-clusters", headers=auth(root))).json()["items"]
    return next(item for item in listed if item["id"] == cid)


def revisions(admin, cid):
    return [
        row[0]
        for row in admin.execute(
            "SELECT revision FROM publication.config_snapshots WHERE cluster_id=%s "
            "ORDER BY revision",
            (cid,),
        )
    ]


async def new_key(client, root, org, service_account_id):
    issued = (await key(client, root, org, str(service_account_id))).json()
    return issued["secret"], issued["key"]["id"]


async def create_cluster(client, root, dep, private, **fields):
    response = await client.post(
        "/api/v1/gateway-clusters",
        headers=auth(root),
        json={
            "name": "cluster-" + uuid4().hex[:8],
            "region": "test",
            "deployment_ids": [dep["id"]],
            "public_key": base64.b64encode(bytes(private.public_key)).decode(),
            **fields,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@asynccontextmanager
async def worker(service_directory, settings, **environment):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "nexus.cli",
        "--env-file",
        str(service_directory / ".env.worker"),
        "worker",
        env=dict(
            os.environ,
            NEXUS_WORKER_ID="auto-publish-" + uuid4().hex[:8],
            NEXUS_REDIS_NAMESPACE=settings.redis_namespace,
            **environment,
        ),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        yield process
    finally:
        process.terminate()
        await asyncio.wait_for(process.wait(), 10)


@asynccontextmanager
async def watching(peer):
    stop = asyncio.Event()
    task = asyncio.create_task(peer.watch(stop, refresh_interval=1))
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


async def reserve_until(peer, secret, model, deadline):
    while True:
        try:
            return await peer.reserve(secret, model, 1, 1)
        except ValueError:
            if monotonic() > deadline:
                raise
            await asyncio.sleep(0.1)


async def test_new_key_reaches_the_gateway_without_a_release(http, admin, service_directory):
    client, root, app, settings = http
    async with provision_plane(http, admin, auto_publish=True) as values:
        peer, _, org, model, _, admission, _, cid = values
        assert await app.state.publication.distribute() == 1
        async with worker(service_directory, settings), watching(peer):
            # The worker settles the manual release without publishing it again.
            for _ in range(100):
                if (await cluster(client, root, cid))["auto_publish_status"]["state"] == "current":
                    break
                await asyncio.sleep(0.1)
            secret, key_id = await new_key(client, root, org, admission.service_account_id)
            issued = monotonic()
            result, lease = await reserve_until(peer, secret, model["id"], issued + 10)
            elapsed = monotonic() - issued
            print(f"new key usable after {elapsed:.2f}s")
            assert result["state"] == "reserved" and elapsed < 10, elapsed
            assert await peer.limits.finish(lease, 0)
        view = await cluster(client, root, cid)
        assert view["desired_revision"] == "2" and revisions(admin, cid) == [1, 2]
        status = view["auto_publish_status"]
        assert status["state"] == "current" and status["last_published"]["revision"] == "2"
        assert set(status["last_published"]["trigger"]) == {"identity"}
        actor, details = admin.execute(
            "SELECT actor_id,details FROM publication.audit_events "
            "WHERE action='config.released' AND details->>'revision'='2'"
        ).fetchone()
        assert actor is None and details["actor"] == "auto_publisher"
        assert details["trigger"]["identity"][0] != details["trigger"]["identity"][1]
        history = await client.get(f"/api/v1/gateway-clusters/{cid}/releases", headers=auth(root))
        assert [
            (r["revision"], r["initiator"], r["actor_id"] is None) for r in history.json()["items"]
        ] == [
            ("2", "auto_publisher", True),
            ("1", "operator", False),
        ]
        assert key_id in {k["id"] for k in peer.snapshot["resources"]["key_verifiers"]}


async def test_source_notifications_wake_the_worker(http):
    from nexus.infrastructure.postgres import Database

    client, root, _, settings = http
    database = Database(
        settings.database_url.get_secret_value(),
        runtime_role="nexus_api",
        pool_size=1,
        max_overflow=0,
        timeout=3,
    )
    wake = database.notifications("nexus_source_revision")
    try:
        assert await wake(1) is True  # connecting reports a possibly missed change
        assert await wake(0.2) is False
        waiting = asyncio.create_task(wake(10))
        await asyncio.sleep(0.2)
        started = monotonic()
        response = await client.post(
            "/api/v1/organizations", headers=auth(root), json={"name": "N"}
        )
        assert response.status_code == 201
        assert await waiting is True and monotonic() - started < 2
    finally:
        await wake.close()
        await database.close()


async def test_personal_organization_is_served_by_an_all_cluster(http, admin, signup):
    # Self-service registration is off by default; signup opens it with email verification.
    client, root, app, _ = http
    async with provision_plane(http, admin) as values:
        _, _, org, model, dep, _, _, _ = values
        private = PrivateKey.generate()
        created = await create_cluster(client, root, dep, private, organization_scope="all")
        cid = created["id"]
        assert created["organization_ids"] == [] and created["auto_publish"] is True
        assert created["auto_publish_status"]["state"] == "unknown"
        dsn = await register_node(client, root, admin, cid)
        auto = publisher(app)
        assert (await auto.tick())[0] == 1
        user = await signup("self-service-" + uuid4().hex[:6])
        personal = {"id": user["personal_organization_id"]}
        await app.state.process_billing()
        # Platform operators grant the model, route it and fund the wallet; no release follows.
        await make_policy(client, root, personal, [model["id"]])
        route = await client.put(
            f"/api/v1/route-policies/{personal['id']}/{model['id']}",
            headers=auth(root),
            json={"candidates": [{"deployment_id": dep["id"]}]},
        )
        assert route.status_code == 201, route.text
        record = await funding(client, root, personal, await receiving(client, root, "bank-p"))
        await post_funding(client, root, personal, record)
        session = user["access_token"]
        proj = await project(client, session, personal, "own-project")
        sa = await account(client, session, personal, proj["id"])
        secret, _ = await new_key(client, session, personal, sa["id"])
        assert (await auto.tick())[0] == 1
        while await app.state.publication.distribute():
            pass
        async with GatewaySimulator.connect(
            dsn, http[3].redis_url.get_secret_value(), http[3].redis_namespace
        ) as peer:
            snapshot = await peer.refresh()
            owners = {k["organization_id"] for k in snapshot["resources"]["key_verifiers"]}
            assert owners == {org["id"], personal["id"]}
            result, lease = await peer.reserve(secret, model["id"], 1, 1)
            assert result["state"] == "reserved"
            assert await peer.limits.finish(lease, 0)
        # The personal rule leaves the enterprise organization out.
        rule = await create_cluster(
            client, root, dep, PrivateKey.generate(), organization_scope="personal"
        )
        assert (await publisher(app).tick())[0] >= 1
        manifest = admin.execute(
            "SELECT manifest FROM publication.config_snapshots WHERE cluster_id=%s",
            (rule["id"],),
        ).fetchone()[0]
        assert {k["organization_id"] for k in manifest["resources"]["key_verifiers"]} == {
            personal["id"]
        }


async def test_suspended_organization_leaves_an_all_cluster(http, admin):
    client, root, app, settings = http
    async with provision_plane(http, admin) as values:
        _, _, org, model, dep, admission, raw, _ = values
        # Another routed tenant keeps the cluster's deployment publishable.
        other = await create_org(client, root, "Other tenant")
        await app.state.process_billing()
        route = await client.put(
            f"/api/v1/route-policies/{other['id']}/{model['id']}",
            headers=auth(root),
            json={"candidates": [{"deployment_id": dep["id"]}]},
        )
        assert route.status_code == 201, route.text
        private = PrivateKey.generate()
        cid = (await create_cluster(client, root, dep, private, organization_scope="all"))["id"]
        dsn = await register_node(client, root, admin, cid)
        auto = publisher(app)
        await auto.tick()
        async with GatewaySimulator.connect(
            dsn, settings.redis_url.get_secret_value(), settings.redis_namespace
        ) as peer:
            await peer.refresh()
            current = await client.get(f"/api/v1/organizations/{org['id']}", headers=auth(root))
            suspended = await client.patch(
                f"/api/v1/organizations/{org['id']}",
                headers=auth(root, current.json()["version"]),
                json={"status": "suspended"},
            )
            assert suspended.status_code == 200, suspended.text
            # The organization is rejected live before any release removes it.
            with pytest.raises(psycopg.errors.RaiseException, match="key_inactive"):
                await peer.reserve(raw, model["id"], 1, 1)
            assert (await auto.tick())[0] == 1
            snapshot = await peer.refresh()
            assert snapshot["resources"]["key_verifiers"] == []
            # Dropping an inactive organization does not expire the previous snapshot.
            assert (await peer.call("get_config_snapshot", 1))["manifest"]["revision"] == "1"


async def test_rollback_pauses_auto_publish_until_resumed(plane, http, admin):
    peer, _, org, model, _, admission, _, cid = plane
    client, root, app, _ = http
    enabled = await client.patch(
        f"/api/v1/gateway-clusters/{cid}",
        headers=auth(root, (await cluster(client, root, cid))["version"]),
        json={"auto_publish": True},
    )
    assert enabled.status_code == 200, enabled.text
    auto = publisher(app)
    assert (await auto.tick())[0] == 0  # the manual release already matches the sources
    await new_key(client, root, org, admission.service_account_id)
    assert (await auto.tick())[0] == 1 and revisions(admin, cid) == [1, 2]
    response = await client.post(
        f"/api/v1/gateway-clusters/{cid}/releases",
        headers=headers(root, "rollback"),
        json={"rollback_revision": 1},
    )
    assert response.status_code == 202 and response.json()["revision"] == "3"
    view = await cluster(client, root, cid)
    assert view["auto_publish"] is False
    status = view["auto_publish_status"]
    assert status["state"] == "paused" and status["paused"]["revision"] == "3"
    assert status["paused"]["reason"] == "rollback"
    await new_key(client, root, org, admission.service_account_id)
    assert await auto.tick() == (0, None) and revisions(admin, cid) == [1, 2, 3]
    resumed = await client.patch(
        f"/api/v1/gateway-clusters/{cid}",
        headers=auth(root, view["version"]),
        json={"auto_publish": True},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["auto_publish_status"]["paused"] is None
    assert (await auto.tick())[0] == 1 and revisions(admin, cid) == [1, 2, 3, 4]
    three, four = (
        admin.execute(
            "SELECT manifest->'resources'->'key_verifiers' FROM publication.config_snapshots "
            "WHERE cluster_id=%s AND revision=%s",
            (cid, rev),
        ).fetchone()[0]
        for rev in (3, 4)
    )
    assert len(three) == 2 and len(four) == 4


async def test_validation_failure_is_recorded_and_recovers(plane, http, admin):
    peer, _, org, model, dep, _, _, cid = plane
    client, root, app, _ = http
    current = await cluster(client, root, cid)
    other = await make_deployment(
        client, root, model, {"id": dep["upstream_id"]}, {"id": dep["credential_id"]}
    )
    response = await client.patch(
        f"/api/v1/gateway-clusters/{cid}",
        headers=auth(root, current["version"]),
        json={"auto_publish": True, "deployment_ids": [dep["id"], other["id"]]},
    )
    assert response.status_code == 200, response.text
    route = await client.put(
        f"/api/v1/route-policies/{org['id']}/{model['id']}",
        headers=auth(root, 1),
        json={"candidates": [{"deployment_id": dep["id"]}, {"deployment_id": other["id"]}]},
    )
    assert route.status_code == 200, route.text
    auto = publisher(app)
    assert await auto.tick() == (0, None)
    status = (await cluster(client, root, cid))["auto_publish_status"]
    assert status["state"] == "failing" and status["last_failure"]["code"] == "invalid_snapshot"
    assert "cost price" in status["last_failure"]["message"] and revisions(admin, cid) == [1]
    metrics = await app.state.publication.metrics()
    assert metrics["auto_publish_failing"] == 1 and metrics["auto_publish_pending"] == 0
    # Unchanged sources are not retried.
    await auto.tick()
    assert (await cluster(client, root, cid))["auto_publish_status"]["last_failure"] == status[
        "last_failure"
    ]
    await publish_price(client, root, "cost", other, "0.4")
    assert (await auto.tick())[0] == 1 and revisions(admin, cid) == [1, 2]
    recovered = (await cluster(client, root, cid))["auto_publish_status"]
    assert recovered["state"] == "current" and recovered["last_failure"] == status["last_failure"]
    assert set(recovered["last_published"]["trigger"]) >= {"billing", "effective_prices"}


async def test_debounce_waits_for_quiet_sources_up_to_the_maximum_delay(plane, http, admin):
    peer, _, org, _, _, admission, _, cid = plane
    client, root, app, _ = http
    current = await cluster(client, root, cid)
    await client.patch(
        f"/api/v1/gateway-clusters/{cid}",
        headers=auth(root, current["version"]),
        json={"auto_publish": True},
    )
    auto = publisher(app, quiet=0.8, max_delay=2.0)
    assert await auto.tick() == (0, None)
    started = monotonic()
    while monotonic() - started < 1.2:
        await new_key(client, root, org, admission.service_account_id)
        published, delay = await auto.tick()
        assert published == 0 and 0 < delay <= 0.8
        assert (await cluster(client, root, cid))["auto_publish_status"]["state"] == "pending"
        await asyncio.sleep(0.2)
    # Changes kept arriving inside the quiet window; the maximum delay still publishes.
    await asyncio.sleep(max(0.0, started + 2.2 - monotonic()))
    await new_key(client, root, org, admission.service_account_id)
    assert (await auto.tick())[0] == 1 and revisions(admin, cid) == [1, 2]
    await new_key(client, root, org, admission.service_account_id)
    assert (await auto.tick())[0] == 0
    await asyncio.sleep(0.9)
    assert (await auto.tick())[0] == 1 and revisions(admin, cid) == [1, 2, 3]


async def test_auto_and_manual_releases_race_without_gaps(plane, http, admin):
    peer, _, org, _, _, admission, _, cid = plane
    client, root, app, _ = http
    current = await cluster(client, root, cid)
    await client.patch(
        f"/api/v1/gateway-clusters/{cid}",
        headers=auth(root, current["version"]),
        json={"auto_publish": True},
    )
    workers = [publisher(app), publisher(app)]
    for round_ in range(4):
        await new_key(client, root, org, admission.service_account_id)
        manual = [
            client.post(
                f"/api/v1/gateway-clusters/{cid}/releases",
                headers=headers(root, f"race-{round_}-{n}"),
                json={},
            )
            for n in range(2)
        ]
        results = await asyncio.gather(*manual, *(w.tick() for w in workers))
        for response in results[:2]:
            assert response.status_code in {202, 409}, response.text
            if response.status_code == 409:
                assert response.json()["error"]["code"] == "cluster_changed"
    seen = revisions(admin, cid)
    assert seen == list(range(1, len(seen) + 1))
    view = await cluster(client, root, cid)
    assert view["desired_revision"] == str(seen[-1])
    # Whatever won each race, the sources are settled and nothing more is published.
    await asyncio.gather(*(w.tick() for w in workers))
    assert revisions(admin, cid) == seen
    newest = admin.execute(
        "SELECT manifest->'resources'->'key_verifiers' FROM publication.config_snapshots "
        "WHERE cluster_id=%s AND revision=%s",
        (cid, seen[-1]),
    ).fetchone()[0]
    assert len(newest) == 6


async def test_disabled_auto_publish_keeps_manual_releases(plane, http, admin, service_directory):
    peer, _, org, _, _, admission, _, cid = plane
    client, root, app, settings = http
    view = await cluster(client, root, cid)
    assert view["auto_publish"] is False and view["auto_publish_status"]["state"] == "disabled"
    async with worker(
        service_directory,
        settings,
        NEXUS_AUTO_PUBLISH_QUIET_SECONDS="0",
        NEXUS_AUTO_PUBLISH_MAX_DELAY_SECONDS="0.5",
    ):
        await new_key(client, root, org, admission.service_account_id)
        await asyncio.sleep(3)
        assert revisions(admin, cid) == [1]
    assert await publisher(app).tick() == (0, None)
    response = await client.post(
        f"/api/v1/gateway-clusters/{cid}/releases", headers=headers(root, "manual"), json={}
    )
    assert response.status_code == 202 and response.json()["revision"] == "2"
    assert (await cluster(client, root, cid))["auto_publish_status"]["state"] == "disabled"


async def test_overlapping_all_clusters_seal_credentials_per_cluster(http, admin):
    client, root, app, settings = http
    async with provision_plane(http, admin) as values:
        _, _, _, model, dep, _, raw, _ = values
        keys = [PrivateKey.generate(), PrivateKey.generate()]
        clusters = [
            (await create_cluster(client, root, dep, k, organization_scope="all"))["id"]
            for k in keys
        ]
        nodes = [await register_node(client, root, admin, cid) for cid in clusters]
        assert (await publisher(app).tick())[0] == 2
        while await app.state.publication.distribute():
            pass
        snapshots = []
        for dsn, private, other in zip(nodes, keys, reversed(keys), strict=True):
            async with GatewaySimulator.connect(
                dsn, settings.redis_url.get_secret_value(), settings.redis_namespace
            ) as peer:
                snapshot = await peer.refresh()
                ref = snapshot["resources"]["credential_refs"][0]
                assert await peer.credential(ref, bytes(private)) == "provider-secret-do-not-echo"
                with pytest.raises(CryptoError):
                    await peer.credential(ref, bytes(other))
                result, lease = await peer.reserve(raw, model["id"], 1, 1)
                assert result["state"] == "reserved"
                assert await peer.limits.finish(lease, 0)
                snapshots.append(snapshot["resources"])
        assert snapshots[0] == snapshots[1]


async def test_rule_based_scope_validation_and_generations(plane, http):
    peer, _, org, _, dep, _, _, cid = plane
    client, root, _, _ = http
    private = PrivateKey.generate()
    for body in [
        {"organization_scope": "all", "organization_ids": [org["id"]]},
        {"organization_scope": "listed"},
    ]:
        response = await client.post(
            "/api/v1/gateway-clusters",
            headers=auth(root),
            json={
                "name": "invalid",
                "region": "test",
                "deployment_ids": [dep["id"]],
                "public_key": base64.b64encode(bytes(private.public_key)).decode(),
                **body,
            },
        )
        assert response.status_code == 422, response.text
    view = await cluster(client, root, cid)

    async def patch(body):
        nonlocal view
        response = await client.patch(
            f"/api/v1/gateway-clusters/{cid}", headers=auth(root, view["version"]), json=body
        )
        if response.status_code == 200:
            view = response.json()
        return response

    generation = view["scope_generation"]
    widened = await patch({"organization_scope": "all"})
    assert widened.status_code == 200 and view["organization_ids"] == []
    assert view["scope_generation"] == generation
    assert (await patch({"organization_ids": [org["id"]]})).status_code == 422
    narrowed = await patch({"organization_scope": "listed", "organization_ids": [org["id"]]})
    assert narrowed.status_code == 200 and view["scope_generation"] == generation + 1
    assert (await patch({"organization_scope": "listed"})).status_code == 200
    assert (await patch({"organization_scope": "personal"})).json()["scope_generation"] == (
        generation + 2
    )
    assert (await patch({"organization_scope": "listed"})).status_code == 422
    assert view["organization_scope"] == "personal" and view["organization_ids"] == []


async def test_superseded_snapshot_copies_expire(plane, http, admin):
    peer, _, org, _, _, admission, _, cid = plane
    client, root, app, settings = http
    service = app.state.publication
    while await service.distribute():
        pass
    await new_key(client, root, org, admission.service_account_id)
    response = await client.post(
        f"/api/v1/gateway-clusters/{cid}/releases", headers=headers(root, "two"), json={}
    )
    assert response.status_code == 202, response.text
    while await service.distribute():
        pass
    prefix = f"{settings.redis_namespace}:cfg:{{{cid}}}"
    assert await peer.redis.get(prefix + ":current") == "2"
    assert await peer.redis.pttl(prefix + ":snapshot:2") == -1
    assert 0 < await peer.redis.pttl(prefix + ":snapshot:1") <= 600_000
    # A delayed delivery of an older release stays temporary and cannot move the pointer.
    await peer.redis.delete(prefix + ":snapshot:1")
    admin.execute(
        "UPDATE publication.config_outbox SET delivered_at=NULL,next_attempt_at=clock_timestamp() "
        "WHERE cluster_id=%s AND revision=1",
        (cid,),
    )
    assert await service.distribute() == 1
    assert await peer.redis.get(prefix + ":current") == "2"
    assert 0 < await peer.redis.pttl(prefix + ":snapshot:1") <= 600_000
    assert (await peer.refresh())["revision"] == "2"


async def test_organization_lifecycle_advances_the_identity_revision(http, admin, signup):
    client, root, app, _ = http
    organizations = app.state.publication.organizations
    await app.state.process_billing()  # settle organizations created by the fixture
    baseline = {str(o["id"]): o["personal"] for o in await organizations()}

    def revision():
        return admin.execute("SELECT revision FROM identity.source_revision").fetchone()[0]

    async def added():
        current = {str(o["id"]): o["personal"] for o in await organizations()}
        return {k: v for k, v in current.items() if k not in baseline}

    async def status(org, value):
        current = await client.get(f"/api/v1/organizations/{org['id']}", headers=auth(root))
        response = await client.patch(
            f"/api/v1/organizations/{org['id']}",
            headers=auth(root, current.json()["version"]),
            json={"status": value},
        )
        assert response.status_code == 200, response.text

    seen = revision()
    org = await create_org(client, root, "Lifecycle tenant")
    assert revision() > seen and await added() == {}
    seen = revision()
    await app.state.process_billing()  # provisioning -> active once billing opens the account
    assert revision() > seen and await added() == {org["id"]: False}
    for value, expected in [("suspended", {}), ("active", {org["id"]: False}), ("archived", {})]:
        seen = revision()
        await status(org, value)
        assert revision() > seen and await added() == expected
    seen = revision()
    user = await signup("lifecycle-" + uuid4().hex[:6])
    await app.state.process_billing()
    assert revision() > seen
    assert await added() == {user["personal_organization_id"]: True}
