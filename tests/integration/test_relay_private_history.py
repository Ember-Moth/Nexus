"""Converted private state survives client replay and stays bound to its source account."""

import asyncio
import copy
import json
from uuid import uuid4

import pytest
from test_billing import headers, publish_price
from test_catalog import make_deployment
from test_identity_api import auth
from test_project_credentials import key as issue_key
from test_relay import relay_binary, running_relay, tls, wait_events  # noqa: F401
from test_relay_streams import stream_plane

pytestmark = pytest.mark.integration


def responses_events(full):
    yield {"type": "response.created", "response": {"id": full["id"], "status": "in_progress"}}
    for i, item in enumerate(full["output"]):
        if item["type"] == "compaction":
            yield {
                "type": "response.output_item.added",
                "output_index": i,
                "item": {"type": "compaction", "id": item["id"], "encrypted_content": ""},
            }
            yield {
                "type": "response.compaction.compacting",
                "output_index": i,
                "item_id": item["id"],
            }
        if item["type"] == "reasoning":
            yield {
                "type": "response.output_item.added",
                "output_index": i,
                "item": {
                    "type": "reasoning",
                    "id": item["id"],
                    "summary": [],
                    "encrypted_content": "incomplete",
                },
            }
            for j, p in enumerate(item["summary"]):
                loc = {"output_index": i, "summary_index": j, "item_id": item["id"]}
                yield {
                    "type": "response.reasoning_summary_part.added",
                    **loc,
                    "part": {"type": "summary_text", "text": ""},
                }
                yield {"type": "response.reasoning_summary_text.delta", **loc, "delta": p["text"]}
                yield {"type": "response.reasoning_summary_part.done", **loc, "part": p}
            if item.get("content"):
                yield {
                    "type": "response.reasoning_text.delta",
                    "output_index": i,
                    "content_index": 0,
                    "item_id": item["id"],
                    "delta": item["content"][0]["text"],
                }
        if item["type"] == "message":
            yield {
                "type": "response.output_item.added",
                "output_index": i,
                "item": item | {"content": [], "status": "in_progress"},
            }
            for j, part in enumerate(item["content"]):
                loc = {"output_index": i, "content_index": j, "item_id": item["id"]}
                yield {"type": "response.output_text.delta", **loc, "delta": part["text"]}
                yield {"type": "response.output_text.done", **loc, "text": part["text"]}
                for n, a in enumerate(part.get("annotations", [])):
                    yield {
                        "type": "response.output_text.annotation.added",
                        **loc,
                        "annotation_index": n,
                        "annotation": a,
                    }
        yield {"type": "response.output_item.done", "output_index": i, "item": item}
    terminal = copy.deepcopy(full)
    for item in terminal["output"]:
        if item["type"] == "reasoning":
            item.pop("encrypted_content", None)
    yield {"type": "response.completed", "response": terminal}


def messages_events(full):
    yield {
        "type": "message_start",
        "message": full
        | {
            "content": [],
            "stop_reason": None,
            "usage": {k: v for k, v in full["usage"].items() if k != "iterations"}
            | {"output_tokens": 0},
        },
    }
    for i, b in enumerate(full["content"]):
        if b["type"] == "thinking":
            yield {
                "type": "content_block_start",
                "index": i,
                "content_block": {"type": "thinking", "thinking": ""},
            }
            yield {
                "type": "content_block_delta",
                "index": i,
                "delta": {"type": "thinking_delta", "thinking": b["thinking"]},
            }
            yield {
                "type": "content_block_delta",
                "index": i,
                "delta": {"type": "signature_delta", "signature": b["signature"]},
            }
        elif b["type"] == "text" and b.get("citations"):
            yield {
                "type": "content_block_start",
                "index": i,
                "content_block": {"type": "text", "text": ""},
            }
            yield {
                "type": "content_block_delta",
                "index": i,
                "delta": {"type": "text_delta", "text": b["text"]},
            }
            for c in b["citations"]:
                yield {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "citations_delta", "citation": c},
                }
        else:
            yield {"type": "content_block_start", "index": i, "content_block": b}
        yield {"type": "content_block_stop", "index": i}
    yield {
        "type": "message_delta",
        "delta": {"stop_reason": full["stop_reason"], "stop_sequence": None},
        "usage": full["usage"],
    }
    yield {"type": "message_stop"}


def read_messages(frames):
    blocks = []
    for f in frames:
        if f["type"] == "content_block_start":
            assert f["index"] == len(blocks)
            blocks.append(f["content_block"])
        elif f["type"] == "content_block_delta":
            d = f["delta"]
            if d["type"] == "citations_delta":
                blocks[f["index"]].setdefault("citations", []).append(d["citation"])
                continue
            k = {
                "thinking_delta": "thinking",
                "signature_delta": "signature",
                "text_delta": "text",
            }[d["type"]]
            blocks[f["index"]][k] = blocks[f["index"]].get(k, "") + d[k]
    assert frames[-1]["type"] == "message_stop"
    return blocks


@pytest.mark.parametrize("family", ["anthropic", "openai_compatible"])
async def test_private_replay_and_scope(http, admin, tls, relay_binary, family):  # noqa: F811
    api, root, _, settings = http
    claude = family == "anthropic"
    endpoint = "openai_responses" if claude else "anthropic_messages"
    path = "/v1/responses" if claude else "/v1/messages"
    native = (
        [
            {
                "type": "thinking",
                "thinking": "Visible summary",
                "signature": "private-native-signature",
            },
            {"type": "redacted_thinking", "data": "private-native-redaction"},
            {"type": "text", "text": "answer"},
        ]
        if claude
        else [
            {
                "id": "rs_a",
                "role": "",
                "quality": "",
                "size": "",
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "First."},
                    {"type": "summary_text", "text": "Second."},
                ],
                "content": [{"type": "reasoning_text", "text": "private-native-content"}],
                "encrypted_content": "private-native-cipher-a",
                "status": "completed",
            },
            {
                "id": "rs_b",
                "type": "reasoning",
                "summary": [],
                "encrypted_content": "private-native-cipher-b",
            },
            {
                "id": "m",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "answer", "annotations": []}],
            },
        ]
    )
    sent = []
    betas = []
    compact = (
        {
            "type": "compaction",
            "content": "Provider summary.",
            "signature": "private-native-compaction",
            "tool_changes": [
                {
                    "type": "tool_addition",
                    "tool": {
                        "type": "tool_definition",
                        "definition": {
                            "name": "local",
                            "input_schema": {"type": "object", "properties": {}},
                        },
                    },
                }
            ],
        }
        if claude
        else {
            "type": "compaction",
            "id": "cmp_source",
            "encrypted_content": "private-native-compaction",
            "created_by": "source",
        }
    )

    citing = False
    source = "https://example.org/source"
    cited = (
        {
            "type": "text",
            "text": "日本",
            "citations": [
                {
                    "type": "char_location",
                    "document_index": 0,
                    "document_title": "note",
                    "start_char_index": 0,
                    "end_char_index": 2,
                    "cited_text": "日本",
                },
                {
                    "type": "web_search_result_location",
                    "url": source,
                    "title": "Report",
                    "cited_text": "quoted",
                    "encrypted_index": "private-native-index",
                },
            ],
        }
        if claude
        else {
            "type": "message",
            "role": "assistant",
            "id": "original_message",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": "First.", "annotations": []},
                {
                    "type": "output_text",
                    "text": "🌲日本",
                    "annotations": [
                        {
                            "type": "url_citation",
                            "start_index": 1,
                            "end_index": 3,
                            "url": source,
                            "title": "Report",
                        }
                    ],
                },
                {
                    "type": "output_text",
                    "text": "Fin.",
                    "annotations": [
                        {
                            "type": "url_citation",
                            "start_index": 0,
                            "end_index": 3,
                            "url": source,
                            "title": "Report",
                        }
                    ],
                },
            ],
        }
    )

    async def provider(reader, writer):
        try:
            lines = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            hdr = {k.lower(): v.strip() for s in lines[1:] if s for k, v in [s.split(":", 1)]}
            body = json.loads(await reader.readexactly(int(hdr["content-length"])))
            sent.append(body)
            betas.append(hdr.get("anthropic-beta"))
            if claude:
                result = {
                    "type": "message",
                    "model": body["model"],
                    "id": "p",
                    "role": "assistant",
                    "content": native,
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                }
            else:
                result = {
                    "object": "response",
                    "id": "p",
                    "model": body["model"],
                    "output": native,
                    "status": "completed",
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 0},
                    },
                }
            if citing:
                result["content" if claude else "output"] = [cited]
            if body.get("context_management"):
                result["content" if claude else "output"] = [compact, native[-1]]
                if claude:
                    u = result["usage"].copy()
                    result["usage"]["iterations"] = [
                        dict.fromkeys(u, 1)
                        | {
                            "type": "compaction",
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        },
                        u | {"type": "message"},
                    ]
            if body.get("stream"):
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Connection: close\r\n\r\n"
                )
                for event in messages_events(result) if claude else responses_events(result):
                    writer.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            else:
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Connection: close\r\n\r\n" + json.dumps(result).encode()
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
            convert_from=endpoint,
        ) as (relay, model, key, _, plane),
    ):

        def h(k):
            return {"Authorization": "Bearer " + k} if claude else {"x-api-key": k}

        start = {"role": "user", "content": "go"}
        opts = (
            {
                "input": [start],
                "reasoning": {"effort": "medium", "summary": "auto"},
                "max_output_tokens": 6,
            }
            if claude
            else {
                "messages": [start],
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": "medium"},
                "max_tokens": 6,
            }
        )
        request = {"model": model, **opts}
        hist = "input" if claude else "messages"
        for stream in [False, True]:
            result = await relay.post(path, headers=h(key), json=request | {"stream": stream})
            assert result.status_code == 200, result.text
            assert "private-native" not in result.text
            if stream:
                events = [
                    json.loads(line[6:])
                    for line in result.text.splitlines()
                    if line.startswith("data: ")
                ]
                blocks = events[-1]["response"]["output"] if claude else read_messages(events)
                if claude:
                    assert any(e["type"] == "response.reasoning_summary_text.delta" for e in events)
            else:
                blocks = result.json()["output" if claude else "content"]
            if claude:
                assert [b["type"] for b in blocks] == ["reasoning", "reasoning", "message"]
                assert blocks[0]["summary"] == [{"type": "summary_text", "text": "Visible summary"}]
                assert blocks[0]["encrypted_content"].startswith("nxctx1.")
                assert sent[-1]["thinking"] == {"type": "adaptive", "display": "summarized"}
            else:
                assert [b["type"] for b in blocks] == ["thinking", "redacted_thinking", "text"]
                assert blocks[0]["thinking"] == "First.\n\nSecond." and blocks[0][
                    "signature"
                ].startswith("nxctx1.")
                assert sent[-1]["include"] == ["reasoning.encrypted_content"]
            follow = request | {
                hist: [
                    start,
                    *(blocks if claude else [{"role": "assistant", "content": blocks}]),
                    {"role": "user", "content": "continue"},
                ]
            }
            # A fresh relay process with the same cluster key opens the state.
            async with running_relay(
                relay_binary,
                plane[0],
                plane[1],
                plane[7],
                settings,
                tls[1],
                plane[0].connection.info.port,
            ) as fresh:
                again = await fresh.post(path, headers=h(key), json=follow)
            assert again.status_code == 200, again.text
            if claude:
                assert sent[-1]["messages"][1]["content"][:2] == native[:2]
            else:
                assert sent[-1]["input"][1:3] == native[:2]
            assert "nxctx1." not in json.dumps(sent[-1])
        await wait_events(admin, 8)
        assert await http[2].state.usage.process() == 4
        assert admin.execute(
            "SELECT count(*) FROM billing.request_runs WHERE charged_amount=3"
        ).fetchone() == (4,)
        assert admin.execute(
            "SELECT count(*) FROM billing.request_attempts WHERE cost_amount=1.2"
        ).fetchone() == (4,)
        persisted = (
            json.dumps(admin.execute("SELECT envelope FROM billing.usage_events").fetchall())
            + relay.relay_log.read_text()
        )
        assert all(
            s not in persisted for s in ["private-native", "nxctx1.", "Visible summary", "First."]
        )

        openai_policy = [{"type": "compaction", "compact_threshold": 50000}]
        claude_policy = {
            "edits": [
                {"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": 50000}}
            ]
        }
        feature = {} if claude else {"anthropic-beta": "compact-2026-01-12"}
        for stream in [False, True]:
            r = await relay.post(
                path,
                headers=h(key) | feature,
                json=request
                | {
                    "context_management": openai_policy if claude else claude_policy,
                    "stream": stream,
                },
            )
            assert r.status_code == 200, r.text
            assert sent[-1]["context_management"] == (claude_policy if claude else openai_policy)
            if claude:
                assert betas[-1] == "compact-2026-01-12"
            if stream:
                frames = [
                    json.loads(line[6:])
                    for line in r.text.splitlines()
                    if line.startswith("data: ")
                ]
                compacted = frames[-1]["response"]["output"] if claude else read_messages(frames)
            else:
                compacted = r.json()["output" if claude else "content"]
            assert "private-native-compaction" not in json.dumps(compacted)
            if claude:
                assert compacted[0]["type"] == "compaction"
            else:
                assert compacted[0].pop("nexus_item_type") == "compaction"
                assert compacted[0]["type"] == "redacted_thinking"
            short = request | {
                hist: [
                    *(compacted if claude else [{"role": "assistant", "content": compacted}]),
                    {"role": "user", "content": "next"},
                ]
            }
            replay = await relay.post(path, headers=h(key) | feature, json=short)
            assert replay.status_code == 200, replay.text
            if claude:
                assert sent[-1]["messages"][0]["content"][0] == compact
                assert betas[-1] == "compact-2026-01-12"
            else:
                assert sent[-1]["input"][0] == compact
        await wait_events(admin, 16)
        assert await http[2].state.usage.process() == 4
        assert admin.execute(
            "SELECT count(*) FROM billing.request_runs WHERE reservation_state='captured'"
        ).fetchone() == (8,)

        citing = True
        source_input = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_file",
                        "filename": "note",
                        "file_data": "data:text/plain;base64,5pel5pys",
                        "citations": {"enabled": True},
                    },
                    {"type": "input_text", "text": "cite"},
                ],
            }
        ]
        for stream in [False, True]:
            q = request | {"stream": stream}
            if claude:
                q["input"] = source_input
            result = await relay.post(path, headers=h(key), json=q)
            assert result.status_code == 200, result.text
            if stream:
                frames = [
                    json.loads(line[6:])
                    for line in result.text.splitlines()
                    if line.startswith("data: ")
                ]
                blocks = frames[-1]["response"]["output"] if claude else read_messages(frames)
            else:
                blocks = result.json()["output" if claude else "content"]
            assert "private-native-index" not in json.dumps(blocks)
            if claude:
                assert sent[-1]["messages"][0]["content"][0]["citations"] == {"enabled": True}
                assert blocks[0]["nexus_citations"][0] == cited["citations"][0]
                assert blocks[0]["content"][0]["annotations"] == [
                    {
                        "type": "url_citation",
                        "url": source,
                        "title": "Report",
                        "start_index": 0,
                        "end_index": 2,
                    }
                ]
            else:
                assert [b["text"] for b in blocks] == ["First.", "🌲日本", "Fin."]
                c = blocks[1]["citations"][0]
                assert c["url"] == source and c["cited_text"] == "" and c["nexus_start_index"] == 1
                assert c["encrypted_index"].startswith("nxctx1.")
                assert blocks[2]["citations"][0]["encrypted_index"].startswith("nxref1.")
            cite_follow = request | {
                hist: [
                    *(source_input if claude else [start]),
                    *(blocks if claude else [{"role": "assistant", "content": blocks}]),
                    {"role": "user", "content": "continue"},
                ]
            }
            roundtrip = await relay.post(path, headers=h(key), json=cite_follow)
            assert roundtrip.status_code == 200, roundtrip.text
            assert (
                sent[-1]["messages"][1]["content"][0] if claude else sent[-1]["input"][1]
            ) == cited
        await wait_events(admin, 24)
        assert await http[2].state.usage.process() == 4

        citing = False
        visibility = (
            {"reasoning": {"effort": "low"}}
            if claude
            else {"thinking": {"type": "adaptive", "display": "omitted"}}
        )
        for stream in [False, True]:
            hidden = await relay.post(
                path, headers=h(key), json=request | visibility | {"stream": stream}
            )
            assert hidden.status_code == 200, hidden.text
            assert all(
                text not in hidden.text
                for text in ["Visible summary", "First.", "Second.", "private-native"]
            )
            assert "nxctx1." in hidden.text

        async def rejected(body, k=key):
            before = len(sent)
            reserved = admin.execute("SELECT count(*) FROM billing.request_runs").fetchone()
            r = await relay.post(path, headers=h(k), json=body)
            assert (
                r.status_code == 400 and r.json()["error"]["message"] == "invalid_private_history"
            ), r.text
            assert (
                len(sent) == before
                and admin.execute("SELECT count(*) FROM billing.request_runs").fetchone()
                == reserved
            )
            return r

        await rejected(follow | {"model": "different-alias"})
        q = copy.deepcopy(cite_follow)
        if claude:
            q[hist][1]["nexus_citations"][0]["document_title"] = "different"
        else:
            q[hist][1]["content"][0]["text"] = "different"
        await rejected(q)
        orphan = copy.deepcopy(cite_follow)
        if claude:
            del orphan[hist][1]["nexus_history"]
        else:
            del orphan[hist][1]["content"][1]
        await rejected(orphan)

        # A genuine capsule placed in the other public protocol cannot be used
        # to forward private material back through a native route.
        secret = (
            follow[hist][1]["encrypted_content"]
            if claude
            else follow[hist][1]["content"][0]["signature"]
        )
        wrong_body = (
            {
                "model": model,
                "max_tokens": 6,
                "messages": [
                    start,
                    {
                        "role": "assistant",
                        "content": [{"type": "redacted_thinking", "data": secret}],
                    },
                    {"role": "user", "content": "next"},
                ],
            }
            if claude
            else {
                "model": model,
                "input": [
                    start,
                    {
                        "type": "reasoning",
                        "id": "external",
                        "summary": [],
                        "encrypted_content": secret,
                    },
                    {"role": "user", "content": "next"},
                ],
            }
        )
        other_path = "/v1/messages" if claude else "/v1/responses"
        other_auth = {"x-api-key": key} if claude else {"Authorization": "Bearer " + key}
        denied = await relay.post(other_path, headers=other_auth, json=wrong_body)
        assert (
            denied.status_code == 400
            and denied.json()["error"]["message"] == "invalid_private_history"
        )
        for tamper in ["visible", "cipher"]:
            q = copy.deepcopy(follow)
            b = q[hist][1] if claude else q[hist][1]["content"][0]
            if tamper == "visible":
                if claude:
                    b["summary"][0]["text"] = "altered"
                else:
                    b["thinking"] = "altered"
            else:
                f = "encrypted_content" if claude else "signature"
                t = b[f]
                b[f] = t[:12] + ("B" if t[12] == "A" else "A") + t[13:]
            await rejected(q)
        other = (
            await issue_key(
                api, root, plane[2], str(plane[5].service_account_id), name="second-active"
            )
        ).json()["secret"]

        async def publish():
            r = await api.post(
                f"/api/v1/gateway-clusters/{plane[7]}/releases",
                headers=headers(root, uuid4().hex),
                json={},
            )
            assert r.status_code == 202
            revision = int(r.json()["revision"])
            async with asyncio.timeout(15):
                while True:
                    current = admin.execute(
                        "SELECT applied_revision FROM publication.gateway_principals "
                        "WHERE cluster_id=%s",
                        (plane[7],),
                    ).fetchone()[0]
                    if current == revision:
                        return
                    await asyncio.sleep(0.05)

        await publish()
        assert (
            await relay.get("/v1/models", headers={"Authorization": "Bearer " + other})
        ).status_code == 200
        await rejected(follow, other)
        await rejected(cite_follow, other)
        dep = plane[4]
        # Prefer a second deployment for ordinary generation. Existing state
        # must still route to the source while it is authorized and reachable.
        alt = await make_deployment(
            api,
            root,
            plane[3],
            {"id": dep["upstream_id"]},
            {"id": dep["credential_id"]},
            name="alternative",
        )
        await publish_price(api, root, "cost", alt, "0.4")
        original = (await api.get(f"/api/v1/deployments/{dep['id']}", headers=auth(root))).json()
        edited = await api.patch(
            f"/api/v1/deployments/{alt['id']}",
            headers=auth(root, alt["version"]),
            json={"upstream_model": "other/model", "relay_profile": original["relay_profile"]},
        )
        assert edited.status_code == 200, edited.text
        cluster_url = f"/api/v1/gateway-clusters/{plane[7]}"
        cluster_version = admin.execute(
            "SELECT version FROM publication.gateway_clusters WHERE id=%s", (plane[7],)
        ).fetchone()[0]
        assert (
            await api.patch(
                cluster_url,
                headers=auth(root, cluster_version),
                json={"deployment_ids": [dep["id"], alt["id"]]},
            )
        ).status_code == 200
        route_url = f"/api/v1/route-policies/{plane[2]['id']}/{plane[3]['id']}"
        changed = await api.put(
            route_url,
            headers=auth(root, 1),
            json={
                "candidates": [
                    {"deployment_id": alt["id"], "priority": 0},
                    {"deployment_id": dep["id"], "priority": 1},
                ]
            },
        )
        assert changed.status_code == 200, changed.text
        await publish()
        citing = False
        assert (await relay.post(path, headers=h(key), json=request)).status_code == 200
        assert sent[-1]["model"] == "other/model"
        assert (await relay.post(path, headers=h(key), json=follow)).status_code == 200
        assert sent[-1]["model"] == original["upstream_model"]
        cred = await api.post(
            f"/api/v1/upstreams/{dep['upstream_id']}/credentials",
            headers=auth(root),
            json={"name": "rotated", "secret": "rotated-test-key"},
        )
        assert cred.status_code == 201
        url = f"/api/v1/deployments/{dep['id']}"
        v = (await api.get(url, headers=auth(root))).json()["version"]
        assert (
            await api.patch(url, headers=auth(root, v), json={"credential_id": cred.json()["id"]})
        ).status_code == 200
        before = len(sent)
        runs_before = admin.execute("SELECT count(*) FROM billing.request_runs").fetchone()
        stale = await relay.post(path, headers=h(key), json=follow)
        assert stale.status_code in (400, 403, 503) and len(sent) == before, stale.text
        assert admin.execute("SELECT count(*) FROM billing.request_runs").fetchone() == runs_before
        await publish()
        await rejected(follow)
