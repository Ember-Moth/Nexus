"""Native Claude compaction receipts and replay of provider-signed history."""

import asyncio
import json
from decimal import Decimal
from uuid import UUID

import pytest
from test_relay import relay_binary, tls, wait_events  # noqa: F401
from test_relay_streams import stream_plane

pytestmark = pytest.mark.integration


async def test_messages_compaction_iterations_and_history(http, admin, tls, relay_binary):  # noqa: F811
    calls = []
    history = []
    mode = "on_demand"
    block = {
        "type": "compaction",
        "content": "The dispatch identifier is AB42.",
        "signature": "signed-context-replay-fixture",
    }
    compact = {
        "type": "compaction",
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_read_input_tokens": 1,
        "cache_creation_input_tokens": 1,
        "cache_creation": {"ephemeral_5m_input_tokens": 1, "ephemeral_1h_input_tokens": 0},
    }

    async def upstream(reader, writer):
        try:
            lines = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            hdr = {
                k.lower(): v.strip() for line in lines[1:] if line for k, v in [line.split(":", 1)]
            }
            req = json.loads(await reader.readexactly(int(hdr["content-length"])))
            calls.append((hdr["anthropic-beta"], req))
            message = {
                "model": req["model"],
                "type": "message",
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            }
            top = {k: v for k, v in message.items() if k.endswith("_tokens")}
            content = [{"type": "text", "text": "AB42"}]
            reason = "end_turn"
            if mode in {"on_demand", "missing", "conflict"}:
                top = dict.fromkeys(top, 0)
                top["iterations"] = [compact]
                content = [block]
                reason = "compaction"
            elif mode == "threshold":
                top["iterations"] = [compact, message]
                content.insert(0, block)
            else:
                top["iterations"] = [message]
            if mode == "conflict":
                top["input_tokens"] = 1
            if mode == "missing":
                del top["iterations"]
            top["output_tokens_details"] = {"thinking_tokens": top["output_tokens"]}
            msg = {
                "type": "message",
                "role": "assistant",
                "id": "msg_compaction",
                "model": req["model"],
                "content": content,
                "stop_reason": reason,
                "stop_sequence": None,
                "usage": top,
            }
            if req.get("stream"):
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    b"Connection: close\r\n\r\n"
                )
                events = [
                    {
                        "type": "message_start",
                        "message": msg
                        | {
                            "content": [],
                            "stop_reason": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0, "iterations": []},
                        },
                    }
                ]
                for index, item in enumerate(content):
                    events.extend(
                        [
                            {"type": "content_block_start", "index": index, "content_block": item},
                            {"type": "content_block_stop", "index": index},
                        ]
                    )
                events.extend(
                    [
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": reason, "stop_sequence": None},
                            "usage": top,
                        },
                        {"type": "message_stop"},
                    ]
                )
                for event in events:
                    writer.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            else:
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Connection: close\r\n\r\n" + json.dumps(msg).encode()
                )
            await writer.drain()
        finally:
            writer.close()

    peer = await asyncio.start_server(upstream, "127.0.0.1", 0, ssl=tls[0])
    async with (
        peer,
        stream_plane(
            http,
            admin,
            relay_binary,
            tls[1],
            peer.sockets[0].getsockname()[1],
            "anthropic",
            auto_cache=True,
        ) as (relay, model, key, _, _),
    ):
        for i, (fixture, stream, sale, cost) in enumerate(
            [
                ("on_demand", False, "4.35", "1.74"),
                ("threshold", True, "6.35", "2.54"),
                ("follow", True, "2", "0.8"),
                ("missing", False, None, None),
                ("conflict", True, None, None),
            ],
            1,
        ):
            mode = fixture
            beta = "compact-2026-01-12" if mode == "threshold" else "compact-2026-09-04"
            initial = [{"role": "user", "content": "The dispatch identifier is AB42."}]
            body = {"model": model, "messages": initial, "max_tokens": 6, "stream": stream}
            if mode == "threshold":
                body["context_management"] = {"edits": [{"type": "compact_20260112"}]}
            elif mode == "follow":
                body["messages"] = [
                    {"role": "assistant", "content": history},
                    {"role": "user", "content": "Give the identifier."},
                ]
            else:
                body["compaction"] = {"type": "summarize"}
            res = await relay.post(
                "/v1/messages", json=body, headers={"x-api-key": key, "anthropic-beta": beta}
            )
            assert res.status_code == 200, res.text
            if stream:
                frames = [
                    json.loads(s[6:]) for s in res.text.splitlines() if s.startswith("data: ")
                ]
                assert frames[-1]["type"] == "message_stop"
                if mode == "threshold":
                    assert frames[1]["content_block"] == block
            else:
                assert res.json()["stop_reason"] == "compaction"
                assert res.json()["content"] == [block]
                if mode == "on_demand":
                    history = res.json()["content"]
            assert calls[-1][0] == beta
            if mode == "follow":
                assert calls[-1][1]["messages"][0]["content"] == history
            rid = UUID(res.headers["x-request-id"])
            await wait_events(admin, 2 * i)
            assert await http[2].state.usage.process() == 1
            amounts = admin.execute(
                """SELECT r.charged_amount,a.cost_amount,r.state FROM billing.request_runs r
                        JOIN billing.request_attempts a ON a.request_id=r.id WHERE r.id=%s""",
                (rid,),
            ).fetchone()
            if sale:
                assert amounts == (Decimal(sale), Decimal(cost), "settled")
                raw = admin.execute(
                    "SELECT envelope->'payload'->'raw_usage' FROM billing.usage_events "
                    "WHERE kind='attempt.finished' AND request_id=%s",
                    (rid,),
                ).fetchone()[0]
                assert raw.get("compaction_output_tokens", 0) == (0 if mode == "follow" else 2)
                assert raw["output_tokens"] == {"follow": 1, "threshold": 3, "on_demand": 2}[mode]
            else:
                assert amounts[1] is None and amounts[2] == "review"
        envelopes = admin.execute("SELECT envelope FROM billing.usage_events").fetchall()
        assert (
            block["signature"] not in json.dumps(envelopes)
            and block["signature"] not in relay.relay_log.read_text()
        )
