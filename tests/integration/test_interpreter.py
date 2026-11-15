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
ROOT = "/v1/containers"


async def configure(http, admin, plane, tariff):
    api, token, app, _ = http
    _, _, _, model, dep, _, _, cluster = plane
    dims = ["container_1g_minutes", "container_4g_minutes"]
    r = await api.patch(
        f"/api/v1/models/{model['id']}",
        headers=auth(token, model["version"]),
        json={"tool_dimensions": dims},
    )
    assert r.status_code == 200, r.text
    for kind, scope, factor in [("sale", model, 1), ("cost", dep, Decimal(".4"))]:
        rates = {
            k: {"unit_price": str(v * factor), "unit_quantity": 20 if k in dims else 1}
            for k, v in {
                "input_uncached_tokens": 1,
                "output_tokens": 1,
                "container_1g_minutes": 2,
                "container_4g_minutes": 8,
            }.items()
        }
        path = f"/api/v1/{kind}-price-versions"
        r = await api.post(
            path,
            headers=headers(token, uuid4().hex),
            json={
                "currency": "USD",
                "rates": rates,
                ("model_id" if kind == "sale" else "deployment_id"): scope["id"],
            },
        )
        assert r.status_code == 201, r.text
        v = r.json()
        r = await api.post(
            f"{path}/{v['id']}/publish", headers=headers(token, uuid4().hex, v["version"]), json={}
        )
        assert r.status_code == 200, r.text
    path = f"/api/v1/deployments/{dep['id']}"
    old = (await api.get(path, headers=auth(token))).json()
    r = await api.patch(
        path,
        headers=auth(token, old["version"]),
        json={
            "relay_profile": old["relay_profile"]
            | {"container_reserve_minutes": 40, "container_tariff": tariff}
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
        headers=auth(token),
        json={
            "node_id": str(node),
            "database_role": role,
            "capabilities": [*caps, "relay.openai-containers.v1"],
        },
    )
    assert r.status_code == 200, r.text
    r = await api.post(
        f"/api/v1/gateway-clusters/{cluster}/releases",
        headers=headers(token, "openai-containers"),
        json={},
    )
    assert r.status_code == 202, r.text
    while await app.state.publication.distribute():
        pass
    async with asyncio.timeout(10):
        while admin.execute(  # noqa: ASYNC110 - wait for the installed revision
            "SELECT applied_revision FROM publication.gateway_principals WHERE node_id=%s", (node,)
        ).fetchone()[0] != int(r.json()["revision"]):
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("tariff", ["session_20m", "minute_min_5"])
async def test_interpreter_lifecycle(http, admin, tls, relay_binary, tariff):  # noqa: F811
    pid = "cntr_at_provider"
    fid = "cfile_at_provider"
    global_file = "file_at_provider"
    operations = []
    container = {
        "id": pid,
        "object": "container",
        "memory_limit": "4g",
        "created_at": 1781234567,
        "last_active_at": 1781234567,
        "name": "analysis",
        "status": "running",
        "expires_after": {"anchor": "last_active_at", "minutes": 20},
    }
    artifact = {
        "id": fid,
        "object": "container.file",
        "container_id": pid,
        "path": "/mnt/data/summary.txt",
        "bytes": 4,
        "created_at": 1781234567,
        "source": "assistant",
    }

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = head[0].split()
            hdr = {
                k.lower(): v.strip() for line in head[1:] if line for k, v in [line.split(":", 1)]
            }
            raw = await reader.readexactly(int(hdr.get("content-length", 0)))
            operations.append((method, path))
            mime = "application/json"
            if path == "/v1/files":
                value = {
                    "object": "file",
                    "id": global_file,
                    "created_at": 1781234567,
                    "filename": "table.txt",
                    "bytes": 4,
                    "purpose": "user_data",
                }
            elif path == ROOT:
                params = json.loads(raw)
                assert (
                    method == "POST"
                    and params["file_ids"] == [global_file]
                    and params["memory_limit"] == "4g"
                )
                value = container
            elif path == "/v1/responses":
                req = json.loads(raw)
                tool = req["tools"][0]
                assert tool["container"] == pid or tool["container"]["file_ids"] == [global_file]
                call = {
                    "id": "ci_one",
                    "type": "code_interpreter_call",
                    "status": "completed",
                    "container_id": pid,
                    "code": "print(4)",
                    "outputs": [{"type": "logs", "logs": "4"}],
                }
                cite = {
                    "type": "container_file_citation",
                    "container_id": pid,
                    "file_id": fid,
                    "filename": "summary.txt",
                    "start_index": 0,
                    "end_index": 1,
                }
                value = {
                    "id": "resp_exec",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        call,
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "4", "annotations": [cite]}
                            ],
                        },
                    ],
                    "tools": req["tools"],
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 0},
                    },
                }
                if req.get("stream"):
                    events = [
                        {
                            "type": "response.created",
                            "response": {
                                "id": "resp_exec",
                                "status": "in_progress",
                                "tools": req["tools"],
                            },
                        },
                        {"type": "response.output_item.done", "item": call},
                        {"type": "response.output_text.annotation.added", "annotation": cite},
                        {"type": "response.completed", "response": value},
                    ]
                    mime = "text/event-stream"
                    wire = "".join(
                        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
                    ).encode()
            elif path == f"{ROOT}/{pid}":
                value = (
                    {"object": "container.deleted", "id": pid, "deleted": True}
                    if method == "DELETE"
                    else container
                )
            elif path.startswith(f"{ROOT}/{pid}/files"):
                if path.endswith("/content"):
                    mime = "text/plain"
                    wire = b"sum4"
                elif path == f"{ROOT}/{pid}/files" and method == "GET":
                    value = {
                        "object": "list",
                        "data": [artifact],
                        "first_id": fid,
                        "last_id": fid,
                        "has_more": False,
                    }
                elif method == "DELETE":
                    value = {"object": "container.file.deleted", "id": fid, "deleted": True}
                else:
                    if method == "POST" and hdr["content-type"].startswith("multipart/"):
                        assert b"attachment" in raw
                    elif method == "POST":
                        assert json.loads(raw) == {"file_id": global_file}
                    value = artifact
            else:
                raise AssertionError(path)
            if mime == "application/json":
                wire = json.dumps(value).encode()
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\n"
                    f"Content-Length: {len(wire)}\r\nConnection: close\r\n\r\n"
                ).encode()
                + wire
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
            features=["code_execution", "files", "stored_responses", "background"],
        ) as (relay, model, key, _, plane),
    ):
        await configure(http, admin, plane, tariff)
        h = {"Authorization": "Bearer " + key}
        assert (
            await relay.post(
                ROOT,
                headers=h,
                json={
                    "name": "bad",
                    "network_policy": {"type": "allowlist", "allowed_domains": ["example.org"]},
                },
            )
        ).status_code == 400
        f = await relay.post(
            "/v1/files",
            headers=h,
            data={"purpose": "user_data"},
            files={"file": ("d.txt", b"data", "text/plain")},
        )
        assert f.status_code == 200, f.text
        local_file = f.json()["id"]
        created = await relay.post(
            ROOT,
            headers=h,
            json={"name": "analysis", "memory_limit": "4g", "file_ids": [local_file]},
        )
        assert created.status_code == 200, created.text
        handle = created.json()["id"]
        path = ROOT + "/" + handle
        assert handle.startswith("cntr_nx_") and pid not in created.text
        assert (await relay.get(ROOT, headers=h)).json()["data"] == [created.json()]
        assert (await relay.get(path, headers=h)).json()["id"] == handle
        assert admin.execute(
            "SELECT held_amount FROM billing.accounts WHERE organization_id=%s", (plane[2]["id"],)
        ).fetchone() == (16,)
        tool = {"type": "code_interpreter", "container": handle}
        body = {"model": model, "input": "Calculate 2+2", "tools": [tool]}
        reply = await relay.post("/v1/responses", headers=h, json=body)
        assert reply.status_code == 200, reply.text
        assert fid not in reply.text and pid not in reply.text
        cfile = reply.json()["output"][1]["content"][0]["annotations"][0]["file_id"]
        assert cfile.startswith("cfile_nx_")
        await wait_events(admin, 2)
        assert await http[2].state.usage.process() == 1
        auto = await relay.post(
            "/v1/responses",
            headers=h,
            json=body
            | {
                "tools": [
                    {
                        "type": "code_interpreter",
                        "container": {"type": "auto", "file_ids": [local_file]},
                    }
                ],
                "stream": True,
            },
        )
        assert auto.status_code == 200 and "event: response.completed" in auto.text, auto.text
        assert (
            fid not in auto.text
            and pid not in auto.text
            and global_file not in auto.text
            and cfile in auto.text
            and handle in auto.text
        )
        await wait_events(admin, 4)
        assert await http[2].state.usage.process() == 1
        assert admin.execute(
            "SELECT held_amount FROM billing.accounts WHERE organization_id=%s", (plane[2]["id"],)
        ).fetchone() == (16,)
        assert admin.execute(
            "SELECT state FROM billing.container_sessions ORDER BY created_at"
        ).fetchall() == [("active",), ("released",)]
        for kwargs in [
            {"json": {"file_id": local_file}},
            {"files": {"file": ("upload.txt", b"attachment", "text/plain")}},
        ]:
            upload = await relay.post(path + "/files", headers=h, **kwargs)
            assert upload.status_code == 200 and upload.json()["id"] == cfile, upload.text
        listing = await relay.get(path + "/files", headers=h)
        assert (
            listing.json()["first_id"] == cfile
            and listing.json()["data"][0]["container_id"] == handle
        )
        assert (
            await relay.get(path + "/files/" + cfile + "/content", headers=h)
        ).content == b"sum4"
        api, token, _, _ = http
        other = (
            await issue_key(
                api, token, plane[2], str(plane[5].service_account_id), name="other-container-owner"
            )
        ).json()["secret"]
        release = await api.post(
            f"/api/v1/gateway-clusters/{plane[7]}/releases",
            headers=headers(token, "container-other"),
            json={},
        )
        assert release.status_code == 202
        foreign = {"Authorization": "Bearer " + other}
        async with asyncio.timeout(10):
            while (await relay.get("/v1/models", headers=foreign)).status_code != 200:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
        n = len(operations)
        assert (await relay.get(ROOT, headers=foreign)).json()["data"] == []
        assert (
            await relay.get(path + "/files/" + cfile + "/content", headers=foreign)
        ).status_code == 404
        assert (await relay.post("/v1/responses", headers=foreign, json=body)).status_code == 404
        assert len(operations) == n
        assert (await relay.delete(path + "/files/" + cfile, headers=h)).json()["id"] == cfile
        assert (await relay.get(path + "/files/" + cfile, headers=h)).status_code == 404
        close = await relay.delete(path, headers=h)
        assert close.status_code == 200 and close.json()["id"] == handle, close.text
        assert await http[2].state.usage.review_containers() == 1
        assert await http[2].state.usage.review_containers() == 0
        assert await http[2].state.usage.process() == 1
        cost = 8 if tariff == "session_20m" else 2
        assert admin.execute(
            "SELECT charged_amount,container_unit,container_minutes_limit FROM "
            "billing.request_runs WHERE container_handle=%s",
            (handle,),
        ).fetchall() == [(cost, "container_4g_minutes", 40)]
        assert admin.execute(
            "SELECT cost_amount FROM billing.request_attempts a JOIN "
            "billing.request_runs r ON r.id=a.request_id WHERE r.container_handle=%s",
            (handle,),
        ).fetchone() == (Decimal(".4") * cost,)
        assert admin.execute(
            "SELECT balance,held_amount FROM billing.accounts WHERE organization_id=%s",
            (plane[2]["id"],),
        ).fetchone() == (
            100 - 6 - cost,
            0,
        )


async def test_container_review_and_unused_auto(http, admin, tls, relay_binary):  # noqa: F811
    pid = "cntr_review_supplier"
    posted = 0

    async def provider(reader, writer):
        nonlocal posted
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = head[0].split()
            hdr = {k.lower(): v.strip() for row in head[1:] if row for k, v in [row.split(":", 1)]}
            raw = await reader.readexactly(int(hdr.get("content-length", 0)))
            if method == "DELETE":
                v = {"id": pid, "object": "container.deleted", "deleted": True}
            elif path == ROOT:
                posted += 1
                v = {
                    "id": pid,
                    "object": "container",
                    "name": "idle",
                    "status": "running",
                    "created_at": 1781234567,
                    "memory_limit": "1g",
                }
            else:
                assert json.loads(raw)["tools"][0]["container"]["type"] == "auto"
                v = {
                    "id": "resp_unused",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}
                    ],
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 0},
                    },
                }
            wire = json.dumps(v).encode()
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(wire)}\r\n"
                    "Content-Type: application/json\r\nConnection: close\r\n\r\n"
                ).encode()
                + wire
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
            features=["code_execution", "files", "stored_responses", "background"],
        ) as (relay, model, key, _, plane),
    ):
        await configure(http, admin, plane, "session_20m")
        h = {"Authorization": "Bearer " + key}
        req = {
            "model": model,
            "input": "hi",
            "tools": [{"type": "code_interpreter", "container": {"type": "auto"}}],
        }
        simple = await relay.post("/v1/responses", headers=h, json=req)
        assert simple.status_code == 200, simple.text
        await wait_events(admin, 2)
        assert await http[2].state.usage.process() == 1
        assert admin.execute("SELECT state FROM billing.container_sessions").fetchone() == (
            "released",
        )
        c = await relay.post(ROOT, headers=h, json={"name": "idle"})
        assert c.status_code == 200, c.text
        handle = c.json()["id"]
        assert (await relay.delete(ROOT + "/" + handle, headers=h)).status_code == 200
        assert await http[2].state.usage.review_containers() == 1
        assert await http[2].state.usage.process() == 1
        rid, aid, charge, state = admin.execute(
            "SELECT r.id,a.id,r.charged_amount,r.state FROM billing.request_runs r JOIN "
            "billing.request_attempts a ON r.id=a.request_id WHERE container_handle=%s",
            (handle,),
        ).fetchone()
        assert (charge, state) == (None, "review")
        api, token, _, _ = http

        async def settle(kind, minutes):
            cases = (await api.get("/api/v1/reconciliation-cases", headers=auth(token))).json()[
                "items"
            ]
            case = next(
                c for c in cases if c["request_id"] == str(rid) and c["source_type"] == kind
            )
            return await api.post(
                f"/api/v1/reconciliation-cases/{case['id']}/resolve",
                headers=headers(token, uuid4().hex, case["version"]),
                json={
                    "action": "supplement_usage",
                    "reason": "Supplier account reviewed",
                    "evidence": "merchant-a/usage/line-01",
                    "normalization_version": "review.v1",
                    "attempt_id": str(aid),
                    "quantities": {
                        "input_uncached_tokens": 0,
                        "output_tokens": 0,
                        "container_1g_minutes": minutes,
                        "container_4g_minutes": 0,
                    },
                },
            )

        assert (await settle("request", 19)).status_code == 409
        for k, field, amount in [
            ("attempt", "cost_amount", "0.800000000000"),
            ("request", "charged_amount", "2.000000000000"),
        ]:
            res = await settle(k, 20)
            assert res.status_code == 200 and res.json()[field] == amount, res.text
        assert posted == 1 and await http[2].state.usage.review_containers() == 0


async def test_interpreter_ws_and_lost_create_receipt(http, admin, tls, relay_binary):  # noqa: F811
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve

    calls = 0

    async def provider(socket):
        nonlocal calls
        async for wire in socket:
            req = json.loads(wire)
            calls += 1
            assert req["tools"][0]["container"] == {"type": "auto"}
            pid = "cntr_ws_generated"
            call = {
                "id": "ci_ws",
                "type": "code_interpreter_call",
                "status": "completed",
                "container_id": pid,
                "code": "print(4)",
                "outputs": [],
            }
            data = {
                "id": "resp_ws_generated",
                "object": "response",
                "status": "completed",
                "output": [call],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "input_tokens_details": {"cached_tokens": 0},
                },
            }
            for event in [
                {
                    "type": "response.created",
                    "response": {"id": data["id"], "status": "in_progress"},
                },
                {"type": "response.output_item.done", "item": call},
                {"type": "response.completed", "response": data},
            ]:
                await socket.send(json.dumps(event))

    async with (
        serve(provider, "127.0.0.1", 0, ssl=tls[0]) as upstream,
        stream_plane(
            http,
            admin,
            relay_binary,
            tls[1],
            upstream.sockets[0].getsockname()[1],
            "openai_compatible",
            ws=True,
            features=["code_execution", "files", "stored_responses", "background"],
        ) as (relay, model, key, _, plane),
    ):
        await configure(http, admin, plane, "session_20m")
        async with connect(
            f"ws://127.0.0.1:{relay.base_url.port}/v1/responses",
            additional_headers={"Authorization": "Bearer " + key},
            proxy=None,
        ) as sock:
            await sock.send(
                json.dumps(
                    {
                        "type": "response.create",
                        "input": "calculate",
                        "model": model,
                        "tools": [{"type": "code_interpreter", "container": {"type": "auto"}}],
                    }
                )
            )
            events = [json.loads(await asyncio.wait_for(sock.recv(), 4)) for _ in range(3)]
            assert events[-2]["item"]["container_id"].startswith("cntr_nx_")
            assert (
                events[-1]["response"]["output"][0]["container_id"]
                == events[-2]["item"]["container_id"]
            )
        await wait_events(admin, 2)
        assert await http[2].state.usage.process() == 1
        assert calls == 1
        # Simulate a node which committed its funding hold before losing the create reply.
        peer, _, _, mod, dep, a, _, cluster = plane
        rev = admin.execute(
            "SELECT desired_revision FROM publication.gateway_clusters WHERE id=%s", (cluster,)
        ).fetchone()[0]
        trace = uuid4()
        await peer.connection.execute(
            "SELECT gateway_v1.container_admit(%s,%s,%s,%s,%s,NULL,'1g',true,false)",
            (trace, a.api_key_id, rev, mod["id"], dep["id"]),
        )
        admin.execute(
            "UPDATE billing.container_sessions SET review_after=clock_timestamp() "
            "WHERE state='provisional'"
        )
        assert (
            await http[2].state.usage.review_containers() == 1
            and await http[2].state.usage.review_containers() == 0
        )
        assert await http[2].state.usage.process() == 1
        row = admin.execute(
            "SELECT state,charged_amount,container_handle FROM billing.request_runs "
            "WHERE container_origin=%s",
            (trace,),
        ).fetchone()
        assert row == ("review", None, None)


async def test_background_container_is_bound_after_restart(http, admin, tls, relay_binary):  # noqa: F811
    from test_relay import running_relay

    finished = asyncio.Event()
    operations = []
    rid = "resp_ci_queued"
    pid = "cntr_ci_async"

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = head[0].split()
            headers_in = {
                k.lower(): v.strip() for row in head[1:] if row for k, v in [row.split(":", 1)]
            }
            raw = await reader.readexactly(int(headers_in.get("content-length", 0)))
            operations.append((method, path))
            value = {"object": "response", "id": rid, "status": "queued"}
            if method == "POST":
                assert (
                    path == "/v1/responses"
                    and json.loads(raw)["tools"][0]["type"] == "code_interpreter"
                )
            else:
                assert path == "/v1/responses/" + rid
                if finished.is_set():
                    value |= {
                        "status": "completed",
                        "output": [
                            {
                                "type": "code_interpreter_call",
                                "status": "completed",
                                "id": "ci_result",
                                "container_id": pid,
                                "code": "print(4)",
                                "outputs": [],
                            }
                        ],
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "input_tokens_details": {"cached_tokens": 0},
                        },
                    }
            wire = json.dumps(value).encode()
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(wire)}\r\n"
                    "Content-Type: application/json\r\nConnection: close\r\n\r\n"
                ).encode()
                + wire
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
            features=["code_execution", "files", "stored_responses", "background"],
        ) as (relay, model, key, _, plane),
    ):
        await configure(http, admin, plane, "minute_min_5")
        h = {"Authorization": "Bearer " + key}
        queued = await relay.post(
            "/v1/responses",
            headers=h,
            json={
                "model": model,
                "input": "calculate",
                "background": True,
                "tools": [{"type": "code_interpreter", "container": {"type": "auto"}}],
            },
        )
        assert queued.status_code == 200, queued.text
        stored = queued.json()["id"]
        assert stored.startswith("resp_nx_")
        process = relay.relay_process
        process.terminate()
        await asyncio.wait_for(process.wait(), 15)
        peer, private, _, _, _, _, _, cluster = plane
        async with running_relay(
            relay_binary, peer, private, cluster, http[3], tls[1], peer.connection.info.port
        ) as replacement:
            finished.set()
            async with asyncio.timeout(12):
                while not admin.execute("SELECT done FROM publication.response_jobs").fetchone()[0]:  # noqa: ASYNC110
                    await asyncio.sleep(0.05)
            # The poller linked the billable session even without a client retrieving the result.
            assert admin.execute(
                "SELECT state,completed_call FROM billing.container_sessions"
            ).fetchone() == ("active", True)
            assert await http[2].state.usage.process() == 1
            result = await replacement.get("/v1/responses/" + stored, headers=h)
            again = await replacement.get("/v1/responses/" + stored, headers=h)
            assert result.status_code == 200 and result.json() == again.json(), result.text
            assert (
                result.json()["output"][0]["container_id"].startswith("cntr_nx_")
                and pid not in result.text
            )
            assert operations.count(("POST", "/v1/responses")) == 1
            assert admin.execute("SELECT count(*) FROM billing.container_sessions").fetchone() == (
                1,
            )
            assert admin.execute(
                "SELECT sum(signed_amount) FROM billing.ledger_entries WHERE kind='usage_debit'"
            ).fetchone() == (-2,)


async def test_container_holds_grow_before_the_session_outlives_them(
    http,
    admin,
    tls,  # noqa: F811
    relay_binary,  # noqa: F811
):
    """A reused container keeps billing while it lives. Each operation first reserves every
    minute the supplier may bill until the operation's deadline plus the idle expiry."""
    pid = "cntr_long_lived"
    container = {
        "id": pid,
        "object": "container",
        "memory_limit": "1g",
        "created_at": 1781234567,
        "last_active_at": 1781234567,
        "name": "long",
        "status": "running",
        "expires_after": {"anchor": "last_active_at", "minutes": 20},
    }

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = head[0].split()
            hdr = {k.lower(): v.strip() for row in head[1:] if row for k, v in [row.split(":", 1)]}
            raw = await reader.readexactly(int(hdr.get("content-length", 0)))
            if path == "/v1/responses":
                req = json.loads(raw)
                call = {
                    "id": "ci_reuse",
                    "type": "code_interpreter_call",
                    "status": "completed",
                    "container_id": pid,
                    "code": "print(1)",
                    "outputs": [{"type": "logs", "logs": "1"}],
                }
                value = {
                    "id": "resp_reuse",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        call,
                        {"type": "message", "content": [{"type": "output_text", "text": "1"}]},
                    ],
                    "tools": req["tools"],
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 0},
                    },
                }
            else:
                assert method in {"GET", "POST"} and path.startswith(ROOT)
                value = container
            wire = json.dumps(value).encode()
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(wire)}\r\n"
                    "Content-Type: application/json\r\nConnection: close\r\n\r\n"
                ).encode()
                + wire
            )
            await writer.drain()
        finally:
            writer.close()

    def held():
        return admin.execute(
            "SELECT held_amount FROM billing.accounts WHERE organization_id=%s", (org["id"],)
        ).fetchone()[0]

    def age(minutes):
        admin.execute(
            "UPDATE billing.container_sessions SET created_at=created_at-make_interval(mins=>%s) "
            "WHERE provider_id=%s",
            (minutes, pid),
        )

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
            features=["code_execution"],
        ) as (relay, model, key, _, plane),
    ):
        org = plane[2]
        await configure(http, admin, plane, "session_20m")
        h = {"Authorization": "Bearer " + key}
        created = await relay.post(ROOT, headers=h, json={"name": "long"})
        assert created.status_code == 200, created.text
        handle = created.json()["id"]
        # The first hold covers the deployment's forty minutes at 2 per twenty minutes.
        assert held() == 4
        # Fifty minutes later a read refreshes the idle timer: the supplier may now bill until
        # fifteen plus twenty minutes from now, 85 minutes after creation, rounded to 100.
        age(50)
        assert (await relay.get(f"{ROOT}/{handle}", headers=h)).status_code == 200
        assert held() == 10
        meter, covered = admin.execute(
            "SELECT id,covered_minutes FROM billing.container_sessions WHERE provider_id=%s",
            (pid,),
        ).fetchone()
        assert covered == 100
        assert admin.execute(
            "SELECT e.minutes,r.container_unit,r.container_handle,r.reserved_amount "
            "FROM billing.container_extensions e JOIN billing.request_runs r ON r.id=e.request_id "
            "WHERE e.meter=%s",
            (meter,),
        ).fetchall() == [(60, "container_1g_minutes", handle, 6)]
        # An extension the wallet cannot fund refuses the reuse; nothing else changes.
        age(30 * 24 * 60)
        refused = await relay.get(f"{ROOT}/{handle}", headers=h)
        assert refused.status_code == 402, refused.text
        assert held() == 10
        age(-30 * 24 * 60 + 60)
        # In auto mode the supplier reuses this container. Its holds no longer cover the
        # request deadline plus idle time, so the request's own container hold extends it.
        auto = await relay.post(
            "/v1/responses",
            headers=h,
            json={
                "model": model,
                "input": "again",
                "tools": [{"type": "code_interpreter", "container": {"type": "auto"}}],
            },
        )
        assert auto.status_code == 200, auto.text
        extensions = admin.execute(
            "SELECT e.minutes,s.state FROM billing.container_extensions e "
            "JOIN billing.container_sessions s ON s.id=e.request_id WHERE e.meter=%s",
            (meter,),
        ).fetchall()
        assert extensions == [(40, "released")]
        assert admin.execute(
            "SELECT covered_minutes FROM billing.container_sessions WHERE id=%s", (meter,)
        ).fetchone() >= (160,)
        await wait_events(admin, 2)
        assert await http[2].state.usage.process() == 1
        before = held()
        admin.execute(
            "UPDATE billing.container_sessions SET review_after=clock_timestamp() WHERE id=%s",
            (meter,),
        )
        assert await http[2].state.usage.review_containers() == 1
        # The session's own hold and every extension wait for the supplier's minutes.
        runs = admin.execute(
            "SELECT count(*) FROM billing.usage_events WHERE kind='request.closed' "
            "AND (request_id=%s OR request_id IN "
            "(SELECT request_id FROM billing.container_extensions WHERE meter=%s))",
            (meter, meter),
        ).fetchone()[0]
        assert (
            runs
            == 1
            + admin.execute(
                "SELECT count(*) FROM billing.container_extensions WHERE meter=%s", (meter,)
            ).fetchone()[0]
        )
        assert held() == before
        # A reviewed session no longer accepts work.
        assert (await relay.get(f"{ROOT}/{handle}", headers=h)).status_code == 404
