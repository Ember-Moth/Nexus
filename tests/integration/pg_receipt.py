"""Loopback fault injector for a SQLx gateway connection on an isolated PostgreSQL instance."""

import asyncio
from contextlib import asynccontextmanager, suppress


async def packet(reader):
    tag = await reader.readexactly(1)
    size = await reader.readexactly(4)
    body = await reader.readexactly(int.from_bytes(size, "big") - 4)
    return tag, body, tag + size + body


@asynccontextmanager
async def lose_gateway_receipts(host, port):
    # Each targeted SELECT commits normally. Discard its first response through
    # ReadyForQuery, then close the client socket. All reconnects go to the same DB.
    pending = {"reserve_request", "start_attempt", "append_usage_event"}
    dropped = []

    async def connect(client, downstream):
        upstream = None
        tasks = []
        try:
            server, upstream = await asyncio.open_connection(host, port)
            length = await client.readexactly(4)
            upstream.write(length + await client.readexactly(int.from_bytes(length, "big") - 4))
            await upstream.drain()
            statements = {}
            portals = {}
            discard = None

            async def requests():
                nonlocal discard
                while True:
                    kind, body, data = await packet(client)
                    if kind == b"P":
                        name, query, _ = body.split(b"\0", 2)
                        statements[name] = query
                    elif kind == b"B":
                        portal, name, _ = body.split(b"\0", 2)
                        portals[portal] = statements.get(name, b"")
                    elif kind == b"E":
                        query = portals.get(body.split(b"\0", 1)[0], b"")
                        for op in sorted(pending):
                            if f"gateway_v1.{op}(".encode() in query:
                                pending.remove(op)
                                discard = op
                                break
                    upstream.write(data)
                    await upstream.drain()

            async def replies():
                while True:
                    kind, body, data = await packet(server)
                    if discard:
                        if kind == b"Z":
                            assert body == b"I"
                            dropped.append(discard)
                            return
                    else:
                        downstream.write(data)
                        await downstream.drain()

            tasks = [asyncio.create_task(requests()), asyncio.create_task(replies())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except OSError, asyncio.IncompleteReadError:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for writer in [upstream, downstream]:
                if writer:
                    writer.close()
                    with suppress(OSError):
                        await writer.wait_closed()

    listener = await asyncio.start_server(connect, "127.0.0.1", 0)
    async with listener:
        yield listener.sockets[0].getsockname()[1], dropped
