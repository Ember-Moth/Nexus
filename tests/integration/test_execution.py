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


async def configure_execution(http, admin, plane):
    api, token, app, _ = http
    _, _, _, model, dep, _, _, cluster = plane
    r = await api.patch(
        f"/api/v1/models/{model['id']}",
        headers=auth(token, model["version"]),
        json={"tool_dimensions": ["code_execution_seconds"]},
    )
    assert r.status_code == 200, r.text
    for kind, scope, factor in [("sale", model, 1), ("cost", dep, Decimal("0.4"))]:
        rates = {
            k: {
                "unit_price": str(v * factor),
                "unit_quantity": 3600 if k == "code_execution_seconds" else 1,
            }
            for k, v in {
                "input_uncached_tokens": 1,
                "output_tokens": 1,
                "code_execution_seconds": 36,
            }.items()
        }
        path = "/api/v1/" + kind + "-price-versions"
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
            | {"code_execution": True, "execution_reserve_seconds": 600}
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
            "capabilities": [*caps, "relay.claude-execution.v1"],
        },
    )
    assert r.status_code == 200, r.text
    r = await api.post(
        f"/api/v1/gateway-clusters/{cluster}/releases",
        headers=headers(token, "claude-execution"),
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


async def test_messages_execution_scope_and_financial_evidence(http, admin, tls, relay_binary):  # noqa: F811
    pid = "container_provider"
    fid = "file_generated_provider"
    seen = []
    count = 1
    meta = {
        "id": fid,
        "type": "file",
        "filename": "answer.txt",
        "size_bytes": 1,
        "mime_type": "text/plain",
        "created_at": "2026-01-01T00:00:00Z",
        "downloadable": True,
    }

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = head[0].split()
            h = {k.lower(): v.strip() for row in head[1:] if row for k, v in [row.split(":", 1)]}
            raw = await reader.readexactly(int(h.get("content-length", 0)))
            mime = "application/json"
            if method == "GET":
                assert path.startswith("/v1/files/" + fid)
                if path.endswith("/content"):
                    payload = b"4"
                    mime = "text/plain"
                else:
                    payload = json.dumps(meta).encode()
            else:
                assert path == "/v1/messages"
                req = json.loads(raw)
                seen.append(req)
                if "container" in req:
                    assert req["container"] == pid
                for m in req["messages"]:
                    if isinstance(m["content"], list):
                        for b in m["content"]:
                            if b["type"] == "container_upload":
                                assert b["file_id"] == fid
                usage = {
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "server_tool_use": {"code_execution_requests": count},
                }
                content = (
                    [
                        {
                            "type": "server_tool_use",
                            "id": "srvtool1",
                            "name": "bash_code_execution",
                            "input": {"command": "printf 4"},
                        },
                        {
                            "type": "bash_code_execution_tool_result",
                            "tool_use_id": "srvtool1",
                            "content": {
                                "type": "bash_code_execution_result",
                                "stdout": "4",
                                "stderr": "",
                                "return_code": 0,
                                "content": [{"type": "bash_code_execution_output", "file_id": fid}],
                            },
                        },
                    ]
                    if count
                    else [{"type": "text", "text": "idle"}]
                )
                c = {"id": pid, "expires_at": "2026-12-01T00:00:00Z"}
                result = {
                    "type": "message",
                    "id": "msg_" + str(len(seen)),
                    "role": "assistant",
                    "content": content,
                    "container": c,
                    "stop_reason": "end_turn",
                    "usage": usage,
                }
                if req.get("stream"):
                    mime = "text/event-stream"
                    events = [
                        {
                            "type": "message_start",
                            "message": result
                            | {
                                "content": [],
                                "container": None,
                                "usage": usage | {"output_tokens": 0},
                            },
                        },
                        {"type": "content_block_start", "index": 0, "content_block": content[-1]},
                        {"type": "content_block_stop", "index": 0},
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": "end_turn", "container": c},
                            "usage": {"output_tokens": 1},
                        },
                        {"type": "message_stop"},
                    ]
                    payload = "".join(
                        f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
                    ).encode()
                else:
                    payload = json.dumps(result).encode()
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\nContent-Length: {len(payload)}\r\n"
                    "Connection: close\r\n\r\n"
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
            http, admin, relay_binary, tls[1], server.sockets[0].getsockname()[1], "anthropic"
        ) as (relay, model, key, _, plane),
    ):
        h = {"x-api-key": key}
        tool = {"type": "code_execution_20260120", "name": "code_execution"}
        req = {
            "model": model,
            "max_tokens": 4,
            "messages": [{"role": "user", "content": "Calculate 2+2."}],
            "tools": [tool],
        }
        assert (await relay.post("/v1/messages", headers=h, json=req)).status_code == 400
        await configure_execution(http, admin, plane)
        bundle = req | {"tools": [tool, {"type": "web_fetch_20260209", "name": "web_fetch"}]}
        res = await relay.post("/v1/messages", headers=h, json=bundle)
        assert res.status_code == 200, res.text
        container = res.json()["container"]["id"]
        file = res.json()["content"][-1]["content"]["content"][0]["file_id"]
        assert (
            container.startswith("cntr_nx_")
            and file.startswith("file_nx_")
            and pid not in res.text
            and fid not in res.text
        )
        assert (await relay.get("/v1/files", headers=h)).json()["data"] == [meta | {"id": file}]
        assert (await relay.get("/v1/files/" + file + "/content", headers=h)).content == b"4"
        preload = [
            {
                "role": "user",
                "content": [
                    {"type": "container_upload", "file_id": file},
                    {"type": "text", "text": "Read it."},
                ],
            }
        ]
        stream = await relay.post(
            "/v1/messages",
            headers=h,
            json=bundle | {"container": container, "messages": preload, "stream": True},
        )
        assert (
            stream.status_code == 200
            and "message_stop" in stream.text
            and container in stream.text
            and file in stream.text
        )
        assert pid not in stream.text and fid not in stream.text
        await wait_events(admin, 4)
        assert await http[2].state.usage.process() == 2
        assert (
            admin.execute(
                "SELECT execution_mode,reserved_amount,charged_amount FROM billing.request_runs"
            ).fetchall()
            == [("bundled", 44, 3)] * 2
        )
        paid = await relay.post("/v1/messages", headers=h, json=req | {"container": container})
        assert paid.status_code == 200, paid.text
        await wait_events(admin, 6)
        assert await http[2].state.usage.process() == 1
        rid, aid, reserved, charged, state = admin.execute(
            "SELECT r.id,a.id,r.reserved_amount,r.charged_amount,r.state FROM "
            "billing.request_runs r JOIN billing.request_attempts a ON a.request_id=r.id "
            "WHERE r.execution_mode='metered'"
        ).fetchone()
        assert (reserved, charged, state) == (50, None, "review")
        api, token, _, _ = http

        async def resolve(rid, aid, kind, quantity, reference):
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
                    "reason": "Verified supplier execution interval",
                    "evidence": reference,
                    "attempt_id": str(aid),
                    "normalization_version": "review.v1",
                    "quantities": quantity,
                },
            )

        units = {"input_uncached_tokens": 2, "output_tokens": 1, "code_execution_seconds": 300}
        assert (
            await resolve(rid, aid, "request", units | {"output_tokens": 0}, "supplier-line-a")
        ).status_code == 409
        for kind, k, v in [
            ("attempt", "cost_amount", "2.400000000000"),
            ("request", "charged_amount", "6.000000000000"),
        ]:
            fixed = await resolve(rid, aid, kind, units, "supplier-line-a")
            assert fixed.status_code == 200 and fixed.json()[k] == v, fixed.text
        count = 0
        idle = await relay.post("/v1/messages", headers=h, json=req | {"container": container})
        assert idle.status_code == 200
        await wait_events(admin, 8)
        await http[2].state.usage.process()
        assert admin.execute(
            "SELECT charged_amount FROM billing.request_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone() == (3,)
        unused = await relay.post("/v1/messages", headers=h, json=req | {"messages": preload})
        assert unused.status_code == 200
        await wait_events(admin, 10)
        await http[2].state.usage.process()
        rid, aid, state = admin.execute(
            "SELECT r.id,a.id,r.state FROM billing.request_runs r JOIN "
            "billing.request_attempts a ON r.id=a.request_id WHERE "
            "execution_mode='preloaded'"
        ).fetchone()
        assert state == "review"
        assert (await resolve(rid, aid, "request", units, "supplier-line-a")).status_code == 409
        foreign = (
            await issue_key(
                api, token, plane[2], str(plane[5].service_account_id), name="other-execution-owner"
            )
        ).json()["secret"]
        release = await api.post(
            f"/api/v1/gateway-clusters/{plane[7]}/releases",
            headers=headers(token, "execution-other"),
            json={},
        )
        assert release.status_code == 202
        other = {"x-api-key": foreign}
        async with asyncio.timeout(10):
            while (  # noqa: ASYNC110 - wait for the new key verifier
                await relay.get("/v1/models", headers={"Authorization": "Bearer " + foreign})
            ).status_code != 200:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
        before = len(seen)
        assert (
            await relay.post("/v1/messages", headers=other, json=bundle | {"container": container})
        ).status_code == 404
        assert (
            await relay.post("/v1/messages", headers=other, json=bundle | {"messages": preload})
        ).status_code == 404
        assert before == len(seen)


async def test_batch_code_execution_has_no_loop_bound_and_is_rejected(
    http,
    admin,
    tls,  # noqa: F811
    relay_binary,  # noqa: F811
):
    """Message Batches run server tool loops past the interactive limit of ten iterations,
    and the batch limit is not documented. Code execution and dynamic fetch take no
    max_uses, so their input has no upper bound and the batch is rejected before any hold."""
    root = "/v1/messages/batches"
    batch_id = "msgbatch_exec"
    result = {
        "type": "message",
        "role": "assistant",
        "id": "msg_batch_artifact",
        "container": {"id": "container_batch"},
        "stop_reason": "end_turn",
        "content": [
            {
                "type": "code_execution_tool_result",
                "tool_use_id": "tool_exec",
                "content": {
                    "type": "code_execution_result",
                    "return_code": 0,
                    "content": [
                        {"type": "code_execution_output", "file_id": "file_batch_artifact"}
                    ],
                },
            }
        ],
        "usage": {
            "input_tokens": 2,
            "output_tokens": 1,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "server_tool_use": {"code_execution_requests": 1},
        },
    }

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = head[0].split()
            hdr = {k.lower(): v.strip() for row in head[1:] if row for k, v in [row.split(":", 1)]}
            raw = await reader.readexactly(int(hdr.get("content-length", 0)))
            if path.endswith("/results"):
                values = [
                    {"custom_id": c, "result": {"type": "succeeded", "message": result}}
                    for c in ["free", "paid"]
                ]
                wire = b"\n".join(json.dumps(v).encode() for v in values)
                mime = "application/x-jsonl"
            else:
                if method == "POST":
                    assert len(json.loads(raw)["requests"]) == 2
                wire = json.dumps(
                    {
                        "id": batch_id,
                        "type": "message_batch",
                        "processing_status": "ended",
                        "request_counts": {
                            "processing": 0,
                            "succeeded": 2,
                            "errored": 0,
                            "expired": 0,
                            "canceled": 0,
                        },
                        "results_url": root + "/" + batch_id + "/results",
                    }
                ).encode()
                mime = "application/json"
            writer.write(
                (
                    f"HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\nContent-Length: {len(wire)}\r\n"
                    "Connection: close\r\n\r\n"
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
            http, admin, relay_binary, tls[1], server.sockets[0].getsockname()[1], "anthropic"
        ) as (relay, model, key, _, plane),
    ):
        await configure_execution(http, admin, plane)
        h = {"x-api-key": key}
        tool = {"type": "code_execution_20250522", "name": "code_execution"}
        base = {
            "model": model,
            "messages": [{"role": "user", "content": "chart"}],
            "max_tokens": 4,
            "tools": [tool],
        }
        free = base | {"tools": [tool, {"type": "web_fetch_20260309", "name": "web_fetch"}]}
        for params in (free, base):
            res = await relay.post(
                root, headers=h, json={"requests": [{"custom_id": "item", "params": params}]}
            )
            assert res.status_code == 400, res.text
        assert admin.execute("SELECT count(*) FROM billing.request_runs").fetchone() == (0,)
        assert admin.execute(
            "SELECT held_amount FROM billing.accounts WHERE organization_id=%s",
            (plane[2]["id"],),
        ).fetchone() == (Decimal(0),)
