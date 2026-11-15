"""Opt-in measurement of full snapshots as the number of key verifiers grows.

    SNAPSHOT_SCALE_KEYS=1000,10000,100000 SNAPSHOT_SCALE_RELAY=release \\
        uv run python scripts/run_integration.py -q -s tests/integration/test_snapshot_scale.py

SNAPSHOT_SCALE_REPORT names a JSON file for the results. The figures are recorded in
docs/control-plane-acceptance.md; they describe one machine, not a capacity guarantee.
"""

import asyncio
import json
import os
import subprocess
from pathlib import Path
from time import monotonic

import pytest
from test_billing import headers
from test_identity_api import auth
from test_plane import provision_plane
from test_relay import CRATE, running_relay, tls  # noqa: F401 - pytest fixture

from nexus.modules.publication.application.automation import AutoPublisher

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("SNAPSHOT_SCALE_KEYS"), reason="set SNAPSHOT_SCALE_KEYS to measure"
    ),
]


async def instant(seconds):
    return False


def timed(started):
    return round(monotonic() - started, 3)


async def wait_applied(admin, node, rev, seconds=120):
    async with asyncio.timeout(seconds):
        while True:
            row = admin.execute(
                "SELECT applied_revision FROM publication.gateway_principals WHERE node_id=%s",
                (node,),
            ).fetchone()
            if row[0] >= rev:
                return
            await asyncio.sleep(0.01)  # noqa: ASYNC110 - external acknowledgement


def add_keys(admin, admission, count):
    admin.execute(
        """INSERT INTO identity.api_keys(id,organization_id,project_id,service_account_id,name,
        digest,prefix) SELECT gen_random_uuid(),%s,%s,%s,'scale',
        encode(sha256(convert_to(gen_random_uuid()::text,'UTF8')),'hex'),'nxk_scale'
        FROM generate_series(1,%s)""",
        (admission.organization_id, admission.project_id, admission.service_account_id, count),
    )


@pytest.fixture(scope="session")
def relay():
    profile = os.environ.get("SNAPSHOT_SCALE_RELAY", "release")
    subprocess.run(
        ["cargo", "build", "--locked", "--bin", "nexus-relay"]
        + (["--release"] if profile == "release" else []),
        cwd=CRATE,
        check=True,
    )
    return CRATE / "target" / profile / "nexus-relay"


async def test_snapshot_scale(http, admin, tls, relay):  # noqa: F811 - pytest fixture
    client, root, app, settings = http
    sizes = [int(n) for n in os.environ["SNAPSHOT_SCALE_KEYS"].split(",")]
    results = []
    async with provision_plane(http, admin, auto_publish=False) as values:
        peer, private, org, model, dep, admission, raw, cid = values
        service = app.state.publication
        admin.execute("UPDATE publication.cluster_policy_defaults SET auto_publish_quiet_seconds=0")
        auto = AutoPublisher(service, service.tx, instant, poll=30)
        node = admin.execute(
            "SELECT node_id FROM publication.gateway_principals WHERE cluster_id=%s", (cid,)
        ).fetchone()[0]
        while await service.distribute():
            pass
        port = admin.info.port
        async with running_relay(relay, peer, private, cid, settings, tls[1], port):
            await wait_applied(admin, node, 1)
            present = admin.execute("SELECT count(*) FROM identity.api_keys").fetchone()[0]
            for size in sizes:
                add_keys(admin, admission, size - present)
                present = size
                started = monotonic()
                response = await client.post(
                    f"/api/v1/gateway-clusters/{cid}/releases",
                    headers=headers(root, f"scale-{size}"),
                    json={},
                )
                publish_s = timed(started)
                assert response.status_code == 202, response.text
                rev = int(response.json()["revision"])
                payload, manifest, stored = admin.execute(
                    "SELECT octet_length(payload_bytes),pg_column_size(manifest),"
                    "pg_column_size(manifest)+pg_column_size(payload_bytes) "
                    "FROM publication.config_snapshots WHERE cluster_id=%s AND revision=%s",
                    (cid, rev),
                ).fetchone()
                started = monotonic()
                while await service.distribute():
                    pass
                distribute_s = timed(started)
                await wait_applied(admin, node, rev)
                relay_apply_s = timed(started)
                redis_bytes = await peer.redis.memory_usage(
                    f"{settings.redis_namespace}:cfg:{{{cid}}}:snapshot:{rev}"
                )
                started = monotonic()
                reply = await peer.call("get_config_snapshot")
                fetch_s = timed(started)
                reply_bytes = len(json.dumps(reply))
                started = monotonic()
                await peer.refresh()
                simulator_refresh_s = timed(started)
                # The auto-publisher on the same cluster: a probe with nothing to do, a shared
                # revision bump with unchanged content, and a release after one more key.
                current = (await client.get("/api/v1/gateway-clusters", headers=auth(root))).json()[
                    "items"
                ][0]
                await client.patch(
                    f"/api/v1/gateway-clusters/{cid}",
                    headers=auth(root, current["version"]),
                    json={"auto_publish": True},
                )
                await auto.tick()
                started = monotonic()
                assert await auto.tick() == (0, None)
                probe_s = timed(started)
                admin.execute("UPDATE identity.api_keys SET name=name WHERE false")
                started = monotonic()
                assert (await auto.tick())[0] == 0
                unchanged_s = timed(started)
                add_keys(admin, admission, 1)
                present += 1
                started = monotonic()
                assert (await auto.tick())[0] == 1
                auto_publish_s = timed(started)
                current = (await client.get("/api/v1/gateway-clusters", headers=auth(root))).json()[
                    "items"
                ][0]
                await client.patch(
                    f"/api/v1/gateway-clusters/{cid}",
                    headers=auth(root, current["version"]),
                    json={"auto_publish": False},
                )
                while await service.distribute():
                    pass
                await wait_applied(admin, node, rev + 1)
                results.append(
                    {
                        "keys": size,
                        "publish_s": publish_s,
                        "payload_bytes": payload,
                        "manifest_stored_bytes": manifest,
                        "row_stored_bytes": stored,
                        "redis_bytes": redis_bytes,
                        "snapshot_reply_bytes": reply_bytes,
                        "distribute_s": distribute_s,
                        "relay_fetch_apply_ack_s": relay_apply_s,
                        "node_fetch_s": fetch_s,
                        "simulator_refresh_s": simulator_refresh_s,
                        "auto_probe_s": probe_s,
                        "auto_unchanged_s": unchanged_s,
                        "auto_publish_s": auto_publish_s,
                    }
                )
    print("\n" + json.dumps(results, indent=2))
    if target := os.environ.get("SNAPSHOT_SCALE_REPORT"):
        await asyncio.to_thread(Path(target).write_text, json.dumps(results, indent=2))
