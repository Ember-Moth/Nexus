"""TLS stand-in for an OpenAI Responses upstream, for the k3s smoke test. Standard library only.

  POST /v1/responses               JSON, or SSE with "stream": true
  GET  /v1/responses (Upgrade)     WebSocket: one response per response.create
  GET  /headers  (plain HTTP 8080) echoes request headers, to check the client IP Traefik passes

Every response reports 2 input and 3 output tokens. An input text containing "hold=<seconds>"
makes the generation last that long, sending a delta every two seconds (for drain drills).
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import ssl
import struct
import sys
import uuid

USAGE = {"input_tokens": 2, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 0}}
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def log(**fields):
    print(json.dumps(fields), flush=True)


def hold_seconds(body) -> float:
    text = json.dumps(body.get("input", ""))
    match = re.search(r"hold=(\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else 0.0


def completed(rid: str) -> dict:
    return {
        "id": rid,
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg_" + rid,
                "role": "assistant",
                "content": [{"type": "output_text", "text": "pong"}],
            }
        ],
        "usage": USAGE,
    }


def events(rid: str, hold: float):
    yield 0, {"type": "response.created", "response": {"id": rid, "status": "in_progress"}}
    ticks = max(1, int(hold // 2))
    for _ in range(ticks):
        yield (
            hold / ticks,
            {
                "type": "response.output_text.delta",
                "item_id": "msg_" + rid,
                "output_index": 0,
                "content_index": 0,
                "delta": "po",
            },
        )
    yield 0, {"type": "response.completed", "response": completed(rid)}


async def read_head(reader):
    head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
    method, path, _ = head[0].split(" ", 2)
    headers = {}
    for line in head[1:]:
        if line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    return method, path, headers


async def read_body(reader, headers) -> bytes:
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = b""
        while True:
            size = int((await reader.readline()).split(b";")[0], 16)
            if size == 0:
                await reader.readline()
                return body
            body += await reader.readexactly(size)
            await reader.readexactly(2)
    return await reader.readexactly(int(headers.get("content-length", "0")))


def respond(writer, status: str, body: bytes, content_type="application/json"):
    writer.write(
        f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )


async def http_responses(reader, writer, headers):
    body = json.loads(await read_body(reader, headers))
    rid = "resp_" + uuid.uuid4().hex[:12]
    hold = hold_seconds(body)
    log(event="request", transport="sse" if body.get("stream") else "http", id=rid, hold=hold)
    if not body.get("stream"):
        await asyncio.sleep(hold)
        respond(writer, "200 OK", json.dumps(completed(rid)).encode())
        return
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\n"
        b"Connection: close\r\n\r\n"
    )
    for delay, event in events(rid, hold):
        await asyncio.sleep(delay)
        writer.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
        await writer.drain()


async def ws_send(writer, text: str, opcode=0x1):
    data = text.encode() if isinstance(text, str) else text
    header = bytes([0x80 | opcode])
    if len(data) < 126:
        header += bytes([len(data)])
    elif len(data) < 65536:
        header += bytes([126]) + struct.pack("!H", len(data))
    else:
        header += bytes([127]) + struct.pack("!Q", len(data))
    writer.write(header + data)
    await writer.drain()


async def ws_recv(reader):
    first, second = await reader.readexactly(2)
    opcode, length = first & 0x0F, second & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if second & 0x80 else b"\0\0\0\0"
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(await reader.readexactly(length)))
    return opcode, payload


async def websocket(reader, writer, headers):
    accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + GUID).encode()).digest())
    writer.write(
        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
    )
    await writer.drain()
    log(event="websocket_open")
    while True:
        opcode, payload = await ws_recv(reader)
        if opcode == 0x8:
            await ws_send(writer, payload[:2], opcode=0x8)
            return
        if opcode == 0x9:
            await ws_send(writer, payload, opcode=0xA)
            continue
        if opcode != 0x1:
            continue
        message = json.loads(payload)
        if message.get("type") != "response.create":
            continue
        rid = "resp_" + uuid.uuid4().hex[:12]
        hold = hold_seconds(message)
        log(event="request", transport="websocket", id=rid, hold=hold)
        for delay, event in events(rid, hold):
            await asyncio.sleep(delay)
            await ws_send(writer, json.dumps(event))


async def upstream(reader, writer):
    try:
        method, path, headers = await read_head(reader)
        if path.split("?")[0] != "/v1/responses":
            respond(writer, "404 Not Found", b'{"error":{"message":"not found"}}')
        elif method == "GET" and headers.get("upgrade", "").lower() == "websocket":
            await websocket(reader, writer, headers)
        elif method == "POST":
            await http_responses(reader, writer, headers)
        else:
            respond(writer, "405 Method Not Allowed", b"{}")
        await writer.drain()
    except asyncio.IncompleteReadError, ConnectionError, ssl.SSLError:
        pass
    except Exception as error:  # keep serving; the smoke test reads the log
        log(event="error", error=repr(error))
    finally:
        writer.close()


async def echo(reader, writer):
    try:
        method, path, headers = await read_head(reader)
        peer = writer.get_extra_info("peername")[0]
        respond(writer, "200 OK", json.dumps({"peer": peer, "headers": headers}).encode())
        await writer.drain()
    except asyncio.IncompleteReadError, ConnectionError:
        pass
    finally:
        writer.close()


async def main():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(os.environ["TLS_CERT"], os.environ["TLS_KEY"])
    context.set_alpn_protocols(["http/1.1"])
    tls = await asyncio.start_server(upstream, "0.0.0.0", 8443, ssl=context)
    plain = await asyncio.start_server(echo, "0.0.0.0", 8080)
    log(event="listening", tls=8443, echo=8080)
    async with tls, plain:
        await asyncio.gather(tls.serve_forever(), plain.serve_forever())


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
