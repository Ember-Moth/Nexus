import json
import os
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest
from redis import Redis

pytestmark = pytest.mark.integration


def wait_for(check, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except OSError, httpx.HTTPError:
            pass
        time.sleep(0.1)
    pytest.fail("process did not reach the expected state")


def test_api_process_starts_and_stops(service_directory):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "nexus.cli",
            "--env-file",
            str(service_directory / ".env.api"),
            "api",
        ],
        env=dict(os.environ, NEXUS_PORT=str(port)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=0.5) as client:
            wait_for(lambda: client.get("/health/ready").status_code == 200)
            assert client.get("/health/live").json() == {"status": "alive"}
        process.terminate()
        _, diagnostics = process.communicate(timeout=10)
        # Uvicorn re-raises SIGTERM after completing its lifespan shutdown.
        assert process.returncode in {0, -signal.SIGTERM}
        assert "Application shutdown complete" in diagnostics
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_worker_process_heartbeat_and_graceful_shutdown(service_directory):
    data = json.loads((service_directory / "services.json").read_text())
    client = Redis(
        host="127.0.0.1",
        port=data["redis_port"],
        password=data["redis_password"],
        decode_responses=True,
    )
    key = "nexus:v1:workers:integration-worker"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "nexus.cli",
            "--env-file",
            str(service_directory / ".env.worker"),
            "worker",
        ],
        env=dict(os.environ, NEXUS_WORKER_ID="integration-worker", NEXUS_WORKER_INTERVAL="0.2"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(lambda: client.get(key) is not None and json.loads(client.get(key))["ready"])
        assert client.ttl(key) > 0
        process.terminate()
        assert process.wait(timeout=10) == 0
        assert client.get(key) is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        client.close()


async def test_worker_process_consumes_account_events(http, service_directory, admin):
    import asyncio

    from test_identity_api import auth, create_org

    client, root, _, _ = http
    org = await create_org(client, root, "Worker provisioned business")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "nexus.cli",
        "--env-file",
        str(service_directory / ".env.worker"),
        "worker",
        env=dict(os.environ, NEXUS_WORKER_ID="billing-worker"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            response = await client.get(f"/api/v1/organizations/{org['id']}", headers=auth(root))
            if response.json()["status"] == "active":
                break
            await asyncio.sleep(0.05)
        assert response.json()["status"] == "active"
        assert (
            admin.execute(
                "SELECT count(*) FROM billing.accounts WHERE organization_id=%s", (org["id"],)
            ).fetchone()[0]
            == 1
        )
        process.terminate()
        assert await asyncio.wait_for(process.wait(), timeout=10) == 0
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5)
