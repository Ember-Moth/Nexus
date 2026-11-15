import asyncio
import json
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from test_billing import headers, publish_price
from test_catalog import make_deployment, make_model
from test_identity_api import auth
from test_project_credentials import key as issue_key
from test_relay import relay_binary, running_relay, tls  # noqa: F401
from test_relay_streams import stream_plane

pytestmark = pytest.mark.integration
ROOT = "/v1/messages/batches"


async def publish_second_model(http, admin, plane):
    api, root, app, _ = http
    _, _, org, first_model, first_dep, _, _, cluster = plane
    model = await make_model(api, root, "batch-second-model", ["anthropic"])
    dep = await make_deployment(
        api, root, model, {"id": first_dep["upstream_id"]}, {"id": first_dep["credential_id"]}
    )
    await publish_price(api, root, "sale", model, "2")
    await publish_price(api, root, "cost", dep, "0.8")
    profile = (await api.get(f"/api/v1/deployments/{first_dep['id']}", headers=auth(root))).json()[
        "relay_profile"
    ]
    res = await api.patch(
        f"/api/v1/deployments/{dep['id']}",
        headers=auth(root, dep["version"]),
        json={"relay_profile": profile},
    )
    assert res.status_code == 200, res.text
    res = await api.put(
        f"/api/v1/route-policies/{org['id']}/{model['id']}",
        headers=auth(root),
        json={"candidates": [{"deployment_id": dep["id"]}]},
    )
    assert res.status_code == 201, res.text
    policy, version = admin.execute(
        "SELECT id,version FROM identity.access_policies WHERE layer='platform'"
    ).fetchone()
    res = await api.patch(
        f"/api/v1/organizations/{org['id']}/access-policies/{policy}",
        headers=auth(root, version),
        json={"allowed_models": [first_model["id"], model["id"]]},
    )
    assert res.status_code == 200, res.text
    version = admin.execute(
        "SELECT version FROM publication.gateway_clusters WHERE id=%s", (cluster,)
    ).fetchone()[0]
    res = await api.patch(
        f"/api/v1/gateway-clusters/{cluster}",
        headers=auth(root, version),
        json={"deployment_ids": [first_dep["id"], dep["id"]]},
    )
    assert res.status_code == 200, res.text
    res = await api.post(
        f"/api/v1/gateway-clusters/{cluster}/releases",
        headers=headers(root, "batch-models"),
        json={},
    )
    assert res.status_code == 202, res.text
    while await app.state.publication.distribute():
        pass
    return model["public_name"]


async def test_batch_restart_cancel_isolation_and_item_settlement(http, admin, tls, relay_binary):  # noqa: F811
    pid = "msgbatch_provider"
    canceled = asyncio.Event()
    submitted = []
    results = [
        {"custom_id": "second", "result": {"type": "canceled"}},
        {
            "custom_id": "first",
            "result": {
                "type": "succeeded",
                "message": {
                    "id": "msg_done",
                    "type": "message",
                    "content": [{"type": "text", "text": "output"}],
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            },
        },
    ]

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = head[0].split()
            hdr = {k.lower(): v.strip() for row in head[1:] if row for k, v in [row.split(":", 1)]}
            body = await reader.readexactly(int(hdr.get("content-length", 0)))
            assert hdr["anthropic-version"] == "2023-06-01"
            if path == ROOT:
                assert method == "POST"
                submitted.append(json.loads(body))
            else:
                assert path.startswith(f"{ROOT}/{pid}")
            if path.endswith("/cancel"):
                canceled.set()
            done = canceled.is_set()
            mime = "application/json"
            if path.endswith("/results"):
                assert done
                payload = b"\n".join(json.dumps(r).encode() for r in results)
                mime = "application/x-jsonl"
            elif method == "DELETE":
                payload = json.dumps({"id": pid, "type": "message_batch_deleted"}).encode()
            else:
                payload = json.dumps(
                    {
                        "id": pid,
                        "type": "message_batch",
                        "processing_status": "ended" if done else "in_progress",
                        "created_at": "2026-11-14T01:00:00Z",
                        "request_counts": {
                            "processing": 0 if done else 2,
                            "canceled": int(done),
                            "succeeded": int(done),
                            "expired": 0,
                            "errored": 0,
                        },
                        "results_url": "https://provider.invalid/results" if done else None,
                    }
                ).encode()
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\n"
                    f"Content-Type: {mime}\r\nConnection: close\r\n\r\n"
                ).encode()
                + payload
            )
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(provider, "127.0.0.1", 0, ssl=tls[0])
    async with (
        server,
        stream_plane(
            http,
            admin,
            relay_binary,
            tls[1],
            server.sockets[0].getsockname()[1],
            "anthropic",
            features=["batches"],
        ) as (client, model, key, _, plane),
    ):
        h = {"x-api-key": key}
        params = {"model": model, "max_tokens": 4, "messages": [{"role": "user", "content": "hi"}]}
        item = {"custom_id": "first", "params": params}
        for bad in [None, 1, {}, {"requests": [item, item]}]:
            invalid = await client.post(ROOT, content=json.dumps(bad), headers=h)
            assert invalid.status_code == 400, invalid.text
        over_budget = await client.post(
            ROOT, json={"requests": [item | {"custom_id": str(i)} for i in range(26)]}, headers=h
        )
        assert over_budget.status_code == 402, over_budget.text
        assert admin.execute("SELECT count(*) FROM billing.request_runs").fetchone() == (0,)
        assert admin.execute("SELECT count(*) FROM publication.message_batches").fetchone() == (0,)
        assert admin.execute("SELECT held_amount FROM billing.accounts").fetchone() == (0,)
        assert submitted == []
        second = await publish_second_model(http, admin, plane)
        async with asyncio.timeout(10):
            while True:
                visible = await client.get("/v1/models", headers={"Authorization": "Bearer " + key})
                if visible.status_code == 200 and len(visible.json()["data"]) == 2:
                    break
                assert visible.status_code in {200, 503}, visible.text
                await asyncio.sleep(0.05)
        response = await client.post(
            ROOT,
            json={
                "requests": [
                    item | {"params": params | {"model": second}},
                    item | {"custom_id": "second"},
                ]
            },
            headers=h,
        )
        assert response.status_code == 200, response.text
        handle = response.json()["id"]
        path = f"{ROOT}/{handle}"
        assert len(submitted) == 1
        assert submitted[0]["requests"][0]["params"]["model"] == plane[4]["upstream_model"]
        assert admin.execute("SELECT count(*) FROM billing.usage_events").fetchone() == (0,)
        frozen = admin.execute(
            "SELECT r.id,r.api_key_id,r.model_id,r.config_revision,"
            "r.input_limit,r.output_limit,a.deployment_id "
            "FROM billing.request_runs r JOIN billing.request_attempts a "
            "ON a.request_id=r.id LIMIT 1"
        ).fetchone()
        with pytest.raises(psycopg.errors.RaiseException, match="request_conflict"):
            await plane[0].call("reserve_request", *frozen[:6])
        with pytest.raises(psycopg.errors.RaiseException, match="invalid_attempt_sequence"):
            await plane[0].call("start_attempt", frozen[0], uuid4(), 2, frozen[6])
        assert (await client.delete(path, headers=h)).status_code == 409
        assert (await client.get(ROOT, headers=h)).json()["data"] == [response.json()]
        foreign = (
            await issue_key(
                http[0], http[1], plane[2], str(plane[5].service_account_id), name="batch-reader"
            )
        ).json()["secret"]
        release = await http[0].post(
            f"/api/v1/gateway-clusters/{plane[7]}/releases",
            headers=headers(http[1], "batch-revision"),
            json={},
        )
        assert release.status_code == 202, release.text
        first = client.relay_process
        first.terminate()
        await asyncio.wait_for(first.wait(), 15)
        peer, private, _, _, _, _, _, cluster = plane
        async with running_relay(
            relay_binary, peer, private, cluster, http[3], tls[1], peer.connection.info.port
        ) as replacement:
            other = {"x-api-key": foreign}
            assert (await replacement.get(ROOT, headers=other)).json()["data"] == []
            for method, suffix in [
                ("GET", ""),
                ("GET", "/results"),
                ("POST", "/cancel"),
                ("DELETE", ""),
            ]:
                denied = await replacement.request(method, path + suffix, headers=other)
                assert denied.status_code == 404, denied.text
            reply = await replacement.post(path + "/cancel", headers=h)
            assert reply.status_code == 200, reply.text
            assert reply.json()["results_url"] == path + "/results"
            rows = await replacement.get(reply.json()["results_url"], headers=h)
            assert [json.loads(line) for line in rows.text.splitlines()] == results
            async with asyncio.timeout(25):
                while admin.execute("SELECT count(*) FROM billing.usage_events").fetchone() != (4,):  # noqa: ASYNC110
                    await asyncio.sleep(0.1)
            assert len(submitted) == 1
            assert await http[2].state.usage.process() == 2
            assert admin.execute(
                "SELECT charged_amount,reservation_state FROM billing.request_runs "
                "ORDER BY charged_amount"
            ).fetchall() == [(0, "released"), (Decimal("3"), "captured")]
            assert admin.execute(
                "SELECT cost_amount FROM billing.request_attempts ORDER BY cost_amount"
            ).fetchall() == [(0,), (Decimal("1.2"),)]
            assert (await replacement.delete(path, headers=h)).json()["id"] == handle
            assert (await replacement.get(path, headers=h)).status_code == 404
            assert (await replacement.get(ROOT, headers=h)).json()["data"] == []


async def test_batch_lost_submission_receipt(http, admin, tls, relay_binary):  # noqa: F811
    posts = 0

    async def disconnect(reader, writer):
        nonlocal posts
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            hdr = {k.lower(): v.strip() for row in head[1:] if row for k, v in [row.split(":", 1)]}
            assert head[0].startswith("POST " + ROOT)
            await reader.readexactly(int(hdr["content-length"]))
            posts += 1
        finally:
            writer.close()

    server = await asyncio.start_server(disconnect, "127.0.0.1", 0, ssl=tls[0])
    async with (
        server,
        stream_plane(
            http,
            admin,
            relay_binary,
            tls[1],
            server.sockets[0].getsockname()[1],
            "anthropic",
            features=["batches"],
        ) as (client, model, key, _, _),
    ):
        h = {"x-api-key": key}
        body = {
            "requests": [
                {
                    "custom_id": "unconfirmed",
                    "params": {
                        "model": model,
                        "max_tokens": 4,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                }
            ]
        }
        response = await client.post(ROOT, headers=h, json=body)
        assert response.status_code == 502
        assert (await client.get(ROOT, headers=h)).json()["data"] == []
        admin.execute("UPDATE publication.message_batches SET next_poll=clock_timestamp()")
        async with asyncio.timeout(8):
            while not admin.execute("SELECT done FROM publication.message_batches").fetchone()[0]:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
        assert posts == 1
        assert await http[2].state.usage.process() == 1
        assert admin.execute(
            "SELECT charged_amount, state, review_reason FROM billing.request_runs"
        ).fetchone() == (None, "review", "unknown_outcome")

        # Human-reviewed supplements use the same frozen batch rates as automatic settlement.
        api, root, _, _ = http
        aid = str(admin.execute("SELECT id FROM billing.request_attempts").fetchone()[0])
        for kind, field, expected in [
            ("attempt", "cost_amount", "0.600000000000"),
            ("request", "charged_amount", "1.500000000000"),
        ]:
            cases = (await api.get("/api/v1/reconciliation-cases", headers=auth(root))).json()[
                "items"
            ]
            case = next(c for c in cases if c["source_type"] == kind)
            result = await api.post(
                f"/api/v1/reconciliation-cases/{case['id']}/resolve",
                headers=headers(root, "batch-supplement-" + kind, case["version"]),
                json={
                    "action": "supplement_usage",
                    "attempt_id": aid,
                    "reason": "Verified provider usage",
                    "evidence": "isolated batch receipt",
                    "normalization_version": "review.v1",
                    "quantities": {"input_uncached_tokens": 2, "output_tokens": 1},
                },
            )
            assert result.status_code == 200, result.text
            assert result.json()[field] == expected
