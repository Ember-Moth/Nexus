import asyncio
import base64
import hashlib
import json
import os
import secrets
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

import psycopg
import pytest
from nacl.public import PrivateKey
from psycopg import sql
from test_api_contract import exact, pages
from test_billing import account_amounts, headers, setup_model, setup_wallet
from test_identity_api import auth, create_user, login

from nexus.gateway_simulator import GatewaySimulator
from nexus.modules.publication.application.schemas import (
    ClusterView,
    PrincipalView,
    ReleaseRecord,
)

pytestmark = pytest.mark.integration
RELAY_CRATE = Path(__file__).resolve().parents[2] / "src" / "relay"


async def register_node(client, root, admin, cid):
    name = "nexus_node_" + uuid4().hex
    password = secrets.token_urlsafe(32)
    admin.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE nexus_gateway").format(
            sql.Identifier(name), sql.Literal(password)
        )
    )
    # Node logins carry their statement timeout as a role setting (see ensure_gateway_login).
    admin.execute(
        sql.SQL("ALTER ROLE {} SET statement_timeout = '8s'").format(sql.Identifier(name))
    )
    response = await client.put(
        f"/api/v1/gateway-clusters/{cid}/principals/{(nid := uuid4())}",
        headers=auth(root),
        json={"database_role": name, "node_id": str(nid)},
    )
    assert response.status_code == 200, response.text
    return psycopg.conninfo.make_conninfo(
        host=admin.info.host,
        port=admin.info.port,
        dbname=admin.info.dbname,
        user=name,
        password=password,
    )


@pytest.fixture
async def plane(http, admin):
    async with provision_plane(http, admin) as values:
        yield values


@asynccontextmanager
async def provision_plane(
    http,
    admin,
    protocol="openai_compatible",
    *,
    balance="100",
    auto_publish=False,
    **upstream_options,
):
    client, root, app, settings = http
    org, _, _ = await setup_wallet(http, balance)
    model, dep, _, _, admission = await setup_model(http, org, protocol, **upstream_options)
    from test_project_credentials import key as issue_key

    issued = (await issue_key(client, root, org, str(admission.service_account_id))).json()
    raw = issued["secret"]
    admission = admission.model_copy(update={"api_key_id": UUID(issued["key"]["id"])})
    response = await client.put(
        f"/api/v1/route-policies/{org['id']}/{model['id']}",
        headers=auth(root),
        json={"candidates": [{"deployment_id": dep["id"]}]},
    )
    assert response.status_code == 201, response.text
    private = PrivateKey.generate()
    response = await client.post(
        "/api/v1/gateway-clusters",
        headers=auth(root),
        json={
            "name": "east",
            "region": "test",
            "organization_ids": [org["id"]],
            "deployment_ids": [dep["id"]],
            "public_key": base64.b64encode(bytes(private.public_key)).decode(),
            # These tests release by hand; test_auto_publish covers the worker path.
            "auto_publish": auto_publish,
        },
    )
    assert response.status_code == 201, response.text
    cid = response.json()["id"]
    dsn = await register_node(client, root, admin, cid)
    response = await client.post(
        f"/api/v1/gateway-clusters/{cid}/releases", headers=headers(root, "release-one"), json={}
    )
    assert response.status_code == 202, response.text
    async with GatewaySimulator.connect(
        dsn, settings.redis_url.get_secret_value(), settings.redis_namespace
    ) as peer:
        yield peer, private, org, model, dep, admission, raw, cid


async def test_gateway_to_ledger(plane, http, admin):
    peer, private, org, model, dep, admission, raw, cid = plane
    app = http[2]
    assert await app.state.publication.distribute() == 1
    snapshot = await peer.refresh()
    assert (
        await peer.credential(snapshot["resources"]["credential_refs"][0], bytes(private))
        == "provider-secret-do-not-echo"
    )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        await peer.connection.execute("SELECT * FROM billing.accounts")
    result, lease = await peer.reserve(raw, model["id"], 4, 6, admission.request_id)
    assert result["state"] == "reserved" and result["reserved_amount"] == "10.000000000000"
    aid = uuid4()
    await peer.call("start_attempt", admission.request_id, aid, 1, UUID(dep["id"]))
    closed = {
        "delivery_outcome": "succeeded",
        "attempt_ids": [str(aid)],
        "delivered_attempt_id": str(aid),
    }
    assert (await peer.event(admission.request_id, "request.closed", closed))[
        "status"
    ] == "accepted"
    await app.state.usage.process()
    assert admin.execute("SELECT state FROM billing.usage_jobs").fetchone()[0] == "waiting"
    with pytest.raises(psycopg.errors.RaiseException):
        await peer.call("start_attempt", admission.request_id, uuid4(), 2, UUID(dep["id"]))
    msg = {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "event_type": "attempt.finished",
        "occurred_at": "2026-11-13T00:00:00Z",
        "request_id": str(admission.request_id),
        "attempt_id": str(aid),
        "config_revision": "1",
        "payload": {
            "outcome": "succeeded",
            "upstream_request_id": "upstream-test",
            "usage_format": "fixture.tokens.v1",
            "raw_usage": {"input_total_tokens": 2, "cached_input_tokens": 0, "output_tokens": 3},
            "elapsed_ms": 2,
        },
    }
    for _ in range(2):
        assert (await peer.call("append_usage_event", json.dumps(msg)))["status"] == "accepted"
    msg["event_id"] = str(uuid4())
    assert (await peer.call("append_usage_event", json.dumps(msg)))["status"] == "conflict"
    assert await app.state.usage.process() == 1
    assert (await peer.call("get_request_status", admission.request_id))[
        "charged_amount"
    ] == "5.000000000000"
    assert account_amounts(admin, org) == (95, 0)
    assert admin.execute("SELECT cost_amount FROM billing.request_attempts").fetchone()[0] == 2
    assert await peer.limits.finish(lease, 5)


async def test_revocation_is_live_and_replay_is_owned_by_node(plane, http, admin):
    peer, _, org, model, dep, a, raw, cid = plane
    await peer.refresh()
    await peer.reserve(raw, model["id"], 4, 6, a.request_id)
    client, root, _, _ = http
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/api-keys/{a.api_key_id}/revoke", headers=auth(root, 1)
    )
    assert response.status_code == 200, response.text
    replay = await peer.call("reserve_request", a.request_id, a.api_key_id, a.model_id, 1, 4, 6)
    assert replay["replayed"]
    with pytest.raises(psycopg.errors.RaiseException, match="key_inactive"):
        await peer.call("reserve_request", uuid4(), a.api_key_id, a.model_id, 1, 4, 6)
    with pytest.raises(psycopg.errors.RaiseException, match="key_inactive"):
        await peer.call("start_attempt", a.request_id, uuid4(), 1, UUID(dep["id"]))
    dsn = await register_node(client, root, admin, cid)
    async with GatewaySimulator.connect(dsn, http[3].redis_url.get_secret_value()) as other:
        assert await other.call("get_request_status", a.request_id) is None
        with pytest.raises(psycopg.errors.RaiseException):
            await other.call("cancel_unstarted_request", a.request_id, "cancel")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await other.connection.execute(
                "SELECT identity.v1_authorize(%s,%s)", (a.api_key_id, a.model_id)
            )
    assert (await peer.call("cancel_unstarted_request", a.request_id, "cancel"))[
        "state"
    ] == "canceled"
    assert account_amounts(admin, org) == (100, 0)


async def test_evidence_review_and_transaction_rollback(plane, http, admin):
    peer, _, org, model, dep, a, raw, _ = plane
    await peer.refresh()
    async with peer.connection.pipeline():
        with pytest.raises(RuntimeError, match="non-pipelined"):
            await peer.call("reserve_request", a.request_id, a.api_key_id, a.model_id, 1, 4, 6)
    assert await peer.call("get_request_status", a.request_id) is None
    # The admission and inbox are both rolled back when their caller loses the transaction.
    with pytest.raises(RuntimeError):
        async with peer.connection.transaction():
            await peer.connection.execute(
                "SELECT gateway_v1.reserve_request(%s,%s,%s,1,4,6)",
                (a.request_id, a.api_key_id, a.model_id),
            )
            raise RuntimeError("abort")
    assert await peer.call("get_request_status", a.request_id) is None
    await peer.reserve(raw, model["id"], 4, 6, a.request_id)
    aid = uuid4()
    await peer.call("start_attempt", a.request_id, aid, 1, UUID(dep["id"]))
    with pytest.raises(psycopg.errors.RaiseException):
        await peer.call("cancel_unstarted_request", a.request_id, "cancel")
    with pytest.raises(psycopg.errors.RaiseException):
        await peer.event(a.request_id, "attempt.finished", {"prompt": "disallowed"}, attempt_id=aid)
    assert admin.execute("SELECT count(*) FROM billing.usage_events").fetchone()[0] == 0
    await peer.event(
        a.request_id,
        "attempt.finished",
        {
            "outcome": "succeeded",
            "usage_format": "fixture.tokens.v1",
            "upstream_request_id": "fixture",
            "elapsed_ms": 100,
            "raw_usage": None,
        },
        attempt_id=aid,
    )
    await peer.event(
        a.request_id,
        "request.closed",
        {
            "delivery_outcome": "succeeded",
            "delivered_attempt_id": str(aid),
            "attempt_ids": [str(aid)],
        },
    )
    await http[2].state.usage.process()
    status = await peer.call("get_request_status", a.request_id)
    assert status["state"] == "review" and status["reservation_state"] == "held"
    assert account_amounts(admin, org) == (100, 10)


async def test_configuration_recovery_rollback_and_old_ack(plane, http, admin):
    peer, _, _, _, _, _, _, cid = plane
    client, root, app, settings = http
    await app.state.publication.distribute()
    one = await peer.refresh()
    response = await client.post(
        f"/api/v1/gateway-clusters/{cid}/releases",
        headers=headers(root, "rollback"),
        json={"rollback_revision": 1},
    )
    assert response.status_code == 202, response.text
    assert response.json()["revision"] == "2"
    await app.state.publication.distribute()
    two = await peer.refresh()
    assert two["resources"] == one["resources"] and two["revision"] == "2"
    old = bytes(await peer.call("get_config_snapshot", 1))
    await peer.call("ack_config", 1, hashlib.sha256(old).hexdigest(), True, "")
    status = (
        await client.get(f"/api/v1/gateway-clusters/{cid}/releases/2", headers=auth(root))
    ).json()
    assert status["distributed_revision"] == status["nodes"][0]["applied_revision"] == "2"
    prefix = f"{settings.redis_namespace}:cfg:{{{cid}}}"
    await peer.redis.delete(prefix + ":current", prefix + ":snapshot:2")
    assert (await peer.refresh())["revision"] == "2"
    admin.execute(
        "UPDATE publication.config_outbox SET next_attempt_at=clock_timestamp() WHERE revision=2"
    )
    assert await app.state.publication.distribute() == 1
    assert await peer.redis.get(prefix + ":current") == "2"
    # A delayed old outbox replay cannot lower a pointer.
    admin.execute(
        "UPDATE publication.config_outbox SET delivered_at=NULL,"
        "next_attempt_at=clock_timestamp() WHERE revision=1"
    )
    await app.state.publication.distribute()
    assert await peer.redis.get(prefix + ":current") == "2"


async def test_cluster_read_principals_and_release_history(plane, http, admin):
    peer, _, _, _, _, _, _, cid = plane
    client, root, app, _ = http
    base = f"/api/v1/gateway-clusters/{cid}"
    read = await client.get(base, headers=auth(root))
    assert read.status_code == 200, read.text
    version = exact(ClusterView, read.json()).version
    assert read.headers["etag"] == f'"{version}"'
    assert await pages(client, root, "/api/v1/gateway-clusters", 1) == [read.json()]
    patch = {**auth(root), "If-Match": read.headers["etag"]}
    updated = await client.patch(base, headers=patch, json={"active": True})
    assert updated.status_code == 200, updated.text
    assert updated.headers["etag"] == f'"{version + 1}"'
    assert (await client.get(base, headers=auth(root))).json() == updated.json()

    await app.state.publication.distribute()
    await peer.refresh()
    for _ in range(2):
        await register_node(client, root, admin, cid)
    nodes = await pages(client, root, base + "/principals", 2)
    assert len(nodes) == 3 and [n["node_id"] for n in nodes] == sorted(n["node_id"] for n in nodes)
    views = {node["node_id"]: exact(PrincipalView, node) for node in nodes}
    applied = views.pop(peer.node_id)
    assert applied.applied_revision == "1" and applied.applied_at is not None
    assert applied.last_ack.model_dump() == {"revision": "1", "applied": True, "error_code": ""}
    for node in views.values():
        assert (node.applied_revision, node.applied_at, node.last_ack) == ("0", None, None)
        assert node.active and node.database_role.startswith("nexus_node_")
        assert node.capabilities == ["reservation.v1", "usage-events.v1"]

    me = (await client.get("/api/v1/me", headers=auth(root))).json()["user"]["id"]
    rollback = await client.post(
        base + "/releases", headers=headers(root, "history-rollback"), json={"rollback_revision": 1}
    )
    assert rollback.status_code == 202 and rollback.json()["revision"] == "2", rollback.text
    latest = await client.post(base + "/releases", headers=headers(root, "history"), json={})
    assert latest.status_code == 202 and latest.json()["revision"] == "3", latest.text
    assert await app.state.publication.distribute() == 1
    history = await pages(client, root, base + "/releases", 2)
    records = [exact(ReleaseRecord, release) for release in history]
    assert [r.revision for r in records] == ["3", "2", "1"]
    assert [r.rollback_revision for r in records] == [None, "1", None]
    assert {str(r.actor_id) for r in records} == {me}
    assert {r.initiator for r in records} == {"operator"}
    assert [r.sha256 for r in records[:2]] == [latest.json()["sha256"], rollback.json()["sha256"]]
    assert (records[0].distribution.attempts, records[0].distribution.delivered_at) == (0, None)
    assert all(r.distribution.delivered_at and r.distribution.attempts for r in records[1:])
    status = (await client.get(base + "/releases/2", headers=auth(root))).json()
    assert (status["sha256"], status["distribution"]) == (
        history[1]["sha256"],
        history[1]["distribution"],
    )

    await create_user(client, root, "cluster-viewer")
    viewer = await login(client, "cluster-viewer")
    unknown = f"/api/v1/gateway-clusters/{uuid4()}"
    for suffix in ["", "/principals", "/releases"]:
        denied = await client.get(base + suffix, headers=auth(viewer))
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "forbidden"
        missing = await client.get(unknown + suffix, headers=auth(root))
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    # A node cursor is not a release position.
    first = await client.get(base + "/principals", headers=auth(root), params={"limit": 1})
    for cursor in ["not-a-cursor", first.json()["next_cursor"]]:
        malformed = await client.get(
            base + "/releases", headers=auth(root), params={"cursor": cursor}
        )
        assert malformed.status_code == 422
        assert malformed.json()["error"]["code"] == "invalid_cursor"


async def test_atomic_multiscope_limits_and_lease_ownership(plane, http):
    peer, _, org, _, _, a, _, _ = plane
    policies = [
        {
            "id": str(uuid4()),
            "organization_id": org["id"],
            "scope_type": scope,
            "scope_id": str(sid),
            "metric": metric,
            "limit_value": limit,
        }
        for scope, sid, metric, limit in [
            ("organization", org["id"], "rpm", 3),
            ("project", a.project_id, "tpm", 10),
            ("key", a.api_key_id, "concurrency", 1),
        ]
    ]
    await http[2].state.publication.cache.prepare_limits(org["id"], True)
    args = (org["id"], a.project_id, a.api_key_id)
    rid = uuid4()
    lease = await peer.limits.acquire(*args, rid, 8, policies)
    replay = await peer.limits.acquire(*args, rid, 8, policies)
    assert replay["owner"] == lease["owner"]
    with pytest.raises(ValueError, match="limited"):
        await peer.limits.acquire(*args, uuid4(), 2, policies)
    assert await peer.limits.renew(lease)
    assert await peer.limits.finish(lease, 3)
    assert await peer.limits.finish(lease, 3)
    newer = await peer.limits.acquire(*args, uuid4(), 7, policies)
    assert not await peer.limits.renew(lease)
    assert await peer.limits.finish(newer)
    with pytest.raises(ValueError, match="limited"):
        await peer.limits.acquire(*args, uuid4(), 1, policies)


async def test_live_cost_eligibility_and_envelope_rotation(plane, http, admin):
    peer, private, _, model, dep, a, raw, cid = plane
    client, root, app, _ = http
    original = await peer.refresh()
    ref = original["resources"]["credential_refs"][0]
    await peer.reserve(raw, model["id"], 4, 6, a.request_id)
    replacement = PrivateKey.generate()
    update = await client.patch(
        f"/api/v1/gateway-clusters/{cid}",
        headers=auth(root, 2),
        json={
            "public_key": base64.b64encode(bytes(replacement.public_key)).decode(),
            "key_grace_seconds": 0,
        },
    )
    assert update.status_code == 200, update.text
    with pytest.raises(psycopg.errors.RaiseException, match="snapshot_scope_expired"):
        await peer.call("get_credential_envelope", 1, UUID(ref["credential_id"]), 1)
    response = await client.post(
        f"/api/v1/gateway-clusters/{cid}/releases", headers=headers(root, "rotation"), json={}
    )
    assert response.status_code == 202, response.text
    current = await peer.refresh()
    assert current["resources"]["credential_refs"][0]["key_version"] == 2
    from nacl.exceptions import CryptoError

    with pytest.raises(CryptoError):
        await peer.credential(current["resources"]["credential_refs"][0], bytes(private))
    assert (
        await peer.credential(current["resources"]["credential_refs"][0], bytes(replacement))
        == "provider-secret-do-not-echo"
    )
    fresh = uuid4()
    await peer.reserve(raw, model["id"], 4, 6, fresh)
    admin.execute(
        "UPDATE catalog.upstream_credentials SET status='revoked',"
        "revoked_at=clock_timestamp(),version=version+1 WHERE id=%s",
        (dep["credential_id"],),
    )
    with pytest.raises(psycopg.errors.RaiseException, match="deployment_unavailable"):
        await peer.call("start_attempt", fresh, uuid4(), 1, UUID(dep["id"]))
    with pytest.raises(psycopg.errors.RaiseException, match="credential_unavailable"):
        await peer.call("get_credential_envelope", 2, UUID(ref["credential_id"]), 1)
    assert await app.state.publication.distribute() == 1


async def test_settlement_commit_failure_replays_and_worker_finishes(
    plane, http, admin, service_directory, monkeypatch
):
    import asyncio
    import os
    import sys

    from nexus.modules.billing.persistence.repository import BillingRepository

    peer, _, org, model, dep, a, raw, _ = plane
    await peer.refresh()
    _, lease = await peer.reserve(raw, model["id"], 4, 6, a.request_id)
    aid = uuid4()
    await peer.start_attempt(a.request_id, aid, 1, dep["id"], lease)
    await peer.event(
        a.request_id,
        "attempt.finished",
        {
            "outcome": "succeeded",
            "upstream_request_id": "fixture",
            "usage_format": "fixture.tokens.v1",
            "raw_usage": {"input_total_tokens": 2, "cached_input_tokens": 0, "output_tokens": 1},
            "elapsed_ms": 10,
        },
        attempt_id=aid,
    )
    await peer.event(
        a.request_id,
        "request.closed",
        {
            "delivery_outcome": "succeeded",
            "attempt_ids": [str(aid)],
            "delivered_attempt_id": str(aid),
        },
    )

    async def crash(self, *args):
        raise RuntimeError("injected failure before task completion")

    with monkeypatch.context() as m:
        m.setattr(BillingRepository, "finish_usage", crash)
        with pytest.raises(RuntimeError):
            await http[2].state.usage.process()
    assert account_amounts(admin, org) == (100, 10)
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "nexus.cli",
        "--env-file",
        str(service_directory / ".env.worker"),
        "worker",
        env=dict(os.environ, NEXUS_WORKER_ID="gateway-settlement-test"),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if (await peer.call("get_request_status", a.request_id))["processing_state"] == "done":
                break
            await asyncio.sleep(0.05)
        assert account_amounts(admin, org) == (
            97,
            0,
        )
        assert (
            admin.execute(
                "SELECT count(*) FROM billing.ledger_entries WHERE kind='usage_debit'"
            ).fetchone()[0]
            == 1
        )
    finally:
        worker.terminate()
        await asyncio.wait_for(worker.wait(), 10)


async def test_late_failed_attempt_cost_is_independent(plane, http, admin):
    peer, _, org, model, dep, a, raw, _ = plane
    await peer.refresh()
    await peer.reserve(raw, model["id"], 4, 6, a.request_id)
    ids = [uuid4(), uuid4()]
    for index, aid in enumerate(ids, 1):
        await peer.call("start_attempt", a.request_id, aid, index, UUID(dep["id"]))
    payload = {
        "outcome": "succeeded",
        "upstream_request_id": "fixture",
        "usage_format": "fixture.tokens.v1",
        "raw_usage": {"input_total_tokens": 2, "cached_input_tokens": 0, "output_tokens": 1},
        "elapsed_ms": 1,
    }
    await peer.event(a.request_id, "attempt.finished", payload, attempt_id=ids[1])
    await peer.event(
        a.request_id,
        "request.closed",
        {
            "delivery_outcome": "succeeded",
            "attempt_ids": list(map(str, ids)),
            "delivered_attempt_id": str(ids[1]),
        },
    )
    await http[2].state.usage.process()
    assert (await peer.call("get_request_status", a.request_id))["processing_state"] == "waiting"
    assert account_amounts(admin, org) == (97, 0)
    await peer.event(
        a.request_id, "attempt.finished", {**payload, "outcome": "failed"}, attempt_id=ids[0]
    )
    await http[2].state.usage.process()
    assert (await peer.call("get_request_status", a.request_id))["processing_state"] == "done"
    assert account_amounts(admin, org) == (97, 0)
    assert admin.execute("SELECT sum(cost_amount) FROM billing.request_attempts").fetchone()[
        0
    ] == Decimal("2.4")


async def test_publication_after_redis_write_is_replayable(plane, http, admin, monkeypatch):
    from nexus.modules.publication.persistence.repository import PublicationRepository

    peer, _, _, _, _, _, _, cid = plane
    publication = http[2].state.publication

    async def lose_delivery(*args):
        raise RuntimeError("interrupted after Redis write")

    with monkeypatch.context() as patch:
        patch.setattr(PublicationRepository, "distributed", lose_delivery)
        with pytest.raises(RuntimeError):
            await publication.distribute()
    assert admin.execute("SELECT delivered_at FROM publication.config_outbox").fetchone()[0] is None
    assert (await peer.refresh())["revision"] == "1"
    admin.execute("UPDATE publication.config_outbox SET next_attempt_at=clock_timestamp()")
    await publication.distribute()
    assert (
        admin.execute("SELECT distributed_revision FROM publication.gateway_clusters").fetchone()[0]
        == 1
    )


async def test_cluster_isolation_and_scope_shrink(plane, http):
    peer, _, org, model, dep, a, raw, cid = plane
    client, root, app, _ = http
    snapshot = await peer.refresh()
    ref = snapshot["resources"]["credential_refs"][0]
    response = await client.patch(
        f"/api/v1/gateway-clusters/{cid}",
        headers=auth(root, 2),
        json={"organization_ids": [str(uuid4())]},
    )
    assert response.status_code == 200
    for operation, args in [
        ("config_head", ()),
        ("get_config_snapshot", (1,)),
        ("config_keys", (1, 0, None, None, None, 10)),
        ("get_credential_envelope", (1, UUID(ref["credential_id"]), 1)),
        ("reserve_request", (uuid4(), a.api_key_id, a.model_id, 1, 1, 1)),
    ]:
        with pytest.raises(psycopg.errors.RaiseException, match="snapshot_scope_expired"):
            await peer.call(operation, *args)
    response = await client.post(
        f"/api/v1/gateway-clusters/{cid}/releases", headers=headers(root, "invalid-scope"), json={}
    )
    assert response.status_code == 422


async def test_lease_expiry_and_replacement_after_cache_loss(plane, http):
    import asyncio

    peer, _, org, _, _, a, _, _ = plane
    policies = [
        {
            "id": str(uuid4()),
            "organization_id": org["id"],
            "scope_type": "organization",
            "scope_id": org["id"],
            "metric": "concurrency",
            "limit_value": 1,
        }
    ]
    await http[2].state.publication.cache.prepare_limits(org["id"], True)
    args = (org["id"], a.project_id, a.api_key_id, a.request_id, 1, policies)
    old = await peer.limits.acquire(*args, ttl_ms=100)
    await asyncio.sleep(0.12)
    assert not await peer.limits.renew(old)
    await peer.redis.delete(*old["keys"])
    await http[2].state.publication.cache.prepare_limits(org["id"], False)
    with pytest.raises(ValueError, match="recovering"):
        await peer.limits.acquire(*args, ttl_ms=1000)
    # Advance the persisted recovery deadline without waiting a minute.
    key = old["keys"][-1]
    marker = json.loads(await peer.redis.get(key))
    marker["after"] = 0
    await peer.redis.set(key, json.dumps(marker))
    new = await peer.limits.acquire(*args, ttl_ms=1000)
    assert not await peer.limits.finish(old)
    assert await peer.limits.renew(new)
    assert await peer.limits.finish(new)


async def test_published_policies_feed_simulator_and_slow_node_cannot_republish(plane, http):
    peer, _, org, model, dep, a, raw, cid = plane
    client, root, app, _ = http
    path = f"/api/v1/organizations/{org['id']}/quota-policies/platform/organization/{org['id']}/rpm"
    body = {"limit_value": 1}
    created = await client.put(path, headers=auth(root), json=body)
    assert created.status_code == 201, created.text
    assert (await client.put(path, headers=auth(root), json=body)).status_code == 428
    assert (await client.put(path, headers=auth(root, 9), json=body)).status_code == 412
    updated = await client.put(path, headers=auth(root, 1), json=body)
    assert updated.status_code == 200 and updated.json()["version"] == 2
    release = f"/api/v1/gateway-clusters/{cid}/releases"
    response = await client.post(release, headers=headers(root, "enable-quota"), json={})
    assert response.status_code == 202
    assert (
        await client.post(release, headers=headers(root, "enable-quota"), json={})
    ).json() == response.json()
    while await app.state.publication.distribute():
        pass
    await peer.refresh()
    hold, _ = await peer.reserve(raw, model["id"], 1, 1, a.request_id)
    assert hold["state"] == "reserved"
    with pytest.raises(ValueError, match="limited"):
        await peer.reserve(raw, model["id"], 1, 1, uuid4())
    node = peer.node_id
    role = (await (await peer.connection.execute("SELECT session_user")).fetchone())[0]
    response = await client.put(
        f"/api/v1/gateway-clusters/{cid}/principals/{node}",
        headers=auth(root),
        json={"node_id": node, "database_role": role, "capabilities": ["reservation.v1"]},
    )
    assert response.status_code == 200
    response = await client.post(release, headers=headers(root, "unsupported"), json={})
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "unsupported_capability"
    )


async def test_cli_simulator_with_worker(plane, http, service_directory):
    import asyncio
    import os
    import sys

    peer, _, _, model, _, _, raw, _ = plane
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "nexus.cli",
        "--env-file",
        str(service_directory / ".env.worker"),
        "worker",
        env=dict(os.environ, NEXUS_WORKER_ID="m4-simulator-worker"),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        result = await asyncio.wait_for(peer.simulate(raw, model["id"], 2, 1), 10)
        assert result["state"] == "settled" and result["charged_amount"] == "3.000000000000"
        assert await peer.redis.get(f"{peer.namespace}:node:{peer.node_id}") is not None
    finally:
        worker.terminate()
        await asyncio.wait_for(worker.wait(), 10)


async def test_gateway_retries_lost_commit_response_with_original_event(
    plane, http, admin, monkeypatch
):
    peer, _, org, model, dep, a, raw, _ = plane
    await peer.refresh()
    await peer.reserve(raw, model["id"], 4, 6, a.request_id)
    aid = uuid4()
    await peer.call("start_attempt", a.request_id, aid, 1, UUID(dep["id"]))
    send = peer._call_once
    replies = 0

    async def lose_first_reply(operation, args):
        nonlocal replies
        result = await send(operation, args)
        if operation == "append_usage_event":
            replies += 1
            if replies == 1:
                raise psycopg.OperationalError("injected lost commit response")
        return result

    monkeypatch.setattr(peer, "_call_once", lose_first_reply)
    reply = await peer.event(
        a.request_id,
        "request.closed",
        {
            "attempt_ids": [str(aid)],
            "delivery_outcome": "upstream_failed",
            "delivered_attempt_id": None,
        },
    )
    assert reply["status"] == "accepted" and replies == 2
    assert admin.execute("SELECT count(*) FROM billing.usage_events").fetchone()[0] == 1
    await http[2].state.usage.process()
    assert account_amounts(admin, org) == (100, 0)


async def test_attempt_rechecks_wallet_after_concurrent_freeze(plane, admin):
    import asyncio

    peer, _, org, model, deployment, admission, raw, _ = plane
    await peer.refresh()
    rid = admission.request_id
    await peer.reserve(raw, model["id"], 4, 6, rid)
    with admin.transaction():
        admin.execute(
            "UPDATE billing.accounts SET status='frozen' WHERE organization_id=%s", (org["id"],)
        )
        starting = asyncio.create_task(
            peer.call("start_attempt", rid, uuid4(), 1, UUID(deployment["id"]))
        )
        async with asyncio.timeout(5):
            while True:
                waiting = admin.execute(
                    "SELECT pg_backend_pid()=ANY(pg_blocking_pids(%s))",
                    (peer.connection.info.backend_pid,),
                ).fetchone()[0]
                if waiting:
                    break
                assert not starting.done(), "Attempt bypassed the in-flight wallet freeze"
                await asyncio.sleep(0.01)
    with pytest.raises(psycopg.errors.RaiseException, match="account_frozen"):
        await starting
    assert admin.execute(
        "SELECT count(*) FROM billing.request_attempts WHERE request_id=%s", (rid,)
    ).fetchone() == (0,)
    assert admin.execute(
        "SELECT balance,held_amount FROM billing.accounts WHERE organization_id=%s", (org["id"],)
    ).fetchone() == (100, 10)


async def test_native_profile_publication_attempt_binding_and_worker_replay(plane, http, admin):
    peer, private, org, model, dep, admission, _, cid = plane
    client, root, app, _ = http
    profile = {
        "endpoint": "openai_responses",
        "transports": ["http", "websocket"],
        "input_ceiling": 4,
        "output_ceiling": 6,
        "count_rule": "deployment_ceiling.v1",
        "bound_reference": "isolated-contract-v1",
    }
    path = f"/api/v1/deployments/{dep['id']}"
    result = await client.patch(
        path, headers=auth(root, dep["version"]), json={"relay_profile": profile}
    )
    assert result.status_code == 200, result.text
    version = result.json()["version"]
    release = f"/api/v1/gateway-clusters/{cid}/releases"
    blocked = await client.post(release, headers=headers(root, "native"), json={})
    assert blocked.status_code == 409
    node_id, role = admin.execute(
        "SELECT node_id, database_role FROM publication.gateway_principals WHERE cluster_id=%s",
        (cid,),
    ).fetchone()
    caps = ["reservation.v1", "usage-events.v1", "relay.native.v1", "relay.provider-features.v1"]
    node_path = f"/api/v1/gateway-clusters/{cid}/principals/{node_id}"
    for extra, status in [([], 409), (["relay.responses-ws.v1"], 202)]:
        result = await client.put(
            node_path,
            headers=auth(root),
            json={"node_id": str(node_id), "database_role": role, "capabilities": caps + extra},
        )
        assert result.status_code == 200, result.text
        result = await client.post(release, headers=headers(root, "native"), json={})
        assert result.status_code == status, result.text
    with pytest.raises(ValueError, match="Unsupported snapshot"):
        await peer.refresh()
    head = await peer.call("config_head")
    manifest = json.loads(bytes(await peer.call("get_config_snapshot", int(head["revision"]))))
    deployment = manifest["resources"]["deployments"][0]
    assert deployment["usage_format"] == "openai.responses.v1"
    assert deployment["relay_profile"]["transports"] == profile["transports"]
    rev = int(manifest["revision"])
    await peer.call("ack_config", rev, head["sha256"], True, "")
    for inputs, outputs in [(3, 6), (4, 7), (4, 0)]:
        rejected = uuid4()
        with pytest.raises(psycopg.errors.RaiseException, match="deployment_unavailable"):
            await peer.call(
                "reserve_request",
                rejected,
                admission.api_key_id,
                UUID(model["id"]),
                rev,
                inputs,
                outputs,
            )
        assert await peer.call("get_request_status", rejected) is None
    valid = {
        "usage_format": "openai.responses.v1",
        "raw_usage": {
            "input_tokens": 2,
            "cached_input_tokens": 0,
            "output_tokens": 3,
            "reasoning_tokens": 1,
        },
    }
    spoof = {
        "usage_format": "fixture.dimensions.v1",
        "raw_usage": {"input_uncached_tokens": 2, "output_tokens": 3},
    }
    await check_rust_gateway(peer, private, cid, admission, model, dep, valid, rev, app)
    attempts = []
    for evidence in [valid, spoof]:
        rid, aid = uuid4(), uuid4()
        await peer.call("reserve_request", rid, admission.api_key_id, UUID(model["id"]), rev, 4, 6)
        for replay in [False, True]:
            result = await peer.call("start_attempt", rid, aid, 1, UUID(dep["id"]))
            assert result["replayed"] == replay
        attempts.append((rid, aid, evidence))

    # Configuration is removed while requests are in flight. The Worker reads the attempt's binding.
    result = await client.patch(path, headers=auth(root, version), json={"relay_profile": None})
    assert result.status_code == 200, result.text
    for rid, aid, evidence in attempts:
        event = peer.prepare_event(
            rid,
            "attempt.finished",
            {
                "outcome": "succeeded",
                "elapsed_ms": 3,
                "upstream_request_id": "native-test",
                **evidence,
            },
            attempt_id=aid,
            revision=str(rev),
        )
        for _ in range(2):
            assert (await peer.call("append_usage_event", event))["status"] == "accepted"
        await peer.event(
            rid,
            "request.closed",
            {
                "delivery_outcome": "succeeded",
                "delivered_attempt_id": str(aid),
                "attempt_ids": [str(aid)],
            },
            revision=str(rev),
        )
    assert await app.state.usage.process() == 2
    valid_status = await peer.call("get_request_status", attempts[0][0])
    assert Decimal(valid_status["charged_amount"]) == 5
    invalid_status = await peer.call("get_request_status", attempts[1][0])
    assert (invalid_status["state"], invalid_status["reservation_state"]) == ("review", "held")
    assert account_amounts(admin, org) == (90, 10)


async def check_rust_gateway(peer, private, cid, admission, model, dep, valid, rev, app):
    # Drive the Rust adapter through the restricted login against this same publication.
    rid, aid = uuid4(), uuid4()
    info = peer.connection.info
    login = f"{info.user}:{quote(info.password, safe='')}"
    url = f"postgresql://{login}@{info.host}:{info.port}/{info.dbname}"
    test_inputs = {
        "url": url,
        "cluster": cid,
        "key": str(admission.api_key_id),
        "model": model["id"],
        "deployment": dep["id"],
        "request": str(rid),
        "attempt": str(aid),
        "unstarted": str(uuid4()),
        "private": base64.b64encode(bytes(private)).decode(),
        "secret_digest": hashlib.sha256(b"provider-secret-do-not-echo").hexdigest(),
        "events": [
            peer.prepare_event(
                rid,
                "request.closed",
                {
                    "delivery_outcome": "succeeded",
                    "delivered_attempt_id": str(aid),
                    "attempt_ids": [str(aid)],
                },
                revision=str(rev),
            ),
            peer.prepare_event(
                rid,
                "attempt.finished",
                {
                    "outcome": "succeeded",
                    "elapsed_ms": 3,
                    "upstream_request_id": "rust-contract",
                    **valid,
                },
                attempt_id=aid,
                revision=str(rev),
            ),
        ],
    }
    process = await asyncio.create_subprocess_exec(
        "cargo",
        "test",
        "--locked",
        "--test",
        "control",
        "isolated_gateway_contract",
        "--",
        "--ignored",
        cwd=RELAY_CRATE,
        env={**os.environ, "NEXUS_RELAY_TEST_INPUT": json.dumps(test_inputs)},
    )
    assert await process.wait() == 0
    assert await app.state.usage.process() == 1
    assert Decimal((await peer.call("get_request_status", rid))["charged_amount"]) == 5
