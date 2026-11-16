"""Admission through the per-revision index and the cluster key table (migration 0044).

Reservations read the key as of the revision's key sequence and only the organization's
routes, deployments, models and access policies; the first attempt commits with the
reservation. These tests drive the SQL contracts through the Python gateway simulator.
"""

import base64
import hashlib
from uuid import UUID, uuid4

import psycopg
import pytest
from nacl.public import PrivateKey
from test_billing import account_amounts, headers
from test_identity_api import auth, create_org
from test_plane import plane, register_node  # noqa: F401 - pytest fixture
from test_project_credentials import key as issue_key

from nexus.gateway_simulator import GatewaySimulator

pytestmark = pytest.mark.integration


async def release(http, cid, name):
    client, root, app, _ = http
    response = await client.post(
        f"/api/v1/gateway-clusters/{cid}/releases", headers=headers(root, name), json={}
    )
    assert response.status_code == 202, response.text
    while await app.state.publication.distribute():
        pass
    return int(response.json()["revision"])


def reserve_call(admission, rev, *, rid=None, key=None, first=()):
    return (
        rid or uuid4(),
        key or admission.api_key_id,
        admission.model_id,
        rev,
        4,
        6,
        *first,
    )


async def test_admission_follows_the_revision_key_set(plane, http, admin):  # noqa: F811
    peer, _, org, model, dep, admission, raw, cid = plane
    client, root, _, _ = http
    await release(http, cid, "first")
    await peer.refresh()
    rev = int(peer.snapshot["revision"])
    assert set(
        admin.execute(
            "SELECT kind FROM publication.snapshot_index WHERE cluster_id=%s AND revision=%s",
            (cid, rev),
        ).fetchall()
    ) == {("route",), ("deployment",), ("model",), ("access",)}
    # A key issued after the release is outside the revision until the next one names it.
    issued = (await issue_key(client, root, org, str(admission.service_account_id))).json()
    fresh = UUID(issued["key"]["id"])
    with pytest.raises(psycopg.errors.RaiseException, match="key_outside_cluster"):
        await peer.call("reserve_request", *reserve_call(admission, rev, key=fresh))
    sequence = int(peer.snapshot["key_set"]["sequence"])
    newer = await release(http, cid, "with-new-key")
    await peer.refresh()
    assert int(peer.snapshot["key_set"]["sequence"]) == sequence + 1
    assert hashlib.sha256(issued["secret"].encode()).hexdigest() in peer.keys
    result, lease = await peer.reserve(issued["secret"], model["id"], 4, 6)
    assert result["state"] == "reserved" and int(result["config_revision"]) == newer
    assert await peer.limits.finish(lease, 0)
    await peer.call("cancel_unstarted_request", UUID(result["request_id"]), "test")
    # Revocation is refused live at once, and the next release removes the key.
    revoked = await client.post(
        f"/api/v1/organizations/{org['id']}/api-keys/{fresh}/revoke", headers=auth(root, 1)
    )
    assert revoked.status_code == 200, revoked.text
    with pytest.raises(psycopg.errors.RaiseException, match="key_inactive"):
        await peer.call("reserve_request", *reserve_call(admission, newer, key=fresh))
    latest = await release(http, cid, "without-revoked-key")
    await peer.refresh()
    assert str(fresh) not in {k["id"] for k in peer.keys.values()}
    with pytest.raises(psycopg.errors.RaiseException, match="key_outside_cluster"):
        await peer.call("reserve_request", *reserve_call(admission, latest, key=fresh))
    # An expired key is refused by the live check before any release drops it.
    admin.execute(
        "UPDATE identity.api_keys SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
        (admission.api_key_id,),
    )
    with pytest.raises(psycopg.errors.RaiseException, match="key_inactive"):
        await peer.call("reserve_request", *reserve_call(admission, latest))
    # A node rebuilt from sequence 0 holds exactly the incremental node's keys.
    fresh_peer = dict(peer.keys)
    peer.keys, peer.key_sequence, peer.snapshot = {}, None, None
    await peer.refresh()
    assert peer.keys == fresh_peer


async def test_a_key_of_another_cluster_is_outside(plane, http, admin):  # noqa: F811
    peer, _, org, model, dep, admission, raw, cid = plane
    client, root, app, settings = http
    other = await create_org(client, root, "Other tenant")
    await app.state.process_billing()
    route = await client.put(
        f"/api/v1/route-policies/{other['id']}/{model['id']}",
        headers=auth(root),
        json={"candidates": [{"deployment_id": dep["id"]}]},
    )
    assert route.status_code == 201, route.text
    response = await client.post(
        "/api/v1/gateway-clusters",
        headers=auth(root),
        json={
            "name": "west",
            "region": "test",
            "organization_ids": [other["id"]],
            "deployment_ids": [dep["id"]],
            "public_key": base64.b64encode(bytes(PrivateKey.generate().public_key)).decode(),
            "auto_publish": False,
        },
    )
    assert response.status_code == 201, response.text
    west = response.json()["id"]
    dsn = await register_node(client, root, admin, west)
    rev = await release(http, west, "west")
    await release(http, cid, "east")
    async with GatewaySimulator.connect(
        dsn, settings.redis_url.get_secret_value(), settings.redis_namespace
    ) as node:
        await node.refresh()
        assert node.keys == {}
        with pytest.raises(psycopg.errors.RaiseException, match="key_outside_cluster"):
            await node.call("reserve_request", *reserve_call(admission, rev))
        with pytest.raises(psycopg.errors.RaiseException, match="key_inactive"):
            await node.connection.execute(
                "SELECT gateway_v1.preflight(%s,%s,NULL,NULL)", (admission.api_key_id, rev)
            )


async def test_first_attempt_commits_with_the_reservation(plane, http, admin):  # noqa: F811
    peer, _, org, model, dep, admission, raw, cid = plane
    rev = await release(http, cid, "first")
    await peer.refresh()
    dep_id = UUID(dep["id"])
    before = account_amounts(admin, org)
    # An attempt on a deployment the database rejects rolls the reservation back with it.
    rid = uuid4()
    with pytest.raises(psycopg.errors.RaiseException, match="deployment_unavailable"):
        await peer.call(
            "reserve_request", *reserve_call(admission, rev, rid=rid, first=(uuid4(), uuid4()))
        )
    assert await peer.call("get_request_status", rid) is None
    assert account_amounts(admin, org) == before
    with pytest.raises(psycopg.errors.RaiseException, match="invalid_attempt"):
        await peer.call("reserve_request", *reserve_call(admission, rev, first=(uuid4(), None)))
    # One call reserves and starts attempt 1; a replay returns both without a second hold.
    rid, aid = uuid4(), uuid4()
    call = reserve_call(admission, rev, rid=rid, first=(aid, dep_id))
    first = await peer.call("reserve_request", *call)
    assert first["state"] == "running" and first["replayed"] is False
    assert first["attempt"]["attempt_id"] == str(aid) and first["attempt"]["replayed"] is False
    assert [a["id"] for a in first["attempts"]] == [str(aid)]
    held = account_amounts(admin, org)
    again = await peer.call("reserve_request", *call)
    assert again["replayed"] is True and again["attempt"]["replayed"] is True
    assert account_amounts(admin, org) == held
    assert (await peer.call("start_attempt", rid, aid, 1, dep_id))["replayed"] is True
    # A replay must repeat the same attempt.
    with pytest.raises(psycopg.errors.RaiseException, match="invalid_attempt_sequence"):
        await peer.call(
            "reserve_request", *reserve_call(admission, rev, rid=rid, first=(uuid4(), dep_id))
        )
    with pytest.raises(psycopg.errors.RaiseException, match="request_conflict"):
        await peer.call(
            "reserve_request", rid, admission.api_key_id, admission.model_id, rev, 5, 6, aid, dep_id
        )
    # A started request cannot be cancelled; failover attempts continue at sequence 2.
    with pytest.raises(psycopg.errors.RaiseException, match="cannot_cancel"):
        await peer.call("cancel_unstarted_request", rid, "test")
    second = await peer.call("start_attempt", rid, uuid4(), 2, dep_id)
    assert second["replayed"] is False
    # A reservation committed without its attempt is completed by the replay that names it.
    lone = uuid4()
    await peer.call("reserve_request", *reserve_call(admission, rev, rid=lone))
    finished = await peer.call(
        "reserve_request", *reserve_call(admission, rev, rid=lone, first=(aid := uuid4(), dep_id))
    )
    assert finished["replayed"] is True and finished["attempt"]["attempt_id"] == str(aid)
    assert admin.execute(
        "SELECT count(*) FROM billing.request_attempts WHERE request_id=%s", (lone,)
    ).fetchone() == (1,)


async def test_the_simulator_reserves_with_its_first_attempt(plane, http, admin):  # noqa: F811
    import asyncio

    peer, _, org, model, dep, admission, raw, cid = plane
    await release(http, cid, "first")
    simulated = asyncio.create_task(peer.simulate(raw, model["id"], 2, 3))
    async with asyncio.timeout(30):
        while not simulated.done():
            await http[2].state.usage.process()
            await asyncio.sleep(0.1)
    status = await simulated
    assert status["state"] == "settled", status
    assert admin.execute(
        "SELECT count(*), min(sequence) FROM billing.request_attempts"
    ).fetchone() == (1, 1)
