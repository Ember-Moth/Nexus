import asyncio
import json
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from test_billing import headers, publish_discount, publish_price
from test_identity_api import auth
from test_relay import relay_binary, tls, wait_events  # noqa: F401
from test_relay_streams import stream_plane

pytestmark = pytest.mark.integration


async def configure_search(http, admin, plane, variant="standard"):
    client, root, app, _ = http
    _, _, _, model, dep, _, _, cluster = plane
    changed = await client.patch(
        f"/api/v1/models/{model['id']}",
        headers=auth(root, model["version"]),
        json={"tool_dimensions": ["web_search_requests"]},
    )
    assert changed.status_code == 200, changed.text
    rates = {
        "input_uncached_tokens": "1",
        "output_tokens": "1",
        "web_search_requests": "5" if variant == "preview" else "3",
    }
    await publish_price(client, root, "sale", model, dimensions=rates)
    await publish_price(client, root, "cost", dep, "0.4", dimensions=rates)
    url = f"/api/v1/deployments/{dep['id']}"
    current = (await client.get(url, headers=auth(root))).json()
    changed = await client.patch(
        url,
        headers=auth(root, current["version"]),
        json={
            "relay_profile": current["relay_profile"]
            | {
                "web_search_ceiling": 2,
                "web_search_variant": variant,
                "provider_features": [*current["relay_profile"]["provider_features"], "web_search"],
            }
        },
    )
    assert changed.status_code == 200, changed.text
    node, role, caps = admin.execute(
        "SELECT node_id,database_role,capabilities FROM publication.gateway_principals "
        "WHERE cluster_id=%s",
        (cluster,),
    ).fetchone()
    published = await client.post(
        f"/api/v1/gateway-clusters/{cluster}/releases",
        headers=headers(root, "old-search-relay"),
        json={},
    )
    assert published.status_code == 409
    changed = await client.put(
        f"/api/v1/gateway-clusters/{cluster}/principals/{node}",
        headers=auth(root),
        json={
            "node_id": str(node),
            "database_role": role,
            "capabilities": [*caps, "relay.web-search.v1"],
        },
    )
    assert changed.status_code == 200, changed.text
    published = await client.post(
        f"/api/v1/gateway-clusters/{cluster}/releases", headers=headers(root, "search"), json={}
    )
    assert published.status_code == 202, published.text
    while await app.state.publication.distribute():
        pass
    async with asyncio.timeout(10):
        while admin.execute(  # noqa: ASYNC110 - wait for the published snapshot
            "SELECT applied_revision FROM publication.gateway_principals WHERE node_id=%s", (node,)
        ).fetchone()[0] != int(published.json()["revision"]):
            await asyncio.sleep(0.05)


@pytest.mark.parametrize(
    ("family", "variant", "chat"),
    [
        ("anthropic", "standard", False),
        ("openai_compatible", "standard", False),
        ("openai_compatible", "standard", True),
        ("openai_compatible", "preview", False),
    ],
)
async def test_search_to_ledger(http, admin, tls, relay_binary, family, variant, chat):  # noqa: F811
    messages = family == "anthropic"
    mode = "ok"
    sent = []

    async def provider(reader, writer):
        try:
            lines = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            hdr = {
                k.lower(): v.strip() for line in lines[1:] if line for k, v in [line.split(":", 1)]
            }
            body = json.loads(await reader.readexactly(int(hdr["content-length"])))
            sent.append(body)
            n = 3 if mode == "over" else 1
            # After two searches the prompt has been read three times.
            prompt = 11 if mode == "loop" else 2
            if messages:
                usage = {
                    "input_tokens": prompt,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                }
                if mode != "ambiguous":
                    usage["server_tool_use"] = {"web_search_requests": n}
                answer = {
                    "id": "msg_search",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": usage,
                    "stop_reason": "end_turn",
                }
                events = [
                    {
                        "type": "message_start",
                        "message": answer | {"usage": usage | {"output_tokens": 0}},
                    },
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn"},
                        "usage": {"output_tokens": 1},
                    },
                    {"type": "message_stop"},
                ]
            else:
                items = [
                    {
                        "type": "web_search_call",
                        "id": f"ws_{i}",
                        "status": "completed",
                        "action": {"type": "search", "queries": ["one", "two"]},
                    }
                    for i in range(n)
                ]
                items.append(
                    {
                        "type": "web_search_call",
                        "id": "ws_open",
                        "status": "completed",
                        "action": {"type": "open_page"},
                    }
                )
                if mode == "ambiguous":
                    items[-1]["action"]["type"] = "unknown_action"
                if mode == "chat":
                    a = {
                        "type": "url_citation",
                        "url": "https://example.org/sky",
                        "title": "Sky",
                        "start_index": 0,
                        "end_index": 2,
                    }
                    items.append(
                        {
                            "id": "msg_search",
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": "🌲", "annotations": []},
                                {"type": "output_text", "text": "京都", "annotations": [a]},
                            ],
                        }
                    )
                answer = {
                    "id": "resp_search",
                    "object": "response",
                    "status": "completed",
                    "output": items,
                    "usage": {
                        "input_tokens": prompt,
                        "output_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 0},
                    },
                }
                events = [
                    {
                        "type": "response.created",
                        "response": {"id": answer["id"], "status": "in_progress"},
                    },
                    *(
                        {"type": "response.output_item.done", "output_index": n, "item": i}
                        for n, i in enumerate(items)
                    ),
                    {"type": "response.completed", "response": answer},
                ]
            if body.get("stream"):
                wire = "".join(
                    f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events
                ).encode()
                mime = "text/event-stream"
            else:
                wire = json.dumps(answer).encode()
                mime = "application/json"
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
            family,
            features=["batches"] if family == "anthropic" else [],
        ) as (relay, model, key, _, plane),
    ):
        path = "/v1/messages" if messages else "/v1/responses"
        h = {"x-api-key": key} if messages else {"Authorization": "Bearer " + key}
        tool = (
            {"type": "web_search_20250305", "name": "web_search"}
            if messages
            else {"type": "web_search_preview" if variant == "preview" else "web_search"}
        )
        opts = (
            {"messages": [{"role": "user", "content": "search"}], "max_tokens": 4}
            if messages
            else {"input": "search", "max_output_tokens": 4}
        )
        request = {"model": model, "tools": [tool], **opts}
        assert (await relay.post(path, headers=h, json=request)).status_code == 400
        await configure_search(http, admin, plane, variant)
        too_many = (
            (request | {"tools": [tool | {"max_uses": 3}]})
            if messages
            else request | {"max_tool_calls": 3}
        )
        assert (await relay.post(path, headers=h, json=too_many)).status_code == 400
        if not messages:
            wrong_variant = request | {
                "tools": [{"type": "web_search" if variant == "preview" else "web_search_preview"}]
            }
            assert (await relay.post(path, headers=h, json=wrong_variant)).status_code == 400
        revision = admin.execute(
            "SELECT desired_revision FROM publication.gateway_clusters WHERE id=%s", (plane[7],)
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.RaiseException, match="deployment_unavailable"):
            await plane[0].connection.execute(
                "SELECT gateway_v1.reserve_search_request(%s,%s,%s,%s,4,4,3)",
                (uuid4(), plane[5].api_key_id, plane[3]["id"], revision),
            )

        assert not sent and admin.execute(
            "SELECT count(*) FROM billing.request_runs"
        ).fetchone() == (0,)
        if chat:
            mode = "chat"
            q = {
                "model": model,
                "messages": [{"role": "user", "content": "search"}],
                "max_tokens": 4,
                "web_search_options": {
                    "search_context_size": "low",
                    "user_location": {"type": "approximate", "approximate": {"city": "Kyoto"}},
                },
            }
            annotation = {
                "type": "url_citation",
                "url_citation": {
                    "url": "https://example.org/sky",
                    "title": "Sky",
                    "start_index": 1,
                    "end_index": 3,
                },
            }
            for n, stream in enumerate([False, True], 1):
                result = await relay.post(
                    "/v1/chat/completions", headers=h, json=q | {"stream": stream}
                )
                assert result.status_code == 200, result.text
                assert sent[-1]["tools"] == [
                    {
                        "type": "web_search",
                        "search_context_size": "low",
                        "user_location": {"type": "approximate", "city": "Kyoto"},
                    }
                ]
                assert sent[-1]["max_tool_calls"] == 2 and sent[-1]["tool_choice"] == {
                    "type": "web_search"
                }
                if stream:
                    frames = [
                        json.loads(line[6:])
                        for line in result.text.splitlines()
                        if line.startswith("data: ") and "[DONE]" not in line
                    ]
                    deltas = [v["choices"][0]["delta"] for v in frames]
                    assert "".join(d.get("content", "") for d in deltas) == "🌲京都"
                    assert [a for d in deltas for a in d.get("annotations", [])] == [annotation]
                    assert sent[-1]["input"][1]["content"][0]["annotations"] == [
                        {"type": "url_citation", **annotation["url_citation"]}
                    ]
                else:
                    msg = result.json()["choices"][0]["message"]
                    assert msg["content"] == "🌲京都" and msg["annotations"] == [annotation]
                    q["messages"].extend([msg, {"role": "user", "content": "next"}])
                await wait_events(admin, n * 2)
                assert await http[2].state.usage.process() == 1
                assert admin.execute(
                    "SELECT web_search_limit,reserved_amount,charged_amount "
                    "FROM billing.request_runs ORDER BY created_at DESC LIMIT 1"
                ).fetchone() == (2, 22, 6)
                assert admin.execute(
                    "SELECT cost_amount FROM billing.request_attempts "
                    "ORDER BY created_at DESC LIMIT 1"
                ).fetchone() == (Decimal("2.4"),)
            return
        for i, stream in enumerate([False, True]):
            reply = await relay.post(path, headers=h, json=request | {"stream": stream})
            assert reply.status_code == 200, reply.text
            if stream:
                assert (
                    "event: message_stop" if messages else "event: response.completed"
                ) in reply.text
            assert (
                sent[-1]["tools"][0]["max_uses"] if messages else sent[-1]["max_tool_calls"]
            ) == 2
            await wait_events(admin, 2 * (i + 1))
            assert await http[2].state.usage.process() == 1
        assert (
            admin.execute(
                "SELECT web_search_limit,reserved_amount,charged_amount FROM billing.request_runs"
            ).fetchall()
            == ([(2, 26, 8)] if variant == "preview" else [(2, 22, 6)]) * 2
        )
        assert (
            admin.execute("SELECT cost_amount FROM billing.request_attempts").fetchall()
            == [(Decimal("3.2") if variant == "preview" else Decimal("2.4"),)] * 2
        )
        mode = "ambiguous"
        missing = await relay.post(path, headers=h, json=request)
        assert missing.status_code == 200, missing.text
        await wait_events(admin, 6)
        assert await http[2].state.usage.process() == 1
        assert admin.execute(
            "SELECT state FROM billing.request_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone() == ("review",)
        mode = "over"
        too_much = await relay.post(path, headers=h, json=request)
        assert too_much.status_code == 200, too_much.text
        await wait_events(admin, 8)
        assert await http[2].state.usage.process() == 1
        assert admin.execute(
            "SELECT review_reason FROM billing.request_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone() == ("authorized_bound_exceeded",)
        # The extra search is held for review, yet the hold covers it: the wallet stays open.
        org_id = plane[2]["id"]
        assert admin.execute(
            "SELECT status FROM billing.accounts WHERE organization_id=%s", (org_id,)
        ).fetchone() == ("active",)
        mode = "loop"
        looped = await relay.post(path, headers=h, json=request)
        assert looped.status_code == 200, looped.text
        await wait_events(admin, 10)
        assert await http[2].state.usage.process() == 1
        price = 5 if variant == "preview" else 3
        assert admin.execute(
            "SELECT state,input_limit,charged_amount FROM billing.request_runs "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone() == ("settled", 12, 11 + 1 + price)


async def test_batch_search_price_is_independent_of_token_discount(http, admin, tls, relay_binary):  # noqa: F811
    root = "/v1/messages/batches"
    submitted = []
    pid = "msgbatch_search"

    async def provider(reader, writer):
        try:
            lines = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = lines[0].split()
            hdr = {k.lower(): v.strip() for row in lines[1:] if row for k, v in [row.split(":", 1)]}
            raw = await reader.readexactly(int(hdr.get("content-length", 0)))
            if method == "POST":
                submitted.append(json.loads(raw))
            if path.endswith("/results"):
                usage = {
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "server_tool_use": {"web_search_requests": 1},
                }
                value = {
                    "custom_id": "find",
                    "result": {
                        "type": "succeeded",
                        "message": {
                            "type": "message",
                            "id": "msg",
                            "role": "assistant",
                            "content": [{"type": "text", "text": "found"}],
                            "stop_reason": "end_turn",
                            "usage": usage,
                        },
                    },
                }
                mime = "application/x-jsonl"
            else:
                value = {
                    "type": "message_batch",
                    "id": pid,
                    "processing_status": "ended",
                    "request_counts": {
                        "processing": 0,
                        "succeeded": 1,
                        "canceled": 0,
                        "errored": 0,
                        "expired": 0,
                    },
                    "results_url": f"{root}/{pid}/results",
                }
                mime = "application/json"
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
            "anthropic",
            features=["batches"],
        ) as (relay, model, key, _, plane),
    ):
        await configure_search(http, admin, plane)
        await publish_discount(http[0], http[1], plane[2], "0.8")
        params = {
            "model": model,
            "max_tokens": 4,
            "messages": [{"role": "user", "content": "find it"}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
        }
        res = await relay.post(
            root,
            headers={"x-api-key": key},
            json={"requests": [{"custom_id": "find", "params": params}]},
        )
        assert res.status_code == 200, res.text
        await wait_events(admin, 2)
        assert submitted[0]["requests"][0]["params"]["tools"][0]["max_uses"] == 1
        assert await http[2].state.usage.process() == 1
        assert admin.execute(
            "SELECT web_search_limit,reserved_amount,charged_amount FROM billing.request_runs"
        ).fetchone() == (1, Decimal("7.2"), Decimal("3.6"))
        assert admin.execute("SELECT cost_amount FROM billing.request_attempts").fetchone() == (
            Decimal("1.8"),
        )
