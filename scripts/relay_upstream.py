"""Local TLS upstream double for Relay load runs.

One TLS port serves OpenAI Responses (JSON, SSE and the WebSocket mode) and Anthropic
Messages (JSON, SSE and count_tokens) with HTTP/1.1 keep-alive. The path prefix before
``/v1/`` selects a lane, so two deployments can point at ``/a/v1`` and ``/b/v1`` of the same
process. A plain-HTTP control port on 127.0.0.1 changes lane behaviour and returns counters:

    POST /lanes   {"a": {"latency_ms": 200}}  merge settings into lanes
    POST /reset   clear counters and per-request records
    GET  /stats   counters, connection gauges and per-request records

Every request whose body contains ``lt:<token>`` is recorded with its arrival and completion
times (``time.monotonic`` is system-wide on macOS and Linux, so a client in another process can
compare it with its own send time). Started by ``tests/integration/relay_load_kit.py``; prints one
JSON line with both ports when ready.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import re
import ssl
import struct
import sys
import time
from collections import Counter

import uvloop

LANE_DEFAULTS = {
    "latency_ms": 0,  # before response headers, or before the first WebSocket event
    "events": 4,  # text deltas per streamed generation
    "interval_ms": 0,  # pause between streamed deltas
    "delta_bytes": 16,  # text per delta; JSON replies carry events * delta_bytes
    "input_tokens": 2,
    "output_tokens": 3,  # reported usage; keep within the deployment's output ceiling
    "error_rate": 0.0,  # deterministic share of generations answered with error_status
    "error_status": 503,  # 429 or 503 with the documented vendor envelope (failover proof)
}
MARKER = re.compile(rb"lt:[A-Za-z0-9_.-]+")
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class State:
    def __init__(self):
        self.lanes = {"default": dict(LANE_DEFAULTS)}
        self.reset()

    def reset(self):
        self.counts = Counter()
        self.records = []
        self.served = Counter()  # per lane, for the deterministic error share
        self.http_open = 0
        self.http_peak = 0
        self.ws_open = 0
        self.ws_peak = 0

    def lane(self, name):
        return self.lanes.get(name) or self.lanes["default"]

    def failing(self, name):
        cfg = self.lane(name)
        n = self.served[name]
        self.served[name] += 1
        rate = float(cfg["error_rate"])
        return int((n + 1) * rate) > int(n * rate)


STATE = State()


def openai_usage(cfg):
    return {
        "input_tokens": cfg["input_tokens"],
        "output_tokens": cfg["output_tokens"],
        "total_tokens": cfg["input_tokens"] + cfg["output_tokens"],
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 0},
    }


def anthropic_usage(cfg, output=None):
    return {
        "input_tokens": cfg["input_tokens"],
        "output_tokens": cfg["output_tokens"] if output is None else output,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def error_body(kind, status):
    if kind == "messages":
        name = "rate_limit_error" if status == 429 else "overloaded_error"
        return {"type": "error", "error": {"type": name, "message": "synthetic"}}
    if status == 429:
        return {
            "error": {"type": "requests", "code": "rate_limit_exceeded", "message": "synthetic"}
        }
    return {
        "error": {
            "type": "service_unavailable_error",
            "code": "server_is_overloaded",
            "message": "synthetic",
        }
    }


def responses_events(cfg, n):
    rid = f"resp_{n}"
    text = "x" * cfg["delta_bytes"]
    yield {"type": "response.created", "response": {"id": rid, "status": "in_progress"}}
    for _ in range(cfg["events"]):
        yield {
            "type": "response.output_text.delta",
            "item_id": f"msg_{n}",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        }
    yield {
        "type": "response.completed",
        "response": {"id": rid, "status": "completed", "usage": openai_usage(cfg)},
    }


def messages_events(cfg, n):
    text = "x" * cfg["delta_bytes"]
    yield {
        "type": "message_start",
        "message": {
            "id": f"msg_{n}",
            "type": "message",
            "role": "assistant",
            "content": [],
            "usage": anthropic_usage(cfg, 0),
        },
    }
    yield {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
    for _ in range(cfg["events"]):
        yield {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        }
    yield {"type": "content_block_stop", "index": 0}
    yield {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": cfg["output_tokens"]},
    }
    yield {"type": "message_stop"}


def json_reply(kind, cfg, n):
    text = "x" * (cfg["delta_bytes"] * cfg["events"])
    if kind == "messages":
        return {
            "id": f"msg_{n}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": anthropic_usage(cfg),
        }
    return {
        "id": f"resp_{n}",
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": f"msg_{n}",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": openai_usage(cfg),
    }


def sse(event):
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


def chunk(data):
    return f"{len(data):x}\r\n".encode() + data + b"\r\n"


async def read_request(reader):
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError, ConnectionError:
        return None
    lines = head.decode("latin-1").split("\r\n")
    method, path, _ = lines[0].split(" ", 2)
    headers = {}
    for line in lines[1:]:
        if line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    if "content-length" in headers:
        body = await reader.readexactly(int(headers["content-length"]))
    elif headers.get("transfer-encoding", "").lower() == "chunked":
        parts = []
        while True:
            size = int((await reader.readuntil(b"\r\n")).split(b";")[0], 16)
            data = await reader.readexactly(size + 2)
            if not size:
                break
            parts.append(data[:-2])
        body = b"".join(parts)
    else:
        body = b""
    return method, path, headers, body


def route(path):
    lane, _, rest = path.partition("/v1/")
    return (lane.strip("/") or "default"), "/v1/" + rest.split("?", 1)[0]


def record(lane, kind, stream, body, arrived, status):
    marker = MARKER.search(body)
    entry = {
        "lane": lane,
        "kind": kind,
        "stream": stream,
        "marker": marker.group().decode() if marker else None,
        "arrived": arrived,
        "status": status,
    }
    STATE.records.append(entry)
    return entry


async def respond(writer, status, payload, *, reason="OK"):
    data = json.dumps(payload).encode()
    writer.write(
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(data)}\r\n\r\n".encode()
        + data
    )
    await writer.drain()


async def generate(writer, lane, kind, body, arrived):
    request = json.loads(body)
    stream = bool(request.get("stream"))
    cfg = STATE.lane(lane)
    n = STATE.counts["generations"] = STATE.counts["generations"] + 1
    STATE.counts[f"{lane}:{kind}:{'sse' if stream else 'json'}"] += 1
    if cfg["latency_ms"]:
        await asyncio.sleep(cfg["latency_ms"] / 1000)
    if STATE.failing(lane):
        status = int(cfg["error_status"])
        entry = record(lane, kind, stream, body, arrived, status)
        STATE.counts[f"{lane}:error_{status}"] += 1
        await respond(writer, status, error_body(kind, status), reason="Error")
        entry["done"] = time.monotonic()
        entry["done_wall"] = time.time()
        return
    entry = record(lane, kind, stream, body, arrived, 200)
    if not stream:
        await respond(writer, 200, json_reply(kind, cfg, n))
        entry["done"] = time.monotonic()
        entry["done_wall"] = time.time()
        return
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
    )
    events = responses_events(cfg, n) if kind == "responses" else messages_events(cfg, n)
    interval = cfg["interval_ms"] / 1000
    for i, event in enumerate(events):
        if i > 1 and interval:
            await asyncio.sleep(interval)
        writer.write(chunk(sse(event)))
        await writer.drain()
    writer.write(b"0\r\n\r\n")
    await writer.drain()
    entry["done"] = time.monotonic()
    entry["done_wall"] = time.time()


# Minimal RFC 6455 server framing: the relay is the only client and sends masked text frames.
async def ws_read(reader):
    first, second = await reader.readexactly(2)
    opcode, length = first & 0x0F, second & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", await reader.readexactly(2))
    elif length == 127:
        (length,) = struct.unpack("!Q", await reader.readexactly(8))
    mask = await reader.readexactly(4) if second & 0x80 else b""
    data = await reader.readexactly(length)
    if mask:
        key = int.from_bytes((mask * (length // 4 + 1))[:length], "big")
        data = (int.from_bytes(data, "big") ^ key).to_bytes(length, "big")
    return first & 0x80, opcode, data


def ws_frame(opcode, data):
    n = len(data)
    if n < 126:
        head = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return head + data


async def websocket(reader, writer, lane, headers):
    accept = base64.b64encode(
        hashlib.sha1(headers["sec-websocket-key"].encode() + WS_GUID).digest()
    ).decode()
    writer.write(
        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        + f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
    )
    await writer.drain()
    STATE.counts["ws_accepted"] += 1
    STATE.ws_open += 1
    STATE.ws_peak = max(STATE.ws_peak, STATE.ws_open)
    turns = 0
    message = b""
    try:
        while True:
            fin, opcode, data = await ws_read(reader)
            if opcode == 0x8:
                writer.write(ws_frame(0x8, data[:2]))
                await writer.drain()
                return
            if opcode == 0x9:
                writer.write(ws_frame(0xA, data))
                await writer.drain()
                continue
            if opcode not in (0x0, 0x1):
                continue
            message += data
            if not fin:
                continue
            body, message = message, b""
            arrived = time.monotonic()
            cfg = STATE.lane(lane)
            turns += 1
            n = STATE.counts["generations"] = STATE.counts["generations"] + 1
            STATE.counts[f"{lane}:responses:ws"] += 1
            entry = record(lane, "responses", True, body, arrived, 200)
            entry["turn"] = turns
            if cfg["latency_ms"]:
                await asyncio.sleep(cfg["latency_ms"] / 1000)
            interval = cfg["interval_ms"] / 1000
            for i, event in enumerate(responses_events(cfg, n)):
                if i > 1 and interval:
                    await asyncio.sleep(interval)
                writer.write(ws_frame(0x1, json.dumps(event).encode()))
                await writer.drain()
            entry["done"] = time.monotonic()
            entry["done_wall"] = time.time()
    except asyncio.IncompleteReadError, ConnectionError:
        pass
    finally:
        STATE.ws_open -= 1
        STATE.counts["ws_turns"] += turns


async def serve_client(reader, writer):
    STATE.counts["http_accepted"] += 1
    STATE.http_open += 1
    STATE.http_peak = max(STATE.http_peak, STATE.http_open)
    try:
        while True:
            request = await read_request(reader)
            if request is None:
                return
            arrived = time.monotonic()
            method, path, headers, body = request
            STATE.counts["http_requests"] += 1
            lane, endpoint = route(path)
            if headers.get("upgrade", "").lower() == "websocket":
                await websocket(reader, writer, lane, headers)
                return
            if method == "POST" and endpoint == "/v1/messages/count_tokens":
                STATE.counts[f"{lane}:count"] += 1
                cfg = STATE.lane(lane)
                if cfg["latency_ms"]:
                    await asyncio.sleep(cfg["latency_ms"] / 1000)
                entry = record(lane, "count", False, body, arrived, 200)
                await respond(writer, 200, {"input_tokens": max(1, len(body) // 4)})
                entry["done"] = time.monotonic()
                entry["done_wall"] = time.time()
            elif method == "POST" and endpoint in ("/v1/responses", "/v1/messages"):
                kind = "responses" if endpoint == "/v1/responses" else "messages"
                await generate(writer, lane, kind, body, arrived)
            else:
                await respond(writer, 404, {"error": {"type": "not_found"}}, reason="Not Found")
    except ConnectionError, asyncio.IncompleteReadError, ssl.SSLError:
        pass
    finally:
        STATE.http_open -= 1
        writer.close()


async def control(reader, writer):
    try:
        request = await read_request(reader)
        if request is None:
            return
        method, path, _, body = request
        if method == "POST" and path == "/lanes":
            for name, values in json.loads(body).items():
                unknown = set(values) - set(LANE_DEFAULTS)
                if unknown:
                    raise ValueError(f"unknown lane settings {sorted(unknown)}")
                STATE.lanes.setdefault(name, dict(STATE.lanes["default"])).update(values)
            payload = STATE.lanes
        elif method == "POST" and path == "/reset":
            STATE.reset()
            payload = {"ok": True}
        elif path == "/stats":
            payload = {
                "lanes": STATE.lanes,
                "counts": dict(STATE.counts),
                "http_open": STATE.http_open,
                "http_peak": STATE.http_peak,
                "ws_open": STATE.ws_open,
                "ws_peak": STATE.ws_peak,
                "records": STATE.records,
            }
        else:
            payload = {"error": "not found"}
        await respond(writer, 200, payload)
    except Exception as error:  # report bad control input instead of hanging the caller
        await respond(writer, 400, {"error": str(error)}, reason="Bad Request")
    finally:
        writer.close()


async def main(args):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert)
    upstream = await asyncio.start_server(
        serve_client, "127.0.0.1", args.port, ssl=context, backlog=4096, limit=8 * 1024 * 1024
    )
    admin = await asyncio.start_server(control, "127.0.0.1", 0, limit=8 * 1024 * 1024)
    ports = {
        "port": upstream.sockets[0].getsockname()[1],
        "control": admin.sockets[0].getsockname()[1],
    }
    print(json.dumps(ports), flush=True)
    async with upstream, admin:
        await asyncio.gather(upstream.serve_forever(), admin.serve_forever())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cert", required=True, help="PEM with the certificate and its key")
    parser.add_argument("--port", type=int, default=0)
    try:
        uvloop.run(main(parser.parse_args()))
    except KeyboardInterrupt:
        sys.exit(0)
