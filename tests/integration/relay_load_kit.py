"""Building blocks for the Relay P5 load tests in test_relay_load.py.

Release Relay processes with their own node logins, the TLS upstream double
(scripts/relay_upstream.py), a raw HTTP/1.1 client with per-request timings, samplers for
pg_stat_activity and process RSS/CPU, PostgreSQL statement durations from the server log, and
the ledger checks. Results go to RELAY_LOAD_OUT as one JSON object per profile run.
"""

import asyncio
import base64
import json
import math
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
NODE_CAPABILITIES = [
    "reservation.v1",
    "usage-events.v1",
    "relay.native.v1",
    "relay.provider-features.v1",
    "relay.sse.v1",
    "relay.responses-ws.v1",
]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def quantile(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return values[max(0, math.ceil(len(values) * q) - 1)]


def ms(values, q):
    value = quantile(values, q)
    return None if value is None else round(1000 * value, 1)


def summary_ms(values):
    """p50/p95/p99/max in milliseconds of durations given in seconds."""
    return {
        "n": len([v for v in values if v is not None]),
        "p50": ms(values, 0.5),
        "p95": ms(values, 0.95),
        "p99": ms(values, 0.99),
        "max": ms(values, 1.0),
    }


# ---------------------------------------------------------------- host load


def host_state():
    load = os.getloadavg()
    return {"load1": round(load[0], 2), "load5": round(load[1], 2), "cpus": os.cpu_count()}


def foreign_cpu(own_pids):
    """Busiest processes outside this run, so a contended round can be told apart."""
    out = subprocess.run(
        ["ps", "-A", "-o", "pid=,%cpu=,comm="], capture_output=True, text=True
    ).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and int(parts[0]) not in own_pids:
            rows.append((float(parts[1]), Path(parts[2]).name))
    rows.sort(reverse=True)
    return {"total_pct": round(sum(r[0] for r in rows), 1), "top": rows[:4]}


def foreign_limit():
    """CPU percent (100 = one core) other processes may use before a profile waits or reruns."""
    return float(os.environ.get("RELAY_LOAD_MAX_FOREIGN_CPU", "250"))


async def calm_host(own_pids, *, limit=None, wait=300):
    """Wait until other work on the host settles; return the observed state."""
    limit = limit or foreign_limit()
    started = time.monotonic()
    while True:
        state = host_state()
        other = foreign_cpu(own_pids)
        if other["total_pct"] <= limit or time.monotonic() - started > wait:
            state["foreign_cpu"] = other
            state["waited_s"] = round(time.monotonic() - started, 1)
            state["contended"] = other["total_pct"] > limit
            return state
        await asyncio.sleep(10)


# ---------------------------------------------------------------- output


def emit(scenario, profile, round_no, metrics, host_before, host_after, **extra):
    record = {
        "scenario": scenario,
        "profile": profile,
        "round": round_no,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host_before": host_before,
        "host_after": host_after,
        "metrics": metrics,
        **extra,
    }
    line = json.dumps(record, default=str)
    print("relay_load " + line, flush=True)
    if target := os.environ.get("RELAY_LOAD_OUT"):
        with open(target, "a") as stream:
            stream.write(line + "\n")
    return record


def rounds():
    return max(1, int(os.environ.get("RELAY_LOAD_ROUNDS", "2")))


def selected(profiles, name=lambda p: p[0]):
    """Profiles whose name contains one of the RELAY_LOAD_PROFILES substrings (default all)."""
    wanted = [w for w in os.environ.get("RELAY_LOAD_PROFILES", "").split(",") if w]
    return [p for p in profiles if not wanted or any(w in name(p) for w in wanted)]


@asynccontextmanager
async def running_worker(service_directory, redis, namespace, log_path):
    """The SQL Worker as in production, with its JSON log kept for per-profile warnings."""
    name = "relay-load-" + uuid4().hex
    with open(log_path, "ab") as log:  # noqa: ASYNC230 - setup, before load
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "nexus.cli",
            "--env-file",
            str(Path(service_directory) / ".env.worker"),
            "worker",
            env=dict(
                os.environ,
                NEXUS_WORKER_ID=name,
                NEXUS_REDIS_NAMESPACE=namespace,
                NEXUS_WORKER_INTERVAL="0.25",
            ),
            stdout=log,
            stderr=log,
        )
    heartbeat = f"{namespace}:workers:{name}"
    try:
        async with asyncio.timeout(30):
            while not json.loads(await redis.get(heartbeat) or "{}").get("ready"):
                assert process.returncode is None, Path(log_path).read_text()[-2000:]  # noqa: ASYNC240 - startup failure diagnostics
                await asyncio.sleep(0.05)
        yield process
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 15)
            except TimeoutError:
                process.kill()
                await process.wait()


class WorkerHandle:
    """A Worker that a profile can stop and start again, e.g. to pause settlement."""

    def __init__(self, service_directory, redis, namespace, log_path):
        self.args = (service_directory, redis, namespace, log_path)
        self.context = None
        self.process = None

    @property
    def pid(self):
        return self.process.pid if self.process else None

    async def start(self):
        self.context = running_worker(*self.args)
        self.process = await self.context.__aenter__()

    async def stop(self):
        if self.context:
            context, self.context, self.process = self.context, None, None
            await context.__aexit__(None, None, None)


class LogTail:
    """Warning and error records appended to a JSON-lines log since the last mark."""

    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0

    def mark(self):
        self.offset = self.path.stat().st_size if self.path.exists() else 0

    def summary(self):
        counts = Counter()
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            for line in stream:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                level = str(record.get("level", "")).upper()
                if level in ("WARN", "WARNING", "ERROR"):
                    detail = record.get("consumer") or record.get("error_type") or ""
                    counts[f"{level} {record.get('message')} {detail}".strip()] += 1
        return dict(counts)


# ---------------------------------------------------------------- upstream double


class Upstream:
    def __init__(self, process, port, control):
        self.process, self.port, self.control = process, port, control

    @classmethod
    @asynccontextmanager
    async def start(cls, pem):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT / "scripts" / "relay_upstream.py"),
            "--cert",
            str(pem),
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            line = await asyncio.wait_for(process.stdout.readline(), 15)
            ports = json.loads(line)
            yield cls(process, ports["port"], ports["control"])
        finally:
            process.terminate()
            await process.wait()

    def url(self, lane="a"):
        return f"https://127.0.0.1:{self.port}/{lane}/v1"

    async def _call(self, method, path, body=None):
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.request(
                method, f"http://127.0.0.1:{self.control}{path}", json=body
            )
        assert response.status_code == 200, response.text
        return response.json()

    async def lanes(self, **lanes):
        return await self._call("POST", "/lanes", lanes)

    async def reset(self):
        return await self._call("POST", "/reset")

    async def stats(self):
        return await self._call("GET", "/stats")


# ---------------------------------------------------------------- Relay fleet


async def register_node(client, root, admin, cid, auth):
    name = "nexus_node_" + uuid4().hex
    password = secrets.token_urlsafe(32)
    admin.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE nexus_gateway").format(
            sql.Identifier(name), sql.Literal(password)
        )
    )
    # Every statement of a node is logged with its server-side duration (see PgLog).
    admin.execute(
        sql.SQL("ALTER ROLE {} SET log_min_duration_statement = 0").format(sql.Identifier(name))
    )
    # The Relay requires a role-level statement timeout (ensure_gateway_login sets the same).
    admin.execute(
        sql.SQL("ALTER ROLE {} SET statement_timeout = '8s'").format(sql.Identifier(name))
    )
    nid = uuid4()
    response = await client.put(
        f"/api/v1/gateway-clusters/{cid}/principals/{nid}",
        headers=auth(root),
        json={"node_id": str(nid), "database_role": name, "capabilities": NODE_CAPABILITIES},
    )
    assert response.status_code == 200, response.text
    return {"node_id": str(nid), "role": name, "password": password}


class Relay:
    def __init__(self, process, port, metrics_port, log, role):
        self.process, self.port, self.metrics_port = process, port, metrics_port
        self.log, self.role = log, role

    @property
    def pid(self):
        return self.process.pid

    async def metrics(self):
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            text = (await client.get(f"http://127.0.0.1:{self.metrics_port}/metrics")).text
        return parse_metrics(text)


def parse_metrics(text):
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            out[name] = float(value)
    return out


class Fleet:
    """Release Relay processes for one cluster, one node login each."""

    def __init__(self, binary, admin, settings, cid, private, ca, workdir):
        self.binary, self.admin, self.settings = binary, admin, settings
        self.cid, self.private, self.ca = cid, private, ca
        self.workdir = Path(workdir)
        self.relays = []
        self.nodes = []

    def env(self, node, port, metrics_port, overrides):
        info = self.admin.info
        login = f"{node['role']}:{quote(node['password'], safe='')}"
        return {
            "NEXUS_RELAY_CLUSTER_ID": self.cid,
            "NEXUS_RELAY_PRIVATE_KEY": base64.b64encode(bytes(self.private)).decode(),
            "NEXUS_RELAY_DATABASE_URL": f"postgresql://{login}@127.0.0.1:{info.port}/{info.dbname}?sslmode=disable",
            "NEXUS_RELAY_REDIS_URL": self.settings.redis_url.get_secret_value(),
            "NEXUS_RELAY_NAMESPACE": self.settings.redis_namespace,
            "NEXUS_RELAY_UPSTREAM_CA": str(self.ca),
            "NEXUS_RELAY_LISTEN": f"127.0.0.1:{port}",
            "NEXUS_RELAY_METRICS_LISTEN": f"127.0.0.1:{metrics_port}",
            **overrides,
        }

    async def start(self, nodes, overrides=None):
        overrides = overrides or {}
        inherited = {k: v for k, v in os.environ.items() if not k.startswith("NEXUS_")}
        started = []
        for i, node in enumerate(nodes):
            port, metrics_port = free_port(), free_port()
            log = self.workdir / f"relay-{len(self.relays) + i}.jsonl"
            with log.open("ab") as stream:
                process = await asyncio.create_subprocess_exec(
                    str(self.binary),
                    env={**inherited, **self.env(node, port, metrics_port, overrides)},
                    stderr=stream,
                    stdout=asyncio.subprocess.DEVNULL,
                )
            started.append(Relay(process, port, metrics_port, log, node["role"]))
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            for relay in started:
                async with asyncio.timeout(60):
                    while True:
                        assert relay.process.returncode is None, relay.log.read_text()[-2000:]
                        try:
                            ready = await client.get(f"http://127.0.0.1:{relay.port}/ready")
                            if ready.status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        await asyncio.sleep(0.05)
        self.relays += started
        self.nodes += nodes
        return started

    async def stop(self, relays=None):
        relays = list(self.relays if relays is None else relays)
        for relay in relays:
            if relay.process.returncode is None:
                relay.process.terminate()
        for relay in relays:
            try:
                await asyncio.wait_for(relay.process.wait(), 30)
            except TimeoutError:
                relay.process.kill()
                await relay.process.wait()
            self.relays.remove(relay)

    async def metrics(self, relays=None):
        relays = self.relays if relays is None else relays
        return await asyncio.gather(*(r.metrics() for r in relays))

    def mark_logs(self):
        self.log_offsets = {relay.log: relay.log.stat().st_size for relay in self.relays}

    def log_summary(self):
        """Warning and error messages written to Relay logs since ``mark_logs``, by message."""
        counts = Counter()
        offsets = getattr(self, "log_offsets", {})
        for relay in self.relays:
            with relay.log.open("rb") as stream:
                stream.seek(offsets.get(relay.log, 0))
                text = stream.read().decode(errors="replace")
            for line in text.splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    counts["unparsable log line"] += 1
                    continue
                if record.get("level") in ("WARN", "ERROR"):
                    counts[f"{record['level']} {record.get('message')}"] += 1
        return dict(counts)


def metric_total(samples, name, **labels):
    """Sum a metric over relays and label sets matching ``labels``."""
    total = 0.0
    for sample in samples:
        for key, value in sample.items():
            base, _, rest = key.partition("{")
            if base != name:
                continue
            if all(f'{k}="{v}"' in rest for k, v in labels.items()):
                total += value
    return total


def metric_delta(before, after, name, **labels):
    return metric_total(after, name, **labels) - metric_total(before, name, **labels)


# ---------------------------------------------------------------- raw HTTP client


class Exchange:
    __slots__ = (
        "marker",
        "sent",
        "sent_wall",
        "connected",
        "first_byte",
        "done",
        "status",
        "request_id",
        "events",
        "event_times",
        "body",
        "error",
        "bytes",
    )

    def __init__(self, marker):
        self.marker = marker
        self.sent = self.sent_wall = self.connected = self.first_byte = self.done = None
        self.status = None
        self.request_id = None
        self.events = 0
        self.event_times = []
        self.body = b""
        self.error = None
        self.bytes = 0

    @property
    def latency(self):
        return None if self.done is None else self.done - self.sent

    @property
    def ttfb(self):
        return None if self.first_byte is None else self.first_byte - self.sent

    def gaps(self):
        t = self.event_times
        return [b - a for a, b in zip(t, t[1:], strict=False)]


async def exchange(
    port,
    path,
    headers,
    body,
    marker=None,
    *,
    keep_body=True,
    slow=None,
    deadline=120,
):
    """One HTTP/1.1 request on its own connection, as the Relay closes after each response.

    ``slow=(bytes_per_read, pause_s)`` reads the body slowly with a small socket buffer.
    """
    result = Exchange(marker)
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    head = (
        f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
        + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        + f"Content-Length: {len(data)}\r\n\r\n"
    ).encode()
    writer = None
    result.sent_wall = time.time()
    result.sent = time.monotonic()
    try:
        async with asyncio.timeout(deadline):
            if slow:
                sock = socket.socket()
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                sock.setblocking(False)
                await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", port))
                reader, writer = await asyncio.open_connection(sock=sock, limit=4096)
            else:
                reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=1 << 22)
            result.connected = time.monotonic()
            writer.write(head + data)
            await writer.drain()
            status = await reader.readline()
            result.first_byte = time.monotonic()
            if not status:
                raise ConnectionError("closed before status")
            result.status = int(status.split()[1])
            response_headers = {}
            while (line := await reader.readline()) not in (b"\r\n", b""):
                name, value = line.decode("latin-1").split(":", 1)
                response_headers[name.strip().lower()] = value.strip()
            result.request_id = response_headers.get("x-request-id")
            sse = response_headers.get("content-type", "").startswith("text/event-stream")
            pending = b""
            parts = []

            async def take(piece):
                nonlocal pending
                result.bytes += len(piece)
                if keep_body or not sse:
                    parts.append(piece)
                if sse:
                    pending += piece
                    while (cut := pending.find(b"\n\n")) >= 0:
                        frame, pending = pending[: cut + 2], pending[cut + 2 :]
                        result.events += 1
                        result.event_times.append(time.monotonic())
                        if not keep_body:
                            parts[:] = [frame]  # keep the last event for the terminal check
                if slow:
                    await asyncio.sleep(slow[1])

            if response_headers.get("transfer-encoding", "").lower() == "chunked":
                while True:
                    size = int((await reader.readuntil(b"\r\n")).split(b";")[0], 16)
                    if not size:
                        await reader.readuntil(b"\r\n")
                        break
                    remaining = size
                    while remaining:
                        piece = await reader.read(min(remaining, slow[0] if slow else 1 << 20))
                        if not piece:
                            raise ConnectionError("truncated chunk")
                        remaining -= len(piece)
                        await take(piece)
                    await reader.readexactly(2)
            elif "content-length" in response_headers:
                await take(await reader.readexactly(int(response_headers["content-length"])))
            else:
                while piece := await reader.read(slow[0] if slow else 1 << 20):
                    await take(piece)
            result.body = b"".join(parts)
            result.done = time.monotonic()
    except Exception as error:  # recorded per request; the profile reports counts by type
        result.error = type(error).__name__
    finally:
        if writer is not None:
            writer.close()
    return result


def terminal_ok(result, kind):
    """The response carries the protocol's successful terminal."""
    if result.status != 200 or result.error:
        return False
    if result.events:
        tail = result.body[-600:]
        return (b"response.completed" in tail) if kind == "responses" else (b"message_stop" in tail)
    return True


# ---------------------------------------------------------------- samplers


class PgLog:
    """Durations of node statements, from log_min_duration_statement=0 on each node role."""

    LINE = re.compile(
        rb"^\S+ \S+ \S+ \[\d+\] (nexus_node_\w+) LOG:  duration: ([\d.]+) ms  "
        rb"(?:execute|statement)[^:]*: SELECT gateway_v1\.(\w+)"
    )

    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0

    def mark(self):
        self.offset = self.path.stat().st_size

    def read(self):
        durations = defaultdict(list)
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            for line in stream:
                if match := self.LINE.match(line):
                    durations[match.group(3).decode()].append(float(match.group(2)) / 1000)
        return durations


def statement_summary(durations, names=None):
    names = names or sorted(durations)
    return {name: summary_ms(durations.get(name, [])) for name in names}


def process_usage(pids):
    """RSS (MiB) and cumulative CPU seconds per pid."""
    if not pids:
        return {}
    out = subprocess.run(
        ["ps", "-o", "pid=,rss=,time=", "-p", ",".join(str(p) for p in pids)],
        capture_output=True,
        text=True,
    ).stdout
    usage = {}
    for line in out.splitlines():
        pid, rss, cpu = line.split()
        minutes, _, seconds = cpu.rpartition(":")
        hours, _, minutes = minutes.rpartition(":")
        total = float(seconds) + 60 * float(minutes or 0) + 3600 * float(hours or 0)
        usage[int(pid)] = (int(rss) / 1024, total)
    return usage


def postgres_pids(service_directory):
    postmaster = int(
        (Path(service_directory) / "postgres" / "postmaster.pid").read_text().split()[0]
    )
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True).stdout
    children = [
        int(p) for p, pp in (line.split() for line in out.splitlines()) if int(pp) == postmaster
    ]
    return [postmaster, *children]


def redis_pid(service_directory):
    path = Path(service_directory) / "redis.pid"
    return int(path.read_text().strip()) if path.exists() else None


class Sampler:
    """pg_stat_activity of node logins every 100 ms; RSS/CPU of our processes every 500 ms."""

    def __init__(self, dsn, service_directory, groups, pool_size=8):
        self.dsn, self.directory, self.groups = dsn, service_directory, groups
        self.pool_size = pool_size
        self.stop = asyncio.Event()
        self.pg = []
        self.proc = []
        self.foreign = []
        self.blockers = Counter()
        self.worker_xact = []

    def _pg_sample(self, connection):
        rows = connection.execute(
            """SELECT usename, state, wait_event_type, wait_event,
                      extract(epoch FROM clock_timestamp()-xact_start)
               FROM pg_stat_activity
               WHERE datname=current_database() AND usename LIKE 'nexus_node_%%'
                 AND pid<>pg_backend_pid()"""
        ).fetchall()
        ready = connection.execute(
            "SELECT count(*) FROM billing.usage_jobs WHERE state='ready'"
        ).fetchone()[0]
        per_node = Counter()
        busy = Counter()
        sample = Counter()
        for user, state, wtype, wevent, _ in rows:
            per_node[user] += 1
            if state not in ("idle", None):
                busy[user] += 1
                sample["active"] += 1
            if wtype == "Lock":
                sample["lock_waiters"] += 1
                sample[f"lock:{wevent}"] += 1
            if state == "idle in transaction":
                sample["idle_in_tx"] += 1
        sample["connections"] = len(rows)
        sample["saturated_nodes"] = sum(1 for n in busy.values() if n >= self.pool_size)
        sample["max_busy_per_node"] = max(busy.values(), default=0)
        sample["ready_jobs"] = ready
        # Worker transactions that are open right now, and how long they have been open.
        for (age,) in connection.execute(
            """SELECT extract(epoch FROM clock_timestamp()-xact_start) FROM pg_stat_activity
               WHERE datname=current_database() AND usename='nexus_worker'
                 AND xact_start IS NOT NULL AND pid<>pg_backend_pid()"""
        ).fetchall():
            self.worker_xact.append(float(age))
        return sample

    def _blocking_sample(self, connection):
        """Who holds the locks node sessions wait for: another node or the Worker."""
        for blocker, state, waiting, n in connection.execute(
            """SELECT CASE WHEN b.usename LIKE 'nexus_node_%%' THEN 'node' ELSE b.usename END,
                      b.state, b.wait_event_type IS NOT NULL AND b.wait_event_type='Lock', count(*)
               FROM pg_stat_activity w
               CROSS JOIN LATERAL unnest(pg_blocking_pids(w.pid)) bp(pid)
               JOIN pg_stat_activity b ON b.pid=bp.pid
               WHERE w.datname=current_database() AND w.wait_event_type='Lock'
                 AND w.usename LIKE 'nexus_node_%%'
               GROUP BY 1,2,3"""
        ).fetchall():
            self.blockers[f"{blocker}:{state}{':waiting' if waiting else ''}"] += n

    async def run(self):
        loop_index = 0
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            while not self.stop.is_set():
                started = time.monotonic()
                sample = await asyncio.to_thread(self._pg_sample, connection)
                self.pg.append((started, sample))
                if loop_index % 5 == 0:
                    pids = {name: list(p) for name, p in self.groups().items()}
                    flat = [pid for group in pids.values() for pid in group]
                    usage = await asyncio.to_thread(process_usage, flat)
                    self.proc.append((started, pids, usage))
                if loop_index % 10 == 5:
                    await asyncio.to_thread(self._blocking_sample, connection)
                if loop_index % 20 == 0:
                    own = {pid for group in self.groups().values() for pid in group}
                    other = await asyncio.to_thread(foreign_cpu, own)
                    self.foreign.append(other["total_pct"])
                    if other["total_pct"] >= max(self.foreign):
                        self.foreign_top = other["top"]
                loop_index += 1
                await asyncio.sleep(max(0, 0.1 - (time.monotonic() - started)))

    def summary(self):
        pg = [s for _, s in self.pg]
        peaks = Counter()
        for sample in pg:
            for key, value in sample.items():
                peaks[key] = max(peaks[key], value)
        total = len(pg) or 1
        out = {
            "samples": len(pg),
            "peak": dict(peaks),
            "mean_lock_waiters": round(sum(s["lock_waiters"] for s in pg) / total, 2),
            "mean_active": round(sum(s["active"] for s in pg) / total, 2),
            "share_with_saturated_pool": round(
                sum(1 for s in pg if s["saturated_nodes"]) / total, 3
            ),
            "blocked_node_sessions_by_blocker": dict(self.blockers),
            "worker_open_xact_ms": summary_ms(self.worker_xact),
            "foreign_cpu_pct": {
                "mean": round(sum(self.foreign) / len(self.foreign), 1) if self.foreign else None,
                "max": max(self.foreign, default=None),
                "top_at_max": getattr(self, "foreign_top", None),
            },
        }
        if len(self.proc) >= 2:
            (t0, _, first), (t1, pids, last) = self.proc[0], self.proc[-1]
            elapsed = max(t1 - t0, 1e-6)
            groups = {}
            for name, group in pids.items():
                cpu = sum(last[p][1] - first.get(p, (0, last[p][1]))[1] for p in group if p in last)
                rss_peak = max(
                    (
                        sum(u[p][0] for p in grp if p in u)
                        for _, g, u in self.proc
                        for n, grp in g.items()
                        if n == name
                    ),
                    default=0,
                )
                per_process_peak = max(
                    (
                        u[p][0]
                        for _, g, u in self.proc
                        for n, grp in g.items()
                        if n == name
                        for p in grp
                        if p in u
                    ),
                    default=0,
                )
                groups[name] = {
                    "cpu_cores": round(cpu / elapsed, 2),
                    "rss_total_peak_mib": round(rss_peak, 1),
                    "rss_process_peak_mib": round(per_process_peak, 1),
                    "processes": len(group),
                }
            out["processes"] = groups
        return out


@asynccontextmanager
async def sampling(dsn, service_directory, groups, pool_size=8):
    sampler = Sampler(dsn, service_directory, groups, pool_size)
    task = asyncio.create_task(sampler.run())
    try:
        yield sampler
    finally:
        sampler.stop.set()
        await task


# ---------------------------------------------------------------- ledger checks


def new_requests(admin, since_ids):
    rows = admin.execute("SELECT id FROM billing.request_runs").fetchall()
    return [r[0] for r in rows if r[0] not in since_ids]


def known_requests(admin):
    return {r[0] for r in admin.execute("SELECT id FROM billing.request_runs").fetchall()}


async def wait_settled(admin, ids, limit=180):
    """Wait until every request is settled, canceled or in review; return the seconds waited."""
    started = time.monotonic()
    while time.monotonic() - started < limit:
        open_runs = admin.execute(
            """SELECT count(*) FROM billing.request_runs r
               LEFT JOIN billing.usage_jobs j ON j.request_id=r.id
               WHERE r.id=ANY(%s) AND r.state NOT IN ('settled','canceled','review')
                 AND j.state IS DISTINCT FROM 'done' AND j.state IS DISTINCT FROM 'review'""",
            (ids,),
        ).fetchone()[0]
        if not open_runs:
            break
        await asyncio.sleep(0.2)
    return time.monotonic() - started


def settlement(admin, ids, expected_events=2):
    """Settlement latency from the last usage evidence commit to the debit commit."""
    row = admin.execute(
        """
        WITH requests AS (SELECT unnest(%s::uuid[]) id),
        evidence AS (
          SELECT request_id, count(*) n, max(pg_xact_commit_timestamp(xmin)) last_event
          FROM billing.usage_events WHERE request_id=ANY(%s) GROUP BY request_id
        ), charges AS (
          SELECT source_id, count(*) n, max(pg_xact_commit_timestamp(xmin)) booked
          FROM billing.ledger_entries WHERE kind='usage_debit' AND source_id=ANY(%s)
          GROUP BY source_id
        ), facts AS (
          SELECT j.state, run.state run_state, e.n events, e.last_event, c.n debits, c.booked,
            max(e.last_event) OVER() ingress_end,
            CASE WHEN j.state='done' THEN pg_xact_commit_timestamp(j.xmin) END done_at
          FROM requests r JOIN billing.request_runs run ON run.id=r.id
          LEFT JOIN evidence e ON e.request_id=r.id
          LEFT JOIN billing.usage_jobs j ON j.request_id=r.id
          LEFT JOIN charges c ON c.source_id=r.id
        )
        SELECT count(*),
          count(*) FILTER(WHERE state IS DISTINCT FROM 'done' AND state IS DISTINCT FROM 'review'
                          AND run_state<>'canceled'),
          count(*) FILTER(WHERE state='review'),
          count(*) FILTER(WHERE run_state<>'canceled' AND (events < %s OR events IS NULL)),
          count(*) FILTER(WHERE done_at>ingress_end),
          extract(epoch FROM max(done_at)-max(ingress_end)),
          percentile_disc(0.95) WITHIN GROUP(ORDER BY extract(epoch FROM booked-last_event)),
          percentile_disc(0.99) WITHIN GROUP(ORDER BY extract(epoch FROM booked-last_event)),
          max(extract(epoch FROM booked-last_event)),
          (SELECT coalesce(sum(failures),0) FROM billing.usage_jobs WHERE request_id=ANY(%s)),
          count(*) FILTER(WHERE run_state='canceled')
        FROM facts
        """,
        (ids, ids, ids, expected_events, ids),
    ).fetchone()
    total, unfinished, review, short, backlog, drain, p95, p99, worst, failures, canceled = row
    return {
        "requests": total,
        "unfinished": unfinished,
        "canceled_before_attempt": canceled,
        "review": review,
        "missing_events": short,
        "backlog_at_ingress_end": backlog,
        "drain_s": None if drain is None else round(float(drain), 3),
        "settle_p95_s": None if p95 is None else round(float(p95), 3),
        "settle_p99_s": None if p99 is None else round(float(p99), 3),
        "settle_max_s": None if worst is None else round(float(worst), 3),
        "job_retries": int(failures),
    }


def money(admin, ids, expected=None):
    """Debits of the given requests and the global wallet and budget invariants.

    ``expected`` maps a request id to its debit (0: none). Holds may remain only on requests in
    review, which keep their reservation until finance resolves them.
    """
    debits = dict(
        admin.execute(
            """SELECT r.id, coalesce(array_agg(l.signed_amount) FILTER (WHERE l.id IS NOT NULL),
                     '{}') FROM unnest(%s::uuid[]) r(id)
               LEFT JOIN billing.ledger_entries l ON l.source_id=r.id AND l.kind='usage_debit'
               GROUP BY r.id""",
            (ids,),
        ).fetchall()
    )
    duplicate = sum(1 for v in debits.values() if len(v) > 1)
    in_review = {
        r[0]
        for r in admin.execute(
            "SELECT id FROM billing.request_runs WHERE id=ANY(%s) AND state='review'", (ids,)
        ).fetchall()
    }
    missing = wrong = reviewed = 0
    if expected is not None:
        for rid, amount in expected.items():
            key = rid if rid in debits else _uuid(rid)
            got = debits.get(key) or []
            if amount and not got and key in in_review:
                reviewed += 1
            elif amount and not got:
                missing += 1
            elif got and (not amount or -sum(got) != amount):
                wrong += 1
    stray = admin.execute(
        """SELECT count(*) FROM billing.request_runs
           WHERE reservation_state='held' AND state<>'review'"""
    ).fetchone()[0]
    accounts = admin.execute(
        """SELECT count(*) FILTER (WHERE a.held_amount <> coalesce(h.held,0)),
                  count(*) FILTER (WHERE a.balance <> coalesce(l.total,0)),
                  coalesce(sum(a.held_amount),0)
           FROM billing.accounts a
           LEFT JOIN (SELECT account_id, sum(reserved_amount) held FROM billing.request_runs
                      WHERE reservation_state='held' GROUP BY account_id) h ON h.account_id=a.id
           LEFT JOIN (SELECT account_id, sum(signed_amount) total FROM billing.ledger_entries
                      GROUP BY account_id) l ON l.account_id=a.id"""
    ).fetchone()
    budgets = admin.execute(
        """SELECT count(*) FROM billing.budget_periods p WHERE p.reserved <> coalesce((
             SELECT sum(h.reserved) FROM billing.budget_holds h WHERE h.period_id=p.id),0)"""
    ).fetchone()[0]
    runs = dict(
        admin.execute(
            """SELECT state||'/'||reservation_state, count(*) FROM billing.request_runs
               WHERE id=ANY(%s) GROUP BY 1""",
            (ids,),
        ).fetchall()
    )
    cases = dict(
        admin.execute(
            """SELECT reason, count(*) FROM billing.review_cases WHERE request_id=ANY(%s)
               GROUP BY reason""",
            (ids,),
        ).fetchall()
    )
    return {
        "charged_requests": sum(1 for v in debits.values() if v),
        "charged_total": float(-sum(sum(v) for v in debits.values())),
        "duplicate_debits": duplicate,
        "missing_debits": missing,
        "wrong_amount": wrong,
        "review_despite_client_success": reviewed,
        "stray_holds": stray,
        "hold_mismatch_accounts": accounts[0],
        "balance_mismatch_accounts": accounts[1],
        "held_total": float(accounts[2]),
        "budget_mismatch_periods": budgets,
        "request_states": runs,
        "review_cases": cases,
    }


MONEY_FAILURES = (
    "duplicate_debits",
    "missing_debits",
    "wrong_amount",
    "stray_holds",
    "hold_mismatch_accounts",
    "balance_mismatch_accounts",
    "budget_mismatch_periods",
)


def _uuid(value):
    from uuid import UUID

    try:
        return UUID(str(value))
    except ValueError:
        return None


def parse_url(url):
    parts = urlsplit(url)
    return parts.hostname, parts.port
