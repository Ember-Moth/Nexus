"""P3 native wire tests use a real client, TLS provider, node SQL role and Worker."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from pg_receipt import lose_gateway_receipts
from test_billing import account_amounts, headers, make_budget
from test_identity_api import auth
from test_plane import provision_plane
from test_relay import relay_binary, running_relay, tls, wait_events  # noqa: F401
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus

pytestmark = pytest.mark.integration


@asynccontextmanager
async def stream_plane(
    http, admin, binary, ca, port, family, *, ws=False, convert_from=None, auto_cache=False
):
    client, root, app, settings = http
    async with provision_plane(
        http,
        admin,
        family,
        base_url=f"https://127.0.0.1:{port}/v1",
        dimensions=(
            {
                "input_uncached_tokens": "1",
                "output_tokens": "1",
                "input_cache_read_tokens": "0.1",
                (
                    "input_cache_write_30m_tokens"
                    if family == "openai_compatible"
                    else "input_cache_write_5m_tokens"
                ): "1.25",
            }
            if auto_cache
            else None
        ),
    ) as plane:
        peer, private, org, model, dep, admission, token, cid = plane
        await make_budget(client, root, org, "100")
        profile = {
            "endpoint": "anthropic_messages" if family == "anthropic" else "openai_responses",
            "transports": ["http", "websocket"] if ws else ["http"],
            "input_ceiling": 4,
            "output_ceiling": 6,
            "streaming": True,
            "tools": True,
            "continuation": ws,
            "cancellation": ws,
            "bound_reference": "isolated-stream",
            "count_rule": "deployment_ceiling.v1",
        }
        if auto_cache:
            profile["auto_prompt_cache"] = True
        if convert_from:
            profile["convert_from"] = convert_from
        r = await client.patch(
            f"/api/v1/deployments/{dep['id']}",
            headers=auth(root, dep["version"]),
            json={"relay_profile": profile},
        )
        assert r.status_code == 200, r.text
        nid, role = admin.execute(
            "SELECT node_id,database_role FROM publication.gateway_principals WHERE cluster_id=%s",
            (cid,),
        ).fetchone()
        caps = ["reservation.v1", "usage-events.v1", "relay.native.v1", "relay.sse.v1"]
        if ws:
            caps.append("relay.responses-ws.v1")
        r = await client.put(
            f"/api/v1/gateway-clusters/{cid}/principals/{nid}",
            headers=auth(root),
            json={"node_id": str(nid), "database_role": role, "capabilities": caps},
        )
        assert r.status_code == 200, r.text
        if convert_from:
            blocked = await client.post(
                f"/api/v1/gateway-clusters/{cid}/releases",
                headers=headers(root, "old-node"),
                json={},
            )
            assert blocked.status_code == 409, blocked.text
            direction = (
                "messages-to-responses"
                if convert_from == "anthropic_messages"
                else "responses-to-messages"
            )
            caps.append(f"relay.{direction}.v1")
            r = await client.put(
                f"/api/v1/gateway-clusters/{cid}/principals/{nid}",
                headers=auth(root),
                json={"node_id": str(nid), "database_role": role, "capabilities": caps},
            )
            assert r.status_code == 200, r.text
        if auto_cache:
            blocked = await client.post(
                f"/api/v1/gateway-clusters/{cid}/releases",
                headers=headers(root, "node-without-prompt-cache"),
                json={},
            )
            assert (
                blocked.status_code == 409
                and blocked.json()["error"]["code"] == "unsupported_capability"
            )
            caps.append("relay.prompt-cache.v1")
            r = await client.put(
                f"/api/v1/gateway-clusters/{cid}/principals/{nid}",
                headers=auth(root),
                json={"node_id": str(nid), "database_role": role, "capabilities": caps},
            )
            assert r.status_code == 200, r.text
        r = await client.put(
            f"/api/v1/organizations/{org['id']}/quota-policies"
            f"/customer/organization/{org['id']}/concurrency",
            headers=auth(root),
            json={"limit_value": 1},
        )
        assert r.status_code == 201, r.text
        r = await client.post(
            f"/api/v1/gateway-clusters/{cid}/releases", headers=headers(root, "p3"), json={}
        )
        assert r.status_code == 202, r.text
        while await app.state.publication.distribute():
            pass
        info = peer.connection.info
        async with (
            lose_gateway_receipts(info.host, info.port) as (proxy, dropped),
            running_relay(binary, peer, private, cid, settings, ca, proxy) as public,
        ):
            yield public, model["public_name"], token, dropped, plane


def usage(messages):
    if messages:
        return {
            "input_tokens": 2,
            "output_tokens": 3,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
    return {"input_tokens": 2, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 0}}


def events(messages, rid):
    if not messages:
        return [
            {"type": "response.created", "response": {"id": rid, "status": "in_progress"}},
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "a",
                "output_index": 0,
                "delta": "日本",
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "b",
                "output_index": 1,
                "delta": "{}",
            },
            {
                "type": "response.completed",
                "response": {"id": rid, "status": "completed", "usage": usage(False)},
            },
        ]
    return [
        {
            "type": "message_start",
            "message": {"id": rid, "usage": {**usage(True), "output_tokens": 0}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "a", "name": "lookup", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"q":"日本"}'},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    ]


@pytest.mark.parametrize("family", ["anthropic", "openai_compatible"])
async def test_native_sse(http, admin, tls, relay_binary, family, monkeypatch):  # noqa: F811
    monkeypatch.setenv("NEXUS_RELAY_STREAM_IDLE_MS", "2000")
    messages = family == "anthropic"
    path = "/v1/messages" if messages else "/v1/responses"
    received = []
    mode = "success"
    gate = asyncio.Event()
    disconnected = asyncio.Event()

    async def provider(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            hdr = dict(line.lower().split(": ", 1) for line in head[1:] if line)
            req = json.loads(await reader.readexactly(int(hdr["content-length"])))
            received.append(req)
            assert req["stream"] and hdr["accept"] == "text/event-stream"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
            )
            if mode == "empty":
                return
            for i, event in enumerate(events(messages, "r1")):
                if i == 1:
                    if mode in {"cancel", "idle"}:
                        await reader.read()
                        disconnected.set()
                        return
                    if mode == "partial":
                        writer.write(b"data: {")
                        return
                    if mode == "error":
                        event = {
                            "type": "error",
                            "error": {"type": "server_error", "message": "synthetic"},
                        }
                    else:
                        await gate.wait()
                wire = (
                    f"event: {event['type']}\r\ndata: "
                    + json.dumps(event, ensure_ascii=False)
                    + "\r\n\r\n"
                ).encode()
                for byte in wire:
                    writer.write(bytes([byte]))
                    await writer.drain()
                if mode == "error" and i == 1:
                    return
        finally:
            writer.close()

    upstream = await asyncio.start_server(provider, "127.0.0.1", 0, ssl=tls[0])
    async with (
        upstream,
        stream_plane(
            http, admin, relay_binary, tls[1], upstream.sockets[0].getsockname()[1], family
        ) as (public, model, token, dropped, plane),
    ):
        org = plane[2]
        hdr = {"x-api-key": token} if messages else {"authorization": f"Bearer {token}"}
        body = {
            "model": model,
            "stream": True,
            **(
                {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 6}
                if messages
                else {"input": "hi"}
            ),
        }
        async with public.stream("POST", path, headers=hdr, json=body) as reply:
            assert reply.status_code == 200 and reply.headers["content-type"] == "text/event-stream"
            lines = reply.aiter_lines()
            first = await asyncio.wait_for(anext(lines), 3)
            assert first.startswith("event: ")
            await asyncio.sleep(0.8)
            assert (await public.post(path, headers=hdr, json=body)).status_code == 429
            gate.set()
            data = [json.loads(line[6:]) async for line in lines if line.startswith("data: ")]
        assert data == events(messages, "r1")
        await wait_events(admin, 2)
        assert await http[2].state.usage.process() == 1
        assert account_amounts(admin, org) == (
            95,
            0,
        )
        assert admin.execute("SELECT spent,reserved FROM billing.budget_periods").fetchone() == (
            5,
            0,
        )
        assert (
            dropped == ["reserve_request", "start_attempt", "append_usage_event"]
            and len(received) == 1
        )
        mode = "error"
        reply = await public.post(path, headers=hdr, json=body)
        assert "event: error" in reply.text
        await wait_events(admin, 4)
        await http[2].state.usage.process()
        assert account_amounts(admin, org) == (
            95,
            0,
        )
        mode = "partial"
        import httpx

        with pytest.raises(httpx.RemoteProtocolError):
            await public.post(path, headers=hdr, json=body)
        await wait_events(admin, 6)
        mode = "cancel"
        async with public.stream("POST", path, headers=hdr, json=body) as reply:
            assert (await anext(reply.aiter_lines())).startswith("event:")
        await asyncio.wait_for(disconnected.wait(), 5)
        await wait_events(admin, 8)
        await http[2].state.usage.process()
        assert account_amounts(admin, org) == (
            95,
            20,
        )
        disconnected.clear()
        mode = "idle"
        with pytest.raises(httpx.RemoteProtocolError):
            await public.post(path, headers=hdr, json=body)
        await asyncio.wait_for(disconnected.wait(), 5)
        await wait_events(admin, 10)
        await http[2].state.usage.process()
        assert account_amounts(admin, org) == (
            95,
            30,
        )
        async with connect(
            f"ws://127.0.0.1:{public.base_url.port}/v1/responses",
            additional_headers={"authorization": f"Bearer {token}"},
            proxy=None,
        ) as ws:
            await send(ws, {"type": "response.create", "model": model, "input": "hi"})
            assert (await recv(ws))["error"]["code"] == "deployment_unavailable"
        mode = "empty"
        first_failure = await public.post(path, headers=hdr, json=body)
        assert first_failure.status_code == 502
        await wait_events(admin, 12)
        await http[2].state.usage.process()
        assert account_amounts(admin, org) == (
            95,
            40,
        )
        assert len(received) == 6


async def send(ws, data):
    await ws.send(json.dumps(data))


async def recv(ws):
    return json.loads(await asyncio.wait_for(ws.recv(), 5))


async def test_responses_ws(http, admin, tls, relay_binary):  # noqa: F811
    connections = []
    creates = []
    finish = asyncio.Event()

    async def provider(ws):
        connections.append(ws)
        assert (
            ws.request.path == "/v1/responses"
            and ws.request.headers["Authorization"] == "Bearer provider-secret-do-not-echo"
        )
        async for text in ws:
            create = json.loads(text)
            creates.append(create)
            assert (
                create["type"] == "response.create"
                and create["store"] is False
                and create["max_output_tokens"] == 6
            )
            seq = len(creates)
            rid = f"r{seq}"
            await send(
                ws, {"type": "response.created", "response": {"id": rid, "status": "in_progress"}}
            )
            if seq == 1:
                await finish.wait()
                await send(
                    ws,
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "id": "i1",
                            "call_id": "c1",
                            "name": "lookup",
                            "arguments": "{}",
                        },
                    },
                )
            if seq == 2:
                # Late terminal and cancel error from turn one cannot end turn two.
                await send(
                    ws,
                    {
                        "type": "response.completed",
                        "response": {"id": "r1", "status": "completed", "usage": usage(False)},
                    },
                )
                await send(
                    ws,
                    {
                        "type": "error",
                        "error": {"event_id": "obsolete-control", "code": "cancel_failed"},
                    },
                )
            await send(
                ws,
                {
                    "type": "response.completed",
                    "response": {"id": rid, "status": "completed", "usage": usage(False)},
                },
            )

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
        ) as (public, model, token, dropped, plane),
    ):
        org = plane[2]
        uri = f"ws://127.0.0.1:{public.base_url.port}/v1/responses"
        admission = plane[5]
        with pytest.raises(InvalidStatus) as error:
            async with connect(
                uri, additional_headers={"authorization": "Bearer invalid"}, proxy=None
            ):
                pass
        assert error.value.response.status_code == 401
        hdr = {"Authorization": f"Bearer {token}"}
        create = {"type": "response.create", "model": model, "input": "hi", "max_output_tokens": 6}
        async with connect(uri, additional_headers=hdr, proxy=None) as ws:
            assert ws.response.status_code == 101
            await (await ws.ping())
            assert not connections and admin.execute(
                "SELECT count(*) FROM billing.request_runs"
            ).fetchone() == (0,)
            await send(ws, {**create, "previous_response_id": "foreign"})
            assert (await recv(ws))["error"]["code"] == "previous_response_not_found"
            await send(ws, create)
            assert (await recv(ws))["response"]["id"] == "r1"
            await send(ws, {**create, "event_id": "overlap"})
            err = await recv(ws)
            assert (
                err["error"]["code"] == "response_in_progress"
                and err["error"]["event_id"] == "overlap"
            )
            await asyncio.sleep(0.8)
            assert (
                await public.post(
                    "/v1/responses", headers=hdr, json={"model": model, "input": "hi"}
                )
            ).status_code == 429
            finish.set()
            assert (await recv(ws))["item"]["call_id"] == "c1"
            assert (await recv(ws))["type"] == "response.completed"
            nxt = {
                **create,
                "previous_response_id": "r1",
                "input": [{"type": "function_call_output", "call_id": "c1", "output": "ok"}],
            }
            await send(ws, nxt)
            assert (await recv(ws))["response"]["id"] == "r2"
            assert (await recv(ws))["response"]["id"] == "r2"
            await wait_events(admin, 4)
            assert len(connections) == 1 and len(creates) == 2
            assert (
                creates[1]["previous_response_id"] == "r1" and creates[1]["input"] == nxt["input"]
            )
            assert await http[2].state.usage.process() == 2
            assert admin.execute(
                "SELECT charged_amount,input_limit FROM billing.request_runs"
            ).fetchall() == [(5, 4), (5, 4)]
            assert admin.execute(
                "SELECT signed_amount FROM billing.ledger_entries WHERE kind='usage_debit'"
            ).fetchall() == [(-5,), (-5,)]
            assert admin.execute(
                "SELECT spent,reserved FROM billing.budget_periods"
            ).fetchone() == (10, 0)
            assert account_amounts(admin, org) == (
                90,
                0,
            )
            assert dropped == ["reserve_request", "start_attempt", "append_usage_event"]
            async with connect(uri, additional_headers=hdr, proxy=None) as other:
                await send(other, nxt)
                assert (await recv(other))["error"]["code"] == "previous_response_not_found"
            r = await http[0].post(
                f"/api/v1/organizations/{org['id']}/api-keys/{admission.api_key_id}/revoke",
                headers=auth(http[1], 1),
            )
            assert r.status_code == 200
            await send(ws, {**nxt, "previous_response_id": "r2"})
            assert (await recv(ws))["type"] == "error"
            assert len(creates) == 2


async def test_ws_controls_and_unknown_send(http, admin, tls, relay_binary):  # noqa: F811
    creates = []
    cancels = []

    async def provider(ws):
        async for raw in ws:
            creates.append(json.loads(raw))
            seq = len(creates)
            if seq == 2:
                await ws.close()
                return
            await send(
                ws,
                {
                    "type": "response.created",
                    "stream_id": "one",
                    "response": {"id": "live", "status": "in_progress"},
                },
            )
            for accepted in [False, True]:
                cmd = json.loads(await ws.recv())
                cancels.append(cmd)
                assert cmd["type"] == "response.cancel" and cmd["response_id"] == "live"
                if not accepted:
                    await send(
                        ws,
                        {
                            "type": "error",
                            "stream_id": "one",
                            "error": {"event_id": cmd["event_id"], "code": "cancel_rejected"},
                        },
                    )
                else:
                    await send(
                        ws,
                        {
                            "type": "response.cancelled",
                            "stream_id": "one",
                            "response": {
                                "id": "live",
                                "status": "cancelled",
                                "usage": usage(False),
                            },
                        },
                    )

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
        ) as (public, model, key, _, plane),
    ):
        org = plane[2]
        uri = f"ws://127.0.0.1:{public.base_url.port}/v1/responses"
        hdr = {"authorization": f"Bearer {key}"}
        create = {"type": "response.create", "stream_id": "one", "model": model, "input": "hi"}
        async with connect(uri, additional_headers=hdr, proxy=None) as ws:
            await send(ws, create)
            assert (await recv(ws))["response"]["id"] == "live"
            cancel = {
                "type": "response.cancel",
                "stream_id": "one",
                "response_id": "live",
                "event_id": "cancel-1",
            }
            await send(ws, {**cancel, "response_id": "foreign"})
            assert (await recv(ws))["error"]["code"] == "cancel_target_mismatch"
            await send(ws, cancel)
            error = await recv(ws)
            assert error["error"] == {"event_id": "cancel-1", "code": "cancel_rejected"}
            await send(ws, {**cancel, "event_id": "cancel-2"})
            assert (await recv(ws))["type"] == "response.cancelled"
            await wait_events(admin, 2)
            assert await http[2].state.usage.process() == 1
            assert account_amounts(admin, org) == (
                95,
                0,
            )
            assert len(cancels) == 2 and len(creates) == 1
            await send(ws, create)
            assert (await recv(ws))["type"] == "error"
        await wait_events(admin, 4)
        assert await http[2].state.usage.process() == 1
        assert len(creates) == 2 and account_amounts(admin, org) == (95, 10)
        upstream.close()
        await upstream.wait_closed()
        async with connect(uri, additional_headers=hdr, proxy=None) as ws:
            await send(ws, create)
            assert (await recv(ws))["error"]["code"] == "upstream_handshake_failed"
        assert admin.execute("SELECT count(*) FROM billing.request_attempts").fetchone() == (2,)
        assert admin.execute(
            "SELECT count(*) FROM billing.request_runs WHERE state='canceled'"
        ).fetchone() == (1,)


async def test_drain_active_ws(http, admin, tls, relay_binary):  # noqa: F811
    release = asyncio.Event()

    async def provider(ws):
        await ws.recv()
        await send(
            ws,
            {"type": "response.created", "response": {"id": "draining", "status": "in_progress"}},
        )
        await release.wait()
        await send(
            ws,
            {
                "type": "response.completed",
                "response": {"id": "draining", "status": "completed", "usage": usage(False)},
            },
        )

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
        ) as (public, model, key, _, plane),
    ):
        org = plane[2]
        uri = f"ws://127.0.0.1:{public.base_url.port}/v1/responses"
        async with connect(
            uri, additional_headers={"authorization": f"Bearer {key}"}, proxy=None
        ) as ws:
            await send(ws, {"type": "response.create", "model": model, "input": "hi"})
            assert (await recv(ws))["type"] == "response.created"
            public.relay_process.terminate()
            await asyncio.sleep(0.1)
            release.set()
            assert (await recv(ws))["type"] == "response.completed"
            await asyncio.wait_for(ws.wait_closed(), 5)
            assert await asyncio.wait_for(public.relay_process.wait(), 5) == 0
        assert await http[2].state.usage.process() == 1
        assert account_amounts(admin, org) == (
            95,
            0,
        )


@pytest.mark.parametrize("transport", ["sse", "websocket"])
async def test_slow_downstream_keeps_lease_until_bounded_abort(
    http,
    admin,
    tls,  # noqa: F811
    relay_binary,  # noqa: F811
    monkeypatch,
    transport,  # noqa: F811
):  # noqa: F811
    import socket

    from websockets.exceptions import ConnectionClosed

    monkeypatch.setenv("NEXUS_RELAY_STREAM_IDLE_MS", "1000")
    started = asyncio.Event()
    ended = asyncio.Event()
    progress = 0
    cap = 2048
    is_ws = transport == "websocket"

    async def flood(put):
        nonlocal progress
        try:
            await put(
                {"type": "response.created", "response": {"id": "slow", "status": "in_progress"}}
            )
            started.set()
            for i in range(cap):
                await put(
                    {"type": "response.output_text.delta", "item_id": "x", "delta": "x" * 65536}
                )
                progress = i + 1
        except OSError, ConnectionClosed:
            pass
        finally:
            ended.set()

    async def http_provider(reader, writer):
        try:
            header = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            length = next(
                int(line.partition(":")[2])
                for line in header.split("\r\n")
                if line.lower().startswith("content-length:")
            )
            await reader.readexactly(length)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
            )

            async def put(e):
                writer.write(("data: " + json.dumps(e) + "\n\n").encode())
                await writer.drain()

            await flood(put)
        finally:
            writer.close()

    async def ws_provider(ws):
        await ws.recv()
        await flood(lambda e: send(ws, e))

    if is_ws:
        provider = await serve(
            ws_provider, "127.0.0.1", 0, ssl=tls[0], compression=None, close_timeout=0.1
        )
    else:
        provider = await asyncio.start_server(http_provider, "127.0.0.1", 0, ssl=tls[0])
    async with (
        provider,
        stream_plane(
            http,
            admin,
            relay_binary,
            tls[1],
            provider.sockets[0].getsockname()[1],
            "openai_compatible",
            ws=is_ws,
        ) as (public, model, key, _, plane),
    ):
        org = plane[2]
        params = {"model": model, "input": "hi"}
        hdr = {"authorization": f"Bearer {key}"}
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.setblocking(False)
        await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", public.base_url.port))
        if is_ws:
            ws = await connect(
                f"ws://127.0.0.1:{public.base_url.port}/v1/responses",
                sock=sock,
                additional_headers=hdr,
                proxy=None,
                max_queue=1,
                compression=None,
                close_timeout=0.1,
            )
            await send(ws, {**params, "type": "response.create"})
        else:
            body = json.dumps({**params, "stream": True}).encode()
            await asyncio.get_running_loop().sock_sendall(
                sock,
                b"POST /v1/responses HTTP/1.1\r\nHost: localhost\r\n"
                + f"Authorization: Bearer {key}\r\nContent-Length: {len(body)}\r\n\r\n".encode()
                + body,
            )
        try:
            await asyncio.wait_for(started.wait(), 5)
            await asyncio.sleep(0.7)
            assert 0 < progress < cap and not ended.is_set()
            assert (await public.post("/v1/responses", headers=hdr, json=params)).status_code == 429
            # The socket remains unread during the entire upstream inactivity deadline.
            await asyncio.wait_for(ended.wait(), 6)
        finally:
            if is_ws:
                await ws.close()
            else:
                sock.close()
        await wait_events(admin, 2)
        await http[2].state.usage.process()
        assert account_amounts(admin, org) == (
            100,
            10,
        )
        assert admin.execute("SELECT count(*) FROM billing.request_attempts").fetchone() == (1,)


@pytest.mark.parametrize("change", ["freeze", "credential", "model"])
async def test_ws_bound_turn_rechecks(http, admin, tls, relay_binary, change):  # noqa: F811
    received = []

    async def provider(ws):
        async for raw in ws:
            received.append(json.loads(raw))
            await send(
                ws,
                {"type": "response.created", "response": {"id": "bound", "status": "in_progress"}},
            )
            await send(
                ws,
                {
                    "type": "response.completed",
                    "response": {"id": "bound", "status": "completed", "usage": usage(False)},
                },
            )

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
        ) as (public, model, key, _, values),
    ):
        _, _, org, _, dep, _, _, cid = values
        async with connect(
            f"ws://127.0.0.1:{public.base_url.port}/v1/responses",
            additional_headers={"authorization": f"Bearer {key}"},
            proxy=None,
        ) as ws:
            create = {"type": "response.create", "model": model, "input": "hi"}
            await send(ws, create)
            assert (await recv(ws))["type"] == "response.created"
            assert (await recv(ws))["type"] == "response.completed"
            await wait_events(admin, 2)
            assert await http[2].state.usage.process() == 1
            if change == "freeze":
                admin.execute(
                    "UPDATE billing.accounts SET status='frozen' WHERE organization_id=%s",
                    (org["id"],),
                )
            elif change == "credential":
                cred = await http[0].post(
                    f"/api/v1/upstreams/{dep['upstream_id']}/credentials",
                    headers=auth(http[1]),
                    json={"name": "next", "secret": "fixture-next"},
                )
                assert cred.status_code == 201
                new = await http[0].patch(
                    f"/api/v1/deployments/{dep['id']}",
                    headers=auth(http[1], 2),
                    json={"credential_id": cred.json()["id"]},
                )
                assert new.status_code == 200
                rel = await http[0].post(
                    f"/api/v1/gateway-clusters/{cid}/releases",
                    headers=headers(http[1], "binding"),
                    json={},
                )
                assert rel.status_code == 202
                async with asyncio.timeout(8):
                    while True:
                        if admin.execute(
                            "SELECT applied_revision FROM publication.gateway_principals "
                            "WHERE cluster_id=%s",
                            (cid,),
                        ).fetchone() == (int(rel.json()["revision"]),):
                            break
                        await asyncio.sleep(0.03)
            else:
                create["model"] = "unbound-model"
            await send(ws, {**create, "previous_response_id": "bound"})
            error = await recv(ws)
            assert error["type"] == "error"
            assert len(received) == 1
        assert admin.execute("SELECT count(*) FROM billing.request_attempts").fetchone() == (1,)
        assert account_amounts(admin, org) == (
            95,
            0,
        )


async def test_ws_cancel_while_upstream_handshake_pending(http, admin, tls, relay_binary):  # noqa: F811
    dialing = asyncio.Event()
    release = asyncio.Event()
    upstream_messages = []

    async def delay_handshake(connection, request):
        dialing.set()
        await release.wait()

    async def provider(ws):
        async for msg in ws:
            upstream_messages.append(msg)

    async with (
        serve(provider, "127.0.0.1", 0, ssl=tls[0], process_request=delay_handshake) as upstream,
        stream_plane(
            http,
            admin,
            relay_binary,
            tls[1],
            upstream.sockets[0].getsockname()[1],
            "openai_compatible",
            ws=True,
        ) as (public, model, key, _, plane),
    ):
        org = plane[2]
        async with connect(
            f"ws://127.0.0.1:{public.base_url.port}/v1/responses",
            additional_headers={"authorization": f"Bearer {key}"},
            proxy=None,
        ) as ws:
            await send(ws, {"type": "response.create", "model": model, "input": "hi"})
            await asyncio.wait_for(dialing.wait(), 3)
            await send(ws, {"type": "response.cancel", "event_id": "cancel-dial"})
            # An ordered ping confirms the earlier cancel reached the session's reader.
            await (await ws.ping())
            release.set()
            error = await recv(ws)
            assert error["error"] == {
                "type": "invalid_request_error",
                "code": "cancelled_before_send",
                "message": "cancelled_before_send",
                "event_id": "cancel-dial",
            }
            await asyncio.wait_for(ws.wait_closed(), 5)
        assert not upstream_messages
        assert admin.execute("SELECT count(*) FROM billing.request_attempts").fetchone() == (0,)
        assert account_amounts(admin, org) == (
            100,
            0,
        )
