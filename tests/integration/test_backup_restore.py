"""Exercise a verified physical backup on a separate PostgreSQL/Redis pair."""

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path
from time import monotonic
from uuid import uuid4

import httpx
import psycopg
import pytest
from test_billing import account_amounts, headers
from test_control_plane_acceptance import drain, submit, worker
from test_identity_api import auth
from test_payments import SIGNING_KEY, channel, order, payments, settle_paid  # noqa: F401
from test_plane import plane, register_node  # noqa: F401

from nexus.api import create_app
from nexus.config import RuntimeSettings
from nexus.gateway_simulator import GatewaySimulator
from scripts.native_services import NativeServices, executable

pytestmark = pytest.mark.integration


def physical_backup(target, source):
    # libpq accepts the test admin password via the child environment, never in argv.
    started = monotonic()
    subprocess.run(
        [
            executable("pg_basebackup"),
            "-h",
            "127.0.0.1",
            "-p",
            str(source["pg_port"]),
            "-U",
            "nexus_local_admin",
            "-D",
            str(target),
            "-X",
            "stream",
            "-c",
            "fast",
            "--no-password",
        ],
        env=dict(os.environ, PGPASSWORD=source["admin_password"]),
        check=True,
        timeout=90,
    )
    subprocess.run([executable("pg_verifybackup"), str(target)], check=True, timeout=90)
    return monotonic() - started


async def test_restore_snapshot_and_resume(plane, payments, http, admin, service_directory):  # noqa: F811
    peer, private, org, model, dep, _, raw, cid = plane
    client, root, app, _ = http
    chan = await channel(payments)
    topup = await order(payments, org, chan)
    await settle_paid(payments, org, topup, chan)
    await peer.refresh()
    completed, pending, after_backup = uuid4(), uuid4(), uuid4()
    await submit(peer, raw, model["id"], dep["id"], completed)
    await app.state.usage.process()
    await submit(peer, raw, model["id"], dep["id"], pending)
    assert account_amounts(admin, org) == (108, 2)
    dsn = await register_node(client, root, admin, cid)
    source = json.loads((service_directory / "services.json").read_text())

    with tempfile.TemporaryDirectory(prefix="nexus-restore-") as directory:
        dest = NativeServices(Path(directory), database=source["database"], track_commits=True)
        # Restore the independently held application key and login secrets. The Redis
        # instance, test ports and filesystem are allocated independently of the source.
        for field in (
            "admin_password",
            "worker_password",
            "api_password",
            "migrator_password",
            "encryption_key",
        ):
            dest.data[field] = source[field]
        dest.metadata.write_text(json.dumps(dest.data))
        backup_seconds = await asyncio.to_thread(physical_backup, dest.pg_data, source)
        await submit(peer, raw, model["id"], dep["id"], after_backup)
        await app.state.usage.process()
        assert account_amounts(admin, org) == (
            104,
            0,
        )

        recovery_start = monotonic()
        try:
            await asyncio.to_thread(dest.start)
            dest.profiles(dest.root)
            restored = RuntimeSettings(
                _env_file=dest.root / ".env.api",
                environment="test",
                mock_payment_url=payments.provider.url,
                payment_public_base_url="https://control.nexus.test",
            )
            api = create_app(restored)
            async with (
                api.router.lifespan_context(api),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=api), base_url="http://restored"
                ) as control,
                GatewaySimulator.connect(
                    psycopg.conninfo.make_conninfo(dsn, port=dest.data["pg_port"]),
                    restored.redis_url.get_secret_value(),
                    restored.redis_namespace,
                ) as node,
            ):
                assert (await control.get("/health/ready")).status_code == 200
                with dest.admin_connection() as db:
                    assert account_amounts(db, org) == (108, 2)
                    assert db.execute(
                        "SELECT count(*) FROM billing.request_runs WHERE id=%s", (after_backup,)
                    ).fetchone() == (0,)
                    assert db.execute(
                        "SELECT count(*) FROM billing.payment_receipts"
                    ).fetchone() == (1,)
                    assert (await node.refresh())["revision"] == "1"
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        await node.connection.execute("SELECT balance FROM billing.accounts")
                    # Publish again to exercise the restored application and cluster keys.
                    publication = await control.post(
                        f"/api/v1/gateway-clusters/{cid}/releases",
                        headers=headers(root, "restore-publish"),
                        json={},
                    )
                    assert publication.status_code == 202, publication.text
                    snapshot = await node.refresh()
                    assert snapshot["revision"] == "2"
                    assert (
                        await node.credential(
                            snapshot["resources"]["credential_refs"][0], bytes(private)
                        )
                        == "provider-secret-do-not-echo"
                    )
                    notice = payments.provider.notice(topup["merchant_order_ref"])
                    body, signed = payments.provider.signed_notice(
                        notice, SIGNING_KEY, mode="raw_json"
                    )
                    assert (
                        await control.post(
                            "/webhooks/payments/" + chan["id"], headers=signed, content=body
                        )
                    ).status_code == 200
                    async with worker(
                        dest.root, node.redis, node.namespace, mock_url=payments.provider.url
                    ):
                        await drain(db, [completed, pending])
                        assert account_amounts(db, org) == (106, 0)
                        assert db.execute(
                            "SELECT count(*) FROM billing.payment_receipts"
                        ).fetchone() == (1,)
                        assert db.execute(
                            "SELECT count(*) FROM billing.ledger_entries WHERE kind='usage_debit'"
                        ).fetchone() == (2,)
                        check = await control.post(
                            f"/api/v1/organizations/{org['id']}/account/reconcile",
                            headers=auth(root),
                        )
                        assert check.json()["consistent"], check.text
                        print(
                            "verified_backup_seconds",
                            round(backup_seconds, 3),
                            "restore_and_drain_seconds",
                            round(monotonic() - recovery_start, 3),
                        )
        finally:
            await asyncio.to_thread(dest.stop)
