import asyncio
import json
from decimal import Decimal
from uuid import uuid4

import pytest
from test_billing import headers
from test_identity_api import auth
from test_project_credentials import key as issue_key
from test_relay import relay_binary, tls, wait_events  # noqa: F401
from test_relay_streams import stream_plane

pytestmark = pytest.mark.integration
GIB_DAY = 2**30 * 86400
ROOT = "/v1/vector_stores"


async def configure(http, admin, plane, *, web=False):
    api, root, app, _ = http
    _, _, _, model, dep, _, _, cluster = plane
    dims = ["file_search_calls", "vector_storage_byte_seconds"]
    if web:
        dims.append("web_search_requests")
    r = await api.patch(
        f"/api/v1/models/{model['id']}",
        headers=auth(root, model["version"]),
        json={"tool_dimensions": dims},
    )
    assert r.status_code == 200, r.text
    for kind, scope, factor in [("sale", model, 1), ("cost", dep, Decimal("0.4"))]:
        rates = {
            k: {
                "unit_price": str(v * factor),
                "unit_quantity": GIB_DAY if k == "vector_storage_byte_seconds" else 1,
            }
            for k, v in {
                "input_uncached_tokens": 1,
                "output_tokens": 1,
                "file_search_calls": 3,
                "vector_storage_byte_seconds": 86400,
            }.items()
        }
        if web:
            rates["web_search_requests"] = {"unit_price": str(5 * factor), "unit_quantity": 1}
        path = "/api/v1/" + kind + "-price-versions"
        r = await api.post(
            path,
            headers=headers(root, uuid4().hex),
            json={
                "currency": "USD",
                "rates": rates,
                ("model_id" if kind == "sale" else "deployment_id"): scope["id"],
            },
        )
        assert r.status_code == 201, r.text
        v = r.json()
        r = await api.post(
            f"{path}/{v['id']}/publish", headers=headers(root, uuid4().hex, v["version"]), json={}
        )
        assert r.status_code == 200, r.text
    path = f"/api/v1/deployments/{dep['id']}"
    current = (await api.get(path, headers=auth(root))).json()
    r = await api.patch(
        path,
        headers=auth(root, current["version"]),
        json={
            "relay_profile": current["relay_profile"]
            | {"file_search_ceiling": 2, "web_search_ceiling": 3 if web else 0}
        },
    )
    assert r.status_code == 200, r.text
    node, role, caps = admin.execute(
        "SELECT node_id,database_role,capabilities FROM publication.gateway_principals "
        "WHERE cluster_id=%s",
        (cluster,),
    ).fetchone()
    r = await api.put(
        f"/api/v1/gateway-clusters/{cluster}/principals/{node}",
        headers=auth(root),
        json={
            "node_id": str(node),
            "database_role": role,
            "capabilities": [
                *caps,
                "relay.file-search.v1",
                *(["relay.web-search.v1"] if web else []),
            ],
        },
    )
    assert r.status_code == 200, r.text
    r = await api.post(
        f"/api/v1/gateway-clusters/{cluster}/releases", headers=headers(root, "vector"), json={}
    )
    assert r.status_code == 202, r.text
    while await app.state.publication.distribute():
        pass
    async with asyncio.timeout(10):
        while admin.execute(  # noqa: ASYNC110 - waiting for the published revision
            "SELECT applied_revision FROM publication.gateway_principals WHERE node_id=%s", (node,)
        ).fetchone()[0] != int(r.json()["revision"]):
            await asyncio.sleep(0.05)


async def test_native_vector_store_search_and_storage(http, admin, tls, relay_binary):  # noqa: F811
    pid = "vs_provider"
    fid = "file-provider"
    bid = "vsfb_provider"
    seen = []
    store = {
        "id": pid,
        "object": "vector_store",
        "created_at": 1700000000,
        "name": "manuals",
        "usage_bytes": 2**30,
        "status": "completed",
        "file_counts": {"in_progress": 0, "completed": 1, "failed": 0, "cancelled": 0, "total": 1},
        "metadata": {},
    }
    attached = {
        "object": "vector_store.file",
        "id": fid,
        "vector_store_id": pid,
        "status": "completed",
    }
    batch = {
        "object": "vector_store.files_batch",
        "id": bid,
        "vector_store_id": pid,
        "status": "completed",
    }

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = head[0].split()
            hdr = {k.lower(): v.strip() for row in head[1:] if row for k, v in [row.split(":", 1)]}
            raw = await reader.readexactly(int(hdr.get("content-length", 0)))
            data = json.loads(raw) if raw and path != "/v1/files" else {}
            seen.append((method, path))
            binary = None
            if path == "/v1/files":
                value = {
                    "id": fid,
                    "object": "file",
                    "filename": "manual.txt",
                    "bytes": 4,
                    "created_at": 1700000000,
                    "purpose": "assistants",
                }
            elif path == ROOT:
                assert data["file_ids"] == [fid]
                value = store
            elif path == f"{ROOT}/{pid}":
                value = (
                    {"object": "vector_store.deleted", "id": pid, "deleted": True}
                    if method == "DELETE"
                    else store
                )
            elif path == f"{ROOT}/{pid}/files":
                if method == "POST":
                    assert data["file_id"] == fid
                value = (
                    attached
                    if method == "POST"
                    else {"object": "list", "data": [attached], "first_id": fid, "last_id": fid}
                )
            elif path == f"{ROOT}/{pid}/file_batches":
                assert data["files"][0]["file_id"] == fid
                value = batch
            elif path == f"{ROOT}/{pid}/file_batches/{bid}":
                value = batch
            elif path == f"{ROOT}/{pid}/search":
                value = {
                    "object": "vector_store.search_results.page",
                    "data": [
                        {
                            "file_id": fid,
                            "score": 1,
                            "content": [{"type": "text", "text": "my manual"}],
                        }
                    ],
                }
            else:
                assert path == "/v1/responses" and data["max_tool_calls"] == 2
                assert data["tools"][0]["vector_store_ids"] == [pid]
                call = {
                    "type": "file_search_call",
                    "id": "fs_one",
                    "status": "completed",
                    "queries": ["first", "second"],
                    "results": [{"file_id": fid, "text": "my manual"}],
                }
                citation = {
                    "type": "file_citation",
                    "file_id": fid,
                    "filename": "manual.txt",
                    "index": 0,
                }
                value = {
                    "object": "response",
                    "id": "resp_lookup",
                    "status": "completed",
                    "tools": data["tools"],
                    "output": [
                        call,
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "found", "annotations": [citation]}
                            ],
                        },
                    ],
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 0},
                    },
                }
                if len(data["tools"]) == 2:
                    value["output"].append(
                        {
                            "type": "web_search_call",
                            "id": "ws_one",
                            "status": "completed",
                            "action": {"type": "search"},
                        }
                    )
                if data.get("stream"):
                    events = [
                        {
                            "type": "response.created",
                            "response": {
                                "id": "resp_lookup",
                                "status": "in_progress",
                                "tools": data["tools"],
                            },
                        },
                        {"type": "response.file_search_call.searching", "item_id": "fs_one"},
                        {"type": "response.output_item.done", "item": call},
                        {"type": "response.output_text.annotation.added", "annotation": citation},
                        {"type": "response.completed", "response": value},
                    ]
                    binary = "".join(
                        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
                    ).encode()
            mime = "text/event-stream" if binary else "application/json"
            payload = binary or json.dumps(value).encode()
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\n"
                    f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
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
            "openai_compatible",
            features=["vector_stores", "files", "web_search"],
        ) as (relay, model, key, _, plane),
    ):
        await configure(http, admin, plane, web=True)
        h = {"Authorization": "Bearer " + key}
        f = await relay.post(
            "/v1/files",
            headers=h,
            data={"purpose": "assistants"},
            files={"file": ("manual.txt", b"test", "text/plain")},
        )
        assert f.status_code == 200, f.text
        local_file = f.json()["id"]
        store_reply = await relay.post(
            ROOT, headers=h, json={"name": "manuals", "file_ids": [local_file]}
        )
        assert store_reply.status_code == 200, store_reply.text
        vs = store_reply.json()["id"]
        assert vs.startswith("vs_nx_")
        path = ROOT + "/" + vs
        assert (await relay.get(ROOT, headers=h)).json()["data"] == [store_reply.json()]
        assert (await relay.get(path, headers=h)).json()["id"] == vs
        f = await relay.post(
            path + "/files", headers=h, json={"file_id": local_file, "attributes": {"tag": "docs"}}
        )
        assert f.json()["id"] == local_file and f.json()["vector_store_id"] == vs
        page = await relay.get(path + "/files", headers=h)
        assert page.json()["first_id"] == page.json()["data"][0]["id"] == local_file
        batch_reply = await relay.post(
            path + "/file_batches", headers=h, json={"files": [{"file_id": local_file}]}
        )
        assert batch_reply.status_code == 200, batch_reply.text
        b = batch_reply.json()["id"]
        assert (
            b.startswith("vsfb_nx_")
            and (await relay.get(path + "/file_batches/" + b, headers=h)).json()["id"] == b
        )
        direct = await relay.post(path + "/search", headers=h, json={"query": "manual"})
        assert direct.json()["data"][0]["file_id"] == local_file
        body = {
            "model": model,
            "input": "lookup",
            "tools": [{"type": "file_search", "vector_store_ids": [vs]}],
            "include": ["file_search_call.results"],
        }
        for i, stream in enumerate([False, True]):
            r = await relay.post("/v1/responses", headers=h, json=body | {"stream": stream})
            assert r.status_code == 200, r.text
            assert pid not in r.text and fid not in r.text
            assert local_file in r.text and vs in r.text
            await wait_events(admin, 2 * i + 2)
            assert await http[2].state.usage.process() == 1
        assert (
            admin.execute(
                "SELECT reserved_amount,charged_amount,file_search_limit FROM billing.request_runs"
            ).fetchall()
            == [(24, 6, 2)] * 2
        )
        both = await relay.post(
            "/v1/responses",
            headers=h,
            json=body | {"tools": body["tools"] + [{"type": "web_search"}]},
        )
        assert both.status_code == 200, both.text
        await wait_events(admin, 6)
        assert await http[2].state.usage.process() == 1
        # All built-in tools share max_tool_calls. Two calls at the more expensive
        # search rate cover every possible combination of these two tools.
        assert admin.execute(
            "SELECT reserved_amount,charged_amount FROM billing.request_runs "
            "WHERE web_search_limit=2"
        ).fetchone() == (28, 11)
        store["usage_bytes"] = 2**29
        admin.execute("UPDATE publication.vector_stores SET next_poll=clock_timestamp()")
        async with asyncio.timeout(8):
            while admin.execute(  # noqa: ASYNC110 - polling the provider meter
                "SELECT usage_bytes FROM billing.vector_storage_contracts"
            ).fetchone() != (2**29,):
                await asyncio.sleep(0.05)
        store["usage_bytes"] = 2**30
        assert (await relay.get(path, headers=h)).json()["usage_bytes"] == 2**30
        api, token, app, _ = http
        foreign = (
            await issue_key(
                api, token, plane[2], str(plane[5].service_account_id), name="second-vector-reader"
            )
        ).json()["secret"]
        r = await api.post(
            f"/api/v1/gateway-clusters/{plane[7]}/releases",
            headers=headers(token, "vector-key"),
            json={},
        )
        assert r.status_code == 202, r.text
        other = {"Authorization": "Bearer " + foreign}
        async with asyncio.timeout(10):
            while (await relay.get("/v1/models", headers=other)).status_code != 200:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
        n = len(seen)
        assert (await relay.get(ROOT, headers=other)).json()["data"] == []
        assert (await relay.get(path, headers=other)).status_code == 404
        assert (await relay.post("/v1/responses", headers=other, json=body)).status_code == 404
        assert len(seen) == n
        # Let the final measured size cover a whole second, keeping already
        # recorded transition boundaries intact.
        wait_seconds = admin.execute(
            "SELECT greatest(0,extract(epoch FROM observed_at+interval '1 second'"
            "-clock_timestamp()))::float8 FROM billing.vector_storage_contracts"
        ).fetchone()[0]
        await asyncio.sleep(wait_seconds + 0.05)
        deleted = await relay.delete(path, headers=h)
        assert deleted.status_code == 200 and deleted.json()["id"] == vs, deleted.text
        assert (await relay.get(path, headers=h)).status_code == 404
        assert await app.state.usage.stage_storage() == 1
        assert await app.state.usage.stage_storage() == 0
        assert await app.state.usage.process() == 1
        amount, units = admin.execute(
            "SELECT charged_amount,storage_byte_seconds FROM billing.request_runs "
            "WHERE billing_policy_version='openai_vector_storage_v1'"
        ).fetchone()
        assert units >= 2**30 and amount == units / 2**30
        usage_report = await api.get(
            f"/api/v1/organizations/{plane[2]['id']}/vector-storage-usage",
            headers=auth(token),
            params={"project_id": str(plane[5].project_id), "limit": 1},
        )
        assert usage_report.status_code == 200, usage_report.text
        day = usage_report.json()["items"][0]
        assert day["vector_store_id"] == vs and day["state"] == "settled" and not day["uncertain"]
        assert (
            day["observed_byte_seconds"] == str(units) and Decimal(day["charged_amount"]) == amount
        )

        assert admin.execute(
            "SELECT cost_amount FROM billing.request_attempts a JOIN billing.request_runs r "
            "ON r.id=a.request_id WHERE r.billing_policy_version='openai_vector_storage_v1'"
        ).fetchone() == (amount * Decimal("0.4"),)


@pytest.mark.parametrize(
    "scenario", ["rollover", "coverage_gap", "insufficient_funds", "submission_unknown"]
)
async def test_storage_worker_recovery(http, admin, tls, relay_binary, scenario):  # noqa: F811
    from datetime import UTC, datetime, timedelta

    import psycopg

    # Meter contract tests drive the same SQL receipts emitted by a relay after its
    # HTTP read. The isolated database clock lets them cover UTC rollover without a timed wait.
    async with stream_plane(
        http,
        admin,
        relay_binary,
        tls[1],
        1,
        "openai_compatible",
        features=["vector_stores", "files", "web_search"],
    ) as (
        _,
        _,
        _,
        _,
        plane,
    ):
        await configure(http, admin, plane)
        peer, _, _, model, dep, admission, _, cluster = plane
        rev = admin.execute(
            "SELECT desired_revision FROM publication.gateway_clusters WHERE id=%s", (cluster,)
        ).fetchone()[0]
        handle = uuid4()
        kid = admission.api_key_id
        stamp = (
            await (
                await peer.connection.execute(
                    "SELECT gateway_v1.vector_prepare(%s,%s,%s,%s,%s)",
                    (handle, kid, rev, model["id"], dep["id"]),
                )
            ).fetchone()
        )[0]
        usage = http[2].state.usage
        midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=1
        )
        gib = 2**30
        if scenario == "submission_unknown":
            await peer.connection.execute(
                "SELECT gateway_v1.vector_observe(%s,%s,%s,NULL,%s,NULL,'unknown',true)",
                (handle, kid, rev, stamp),
            )
            assert await usage.stage_storage() == 1
            assert await usage.process() == 1
            assert admin.execute(
                "SELECT state,review_reason FROM billing.request_runs"
            ).fetchone() == ("review", "unknown_outcome")
            return
        # File Search can outlive the day on which the resource was created.
        start = midnight - timedelta(seconds=2)
        size = 100 * gib if scenario == "insufficient_funds" else gib
        admin.execute(
            "SELECT billing.v1_storage_sample(%s,%s,%s,NULL,false,false)", (handle, start, size)
        )
        if scenario == "rollover":
            end = midnight + timedelta(seconds=3)
            admin.execute(
                "SELECT billing.v1_storage_sample(%s,%s,0,NULL,true,false)", (handle, end)
            )
            staged = await asyncio.gather(usage.stage_storage(), usage.stage_storage())
            assert sum(staged) == 2 and await usage.stage_storage() == 0
            assert await usage.process() == 2
            assert admin.execute(
                "SELECT charged_amount FROM billing.request_runs ORDER BY created_at"
            ).fetchall() == [(2,), (3,)]
            sql = (
                "SELECT count(*),sum(signed_amount) FROM billing.ledger_entries "
                "WHERE kind='usage_debit'"
            )
            assert admin.execute(sql).fetchone() == (2, -5)
            assert admin.execute(
                "SELECT sum(extract(epoch FROM ended_at-started_at)*usage_bytes) "
                "FROM billing.vector_storage_segments WHERE NOT uncertain"
            ).fetchone() == (5 * gib,)
            assert admin.execute(
                "SELECT sum(spent),sum(reserved) FROM billing.budget_holds"
            ).fetchone() == (5, 0)
        elif scenario == "coverage_gap":
            admin.execute(
                "SELECT billing.v1_storage_sample(%s,%s,0,NULL,true,false)",
                (handle, midnight + timedelta(minutes=6)),
            )
            assert await usage.stage_storage() == 2 and await usage.process() == 2
            assert admin.execute(
                "SELECT count(*) FROM billing.request_runs "
                "WHERE state='review' AND charged_amount IS NULL"
            ).fetchone() == (2,)
            assert admin.execute(
                "SELECT count(*) FROM billing.ledger_entries WHERE kind='usage_debit'"
            ).fetchone() == (0,)
        else:
            admin.execute(
                "SELECT billing.v1_storage_sample(%s,%s,0,NULL,true,false)", (handle, midnight)
            )
            assert await usage.stage_storage() == 1 and await usage.stage_storage() == 0
            assert admin.execute(
                "SELECT state,reason FROM billing.vector_storage_days"
            ).fetchone() == ("review", "insufficient_funds")
            assert admin.execute("SELECT count(*) FROM billing.request_runs").fetchone() == (0,)
            with pytest.raises(psycopg.errors.RaiseException, match="insufficient_funds"):
                await peer.connection.execute(
                    "SELECT gateway_v1.vector_prepare(%s,%s,%s,%s,%s)",
                    (uuid4(), kid, rev, model["id"], dep["id"]),
                )

            from test_billing import funding, post_funding

            api, token, _, _ = http
            org = plane[2]
            receiver = {
                "id": str(
                    admin.execute("SELECT id FROM billing.receiving_accounts LIMIT 1").fetchone()[0]
                )
            }
            credit = await funding(api, token, org, receiver, "200")
            await post_funding(api, token, org, credit)
            retry = "UPDATE billing.vector_storage_days SET next_attempt=clock_timestamp()"
            admin.execute(retry)
            assert await usage.stage_storage() == 1
            assert admin.execute("SELECT reason FROM billing.vector_storage_days").fetchone() == (
                "budget_exceeded",
            )
            assert admin.execute("SELECT count(*) FROM billing.request_runs").fetchone() == (0,)
            policy, version = admin.execute(
                "SELECT id,version FROM billing.budget_policies"
            ).fetchone()
            raised = await api.patch(
                f"/api/v1/organizations/{org['id']}/budget-policies/{policy}",
                headers=auth(token, version),
                json={"limit_amount": "300"},
            )
            assert raised.status_code == 200, raised.text
            admin.execute(retry)
            assert await usage.stage_storage() == 1 and await usage.stage_storage() == 0
            assert await usage.process() == 1
            assert admin.execute(
                "SELECT count(*),sum(signed_amount) FROM billing.ledger_entries "
                "WHERE kind='usage_debit'"
            ).fetchone() == (1, -200)


async def test_file_search_ws_uses_owned_handles_and_native_receipt(http, admin, tls, relay_binary):  # noqa: F811
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve

    file = uuid4()
    store = uuid4()
    pid = "vs_at_ws_provider"
    fid = "file_at_ws_provider"
    seen = []

    async def provider(socket):
        async for wire in socket:
            body = json.loads(wire)
            seen.append(body["tools"][0]["vector_store_ids"])
            result = {
                "type": "file_search_call",
                "id": "fs_ws",
                "status": "completed",
                "queries": ["one", "two"],
                "results": [{"file_id": fid, "text": "hello"}],
            }
            response = {
                "object": "response",
                "id": "resp_ws_file",
                "output": [result],
                "status": "completed",
                "tools": body["tools"],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "input_tokens_details": {"cached_tokens": 0},
                },
            }
            for e in [
                {
                    "type": "response.created",
                    "response": {"id": "resp_ws_file", "status": "in_progress"},
                },
                {"type": "response.output_item.done", "item": result},
                {"type": "response.completed", "response": response},
            ]:
                await socket.send(json.dumps(e))

    async with (
        serve(provider, "127.0.0.1", 0, ssl=tls[0]) as server,
        stream_plane(
            http,
            admin,
            relay_binary,
            tls[1],
            server.sockets[0].getsockname()[1],
            "openai_compatible",
            ws=True,
            features=["vector_stores", "files", "web_search"],
        ) as (relay, model, key, _, plane),
    ):
        await configure(http, admin, plane)
        peer, _, _, mod, dep, admission, _, cluster = plane
        rev = admin.execute(
            "SELECT desired_revision FROM publication.gateway_clusters WHERE id=%s", (cluster,)
        ).fetchone()[0]
        meta = {
            "id": fid,
            "object": "file",
            "filename": "ws.txt",
            "bytes": 1,
            "created_at": 1700000000,
            "purpose": "assistants",
        }
        await peer.connection.execute(
            "SELECT gateway_v1.native_file_save(%s,%s,%s,%s,%s,%s::jsonb,'openai_responses')",
            (file, admission.api_key_id, rev, mod["id"], dep["id"], json.dumps(meta)),
        )
        stamp = (
            await (
                await peer.connection.execute(
                    "SELECT gateway_v1.vector_prepare(%s,%s,%s,%s,%s)",
                    (store, admission.api_key_id, rev, mod["id"], dep["id"]),
                )
            ).fetchone()
        )[0]
        await peer.connection.execute(
            "SELECT gateway_v1.vector_observe(%s,%s,%s,%s,%s,%s::jsonb,'active',false)",
            (
                store,
                admission.api_key_id,
                rev,
                pid,
                stamp,
                json.dumps(
                    {
                        "object": "vector_store",
                        "id": f"vs_nx_{store}",
                        "created_at": 1700000000,
                        "status": "completed",
                        "usage_bytes": 0,
                    }
                ),
            ),
        )
        async with connect(
            f"ws://127.0.0.1:{relay.base_url.port}/v1/responses",
            additional_headers={"Authorization": "Bearer " + key},
            proxy=None,
        ) as socket:
            await socket.send(
                json.dumps(
                    {
                        "type": "response.create",
                        "model": model,
                        "input": "lookup",
                        "tools": [{"type": "file_search", "vector_store_ids": [f"vs_nx_{store}"]}],
                    }
                )
            )
            events = [json.loads(await asyncio.wait_for(socket.recv(), 4)) for _ in range(3)]
        assert seen == [[pid]]
        assert events[-2]["item"]["results"][0]["file_id"] == f"file_nx_{file}"
        assert events[-1]["response"]["tools"][0]["vector_store_ids"] == [f"vs_nx_{store}"]
        await wait_events(admin, 2)
        assert await http[2].state.usage.process() == 1
        assert admin.execute("SELECT charged_amount FROM billing.request_runs").fetchone() == (5,)


async def test_vector_create_recovers_known_locator(http, admin, tls, relay_binary):  # noqa: F811
    calls = []

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = head[0].split()
            hdr = {
                k.lower(): v.strip() for line in head[1:] if line for k, v in [line.split(":", 1)]
            }
            await reader.readexactly(int(hdr.get("content-length", 0)))
            calls.append(method)
            value = {
                "object": "vector_store",
                "id": "vs_receipt",
                "created_at": 1700000000,
                "status": "completed",
                "usage_bytes": None if method == "POST" else 15,
                "file_counts": {"total": 0},
            }
            if method == "DELETE":
                value = {"object": "vector_store.deleted", "id": "vs_receipt", "deleted": True}
            if method != "POST":
                assert path == ROOT + "/vs_receipt"
            payload = json.dumps(value).encode()
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
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
            "openai_compatible",
            features=["vector_stores", "files", "web_search"],
        ) as (relay, _, key, _, plane),
    ):
        await configure(http, admin, plane)
        h = {"Authorization": "Bearer " + key}
        failure = await relay.post(ROOT, headers=h, json={"name": "recover"})
        assert failure.status_code == 502
        async with asyncio.timeout(8):
            while True:
                rows = (await relay.get(ROOT, headers=h)).json()["data"]
                if rows:
                    break
                await asyncio.sleep(0.05)
        assert rows[0]["usage_bytes"] == 15 and calls.count("POST") == 1
        assert (await relay.delete(ROOT + "/" + rows[0]["id"], headers=h)).status_code == 200
        assert await http[2].state.usage.stage_storage() == 1
        assert await http[2].state.usage.process() == 1
        assert admin.execute("SELECT state,review_reason FROM billing.request_runs").fetchone() == (
            "review",
            "unknown_outcome",
        )


async def test_storage_is_prepaid_renewed_and_lapses(
    http,
    admin,
    tls,  # noqa: F811
    relay_binary,  # noqa: F811
):
    """Storage is reserved days ahead of use. A store the wallet cannot keep prepaid stops
    taking writes, and once its prepaid window runs out the lapse policy applies."""
    from datetime import UTC, datetime, timedelta

    import psycopg

    async with stream_plane(http, admin, relay_binary, tls[1], 1, "openai_compatible") as (
        _,
        _,
        _,
        _,
        plane,
    ):
        await configure(http, admin, plane)
        peer, _, org, model, dep, admission, _, cluster = plane
        rev = admin.execute(
            "SELECT desired_revision FROM publication.gateway_clusters WHERE id=%s", (cluster,)
        ).fetchone()[0]
        usage = http[2].state.usage

        async def store():
            handle = uuid4()
            await peer.connection.execute(
                "SELECT gateway_v1.vector_prepare(%s,%s,%s,%s,%s)",
                (handle, admission.api_key_id, rev, model["id"], dep["id"]),
            )
            return handle

        def held():
            return admin.execute(
                "SELECT held_amount FROM billing.accounts WHERE organization_id=%s", (org["id"],)
            ).fetchone()[0]

        def contract(handle):
            return admin.execute(
                "SELECT hold_id,prepaid_until,prepaid_bytes,renewal_failed_at,lapse_action "
                "FROM billing.vector_storage_contracts WHERE id=%s",
                (handle,),
            ).fetchone()

        def sample(handle, at, size, closing=False):
            admin.execute(
                "SELECT billing.v1_storage_sample(%s,%s,%s,NULL,%s,false)",
                (handle, at, size, closing),
            )

        # A store of 100 bytes holds seven days at 125 bytes before any day is settled.
        small = await store()
        midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        sample(small, midnight - timedelta(seconds=2), 100)
        assert await usage.renew_storage() == 1 and await usage.renew_storage() == 0
        hold, until, prepaid, failed, lapse = contract(small)
        now = datetime.now(UTC)
        assert (prepaid, failed, lapse) == (125, None, None)
        assert timedelta(days=7) - timedelta(minutes=1) < until - now <= timedelta(days=7)
        seconds, reserved = admin.execute(
            "SELECT storage_byte_seconds,reserved_amount FROM billing.request_runs WHERE id=%s",
            (hold,),
        ).fetchone()
        assert 125 * (7 * 86400 - 60) <= seconds <= 125 * 7 * 86400
        assert held() == reserved > 0
        # Settling yesterday draws from the prepaid hold: what remains is held again.
        sample(small, midnight + timedelta(seconds=3), 100)
        assert await usage.stage_storage() == 1
        restored, left, *_ = contract(small)
        assert restored != hold and left == until
        day = admin.execute(
            "SELECT reserved_amount FROM billing.request_runs WHERE id=("
            "SELECT id FROM billing.vector_storage_days WHERE store_id=%s AND state='staged')",
            (small,),
        ).fetchone()[0]
        remainder = admin.execute(
            "SELECT reserved_amount FROM billing.request_runs WHERE id=%s", (restored,)
        ).fetchone()[0]
        assert held() == day + remainder and remainder <= reserved
        admin.execute("SELECT billing.v1_storage_write(%s)", (small,))

        # A terabyte cannot be prepaid: the hold stays, writes stop, and its funds cover the
        # observed size only briefly, after which the store lapses under the delete policy.
        large = await store()
        sample(large, datetime.now(UTC) - timedelta(seconds=1), 100)
        assert await usage.renew_storage() == 1
        before = held()
        sample(large, datetime.now(UTC), 2**40)
        admin.execute(
            "UPDATE billing.vector_storage_contracts SET renew_after=clock_timestamp() WHERE id=%s",
            (large,),
        )
        assert await usage.renew_storage() == 1
        _, until, prepaid, failed, _ = contract(large)
        assert prepaid == 125 and failed is not None and held() == before
        assert until - datetime.now(UTC) < timedelta(seconds=1)
        with pytest.raises(psycopg.errors.RaiseException, match="insufficient_funds"):
            admin.execute("SELECT billing.v1_storage_write(%s)", (large,))
        admin.execute(
            "UPDATE billing.vector_storage_contracts SET renew_after=clock_timestamp() WHERE id=%s",
            (large,),
        )
        await usage.renew_storage()
        assert contract(large)[4] == "delete"
        admin.execute(
            "UPDATE publication.vector_stores SET next_poll=clock_timestamp()+interval '1 hour'"
        )
        admin.execute(
            "UPDATE publication.vector_stores SET next_poll=clock_timestamp() WHERE id=%s",
            (large,),
        )
        claim = (
            await (await peer.connection.execute("SELECT gateway_v1.vector_claim()")).fetchone()
        )[0]
        assert claim["id"] == str(large) and claim["retire"] is True
        # Deletion ends the store and releases what is left of its hold.
        sample(large, datetime.now(UTC), 0, closing=True)
        assert contract(large)[0] is not None
        assert admin.execute(
            "SELECT r.reservation_state FROM billing.vector_storage_contracts c "
            "JOIN billing.request_runs r ON r.id=c.hold_id WHERE c.id=%s",
            (large,),
        ).fetchone() == ("released",)


async def test_lapsed_store_is_deleted_by_the_poller(
    http,
    admin,
    tls,  # noqa: F811
    relay_binary,  # noqa: F811
):
    calls = []
    deleted = asyncio.Event()

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = head[0].split()
            hdr = {
                k.lower(): v.strip() for line in head[1:] if line for k, v in [line.split(":", 1)]
            }
            await reader.readexactly(int(hdr.get("content-length", 0)))
            calls.append((method, path))
            value = {
                "object": "vector_store",
                "id": "vs_unfunded",
                "created_at": 1700000000,
                "status": "completed",
                "usage_bytes": 2**40,
                "file_counts": {"total": 0},
            }
            if method == "DELETE":
                value = {"object": "vector_store.deleted", "id": "vs_unfunded", "deleted": True}
                deleted.set()
            payload = json.dumps(value).encode()
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
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
            "openai_compatible",
            features=["vector_stores"],
        ) as (relay, _, key, _, plane),
    ):
        await configure(http, admin, plane)
        h = {"Authorization": "Bearer " + key}
        created = await relay.post(ROOT, headers=h, json={"name": "unfunded"})
        assert created.status_code == 200, created.text
        handle = created.json()["id"]
        store = handle.removeprefix("vs_nx_")
        # A terabyte for seven days exceeds the wallet: the store lapses at once.
        await http[2].state.usage.renew_storage()
        assert admin.execute(
            "SELECT lapse_action FROM billing.vector_storage_contracts WHERE id=%s", (store,)
        ).fetchone() == ("delete",)
        blocked = await relay.get(f"{ROOT}/{handle}", headers=h)
        assert blocked.status_code == 402, blocked.text
        admin.execute(
            "UPDATE publication.vector_stores SET next_poll=clock_timestamp() WHERE id=%s", (store,)
        )
        async with asyncio.timeout(10):
            await deleted.wait()
            while admin.execute(  # noqa: ASYNC110 - waiting for the poller's observation
                "SELECT closed FROM billing.vector_storage_contracts WHERE id=%s", (store,)
            ).fetchone() != (True,):
                await asyncio.sleep(0.05)
        assert ("DELETE", f"{ROOT}/vs_unfunded") in calls
        assert (await relay.get(f"{ROOT}/{handle}", headers=h)).status_code == 404
