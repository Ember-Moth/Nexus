"""Business policies in each module's typed policy table: API writes and policy file imports."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from uuid import uuid4

import pytest
from policies import body_of, merged, put_overrides, put_policy
from test_auto_publish import instant
from test_billing import headers
from test_identity_api import PASSWORD, auth, create_org, create_user, login
from test_plane import provision_plane

from nexus.api import create_app
from nexus.commands import import_policies
from nexus.modules.publication import AutoPublisher
from nexus.shared.policy import POLICY_TTL_SECONDS, PolicyRejected

pytestmark = pytest.mark.integration
AUTH = "/api/v1/settings/authentication"
BILLING = "/api/v1/settings/billing"
REPORTING = "/api/v1/settings/reporting"
QUOTAS = "/api/v1/settings/resource-quotas"
DEFAULTS = "/api/v1/settings/cluster-defaults"
# Each policy with one valid change, one change outside its bounds, and its audit trail.
POLICIES = [
    (AUTH, {"sign_in": {"per_ip": 5}}, {"sign_in": {"per_ip": 10001}}, "identity"),
    (
        AUTH,
        {"mail": {"per_recipient": 4}},
        {"mail": {"per_recipient": 11, "per_recipient_total": 10}},
        "identity",
    ),
    (
        BILLING,
        {"vector_storage": {"lapse_policy": "suspend"}},
        {"vector_storage": {"prepaid_days": 32}},
        "billing",
    ),
    (REPORTING, {"exports": {"max_rows": 5}}, {"exports": {"ttl_seconds": 59}}, "reporting"),
    (QUOTAS, {"vector_stores_per_key": 7}, {"resources_per_key": 0}, "catalog"),
    (
        DEFAULTS,
        {"relay": {"max_attempts": 3}},
        {"auto_publish": {"quiet_seconds": 40, "max_delay_seconds": 30}},
        "publication",
    ),
]
ACTIONS = {
    "identity": "auth_policy.updated",
    "billing": "billing_policy.updated",
    "reporting": "reporting_policy.updated",
    "catalog": "resource_quotas.updated",
    "publication": "cluster_defaults.updated",
}
PATHS = {
    "identity.auth_policy": (AUTH, "identity"),
    "billing.policy": (BILLING, "billing"),
    "reporting.export_policy": (REPORTING, "reporting"),
    "catalog.quota_defaults": (QUOTAS, "catalog"),
    "publication.cluster_defaults": (DEFAULTS, "publication"),
}


def audits(admin, module):
    return [
        row[0]
        for row in admin.execute(
            f"SELECT details FROM {module}.audit_events WHERE action=%s ORDER BY created_at",
            (ACTIONS[module],),
        )
    ]


async def versions(client, root):
    return {
        group: (await client.get(path, headers=auth(root))).json()["version"]
        for group, (path, _) in PATHS.items()
    }


def policy_file(tmp_path, groups, name="policies.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"version": 1, "groups": groups}))
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(("path", "change", "invalid", "module"), POLICIES)
async def test_policy_writes_are_versioned_audited_and_bounded(
    http, admin, path, change, invalid, module
):
    client, root, _, _ = http
    before = await client.get(path, headers=auth(root))
    assert before.status_code == 200, before.text
    assert before.json()["version"] == 1 and before.json()["managed"] is None
    assert before.headers["ETag"] == '"1"'
    written = await put_policy(client, root, path, change)
    assert written["version"] == 2
    assert (await client.get(path, headers=auth(root))).json() == written
    trail = audits(admin, module)
    assert len(trail) == 1 and trail[0] and "actor" not in trail[0]
    rejected = await client.put(path, headers=auth(root, 2), json=merged(body_of(written), invalid))
    assert rejected.status_code == 422, rejected.text
    # Writes need If-Match, and two writers holding the same version cannot both win.
    assert (await client.put(path, headers=auth(root), json=body_of(written))).status_code == 428
    results = await asyncio.gather(
        *(client.put(path, headers=auth(root, 2), json=body_of(written)) for _ in range(2))
    )
    assert sorted(r.status_code for r in results) == [200, 412]
    conflict = next(r for r in results if r.status_code == 412)
    assert conflict.json()["error"]["code"] == "version_conflict"


@pytest.mark.parametrize("path", [AUTH, BILLING, REPORTING, QUOTAS, DEFAULTS])
async def test_only_platform_administrators_read_or_write_policies(http, admin, path):
    client, root, _, _ = http
    await create_user(client, root, "policy-finance")
    user = admin.execute("SELECT id FROM identity.users WHERE login='policy-finance'").fetchone()
    admin.execute(
        "INSERT INTO identity.platform_role_bindings(user_id,role) VALUES (%s,'platform_finance')",
        user,
    )
    token = await login(client, "policy-finance")
    current = (await client.get(path, headers=auth(root))).json()
    assert (await client.get(path, headers=auth(token))).status_code == 403
    denied = await client.put(path, headers=auth(token, 1), json=body_of(current))
    assert denied.status_code == 403
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=auth(root))).json()["version"] == 1


async def test_identity_policy_applies_to_new_sessions_and_registrations(http, admin):
    client, root, _, _ = http
    policy = await put_policy(
        client,
        root,
        AUTH,
        {"sessions": {"session_ttl_seconds": 120}, "reserved_logins": ["Reserved-Team"]},
    )
    assert policy["reserved_logins"] == ["reserved-team"]
    created = await client.post(
        "/api/v1/auth/sessions", json={"login": "root-admin", "password": PASSWORD}
    )
    expires = datetime.fromisoformat(created.json()["expires_at"])
    now = admin.execute("SELECT now()").fetchone()[0]
    assert 100 < (expires - now).total_seconds() <= 121
    # Registration reads the policy fresh; once open, the reserved login is refused.
    admin.execute(
        "UPDATE identity.auth_policy SET registration=true,email_verification=true,"
        "version=version+1"
    )
    refused = await client.post(
        "/api/v1/auth/registrations",
        json={"login": "RESERVED-team", "password": PASSWORD, "email": "r@example.com"},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "invalid_login"


async def test_a_change_reaches_every_process_within_the_policy_ttl(http):
    client, root, _, settings = http
    other = create_app(settings.model_copy(update={"redis_namespace": "nexus:p2:" + uuid4().hex}))
    async with other.router.lifespan_context(other):
        cached = other.state.identity_service._settings
        assert (await cached.get()).sign_in.per_account == 10
        await put_policy(client, root, AUTH, {"sign_in": {"per_account": 3}})
        # The writer applies its own change at once; another process within the TTL.
        assert (await http[2].state.identity_service._settings.get()).sign_in.per_account == 3
        await asyncio.sleep(POLICY_TTL_SECONDS + 0.1)
        assert (await cached.get()).sign_in.per_account == 3
        billing = other.state.billing_settings.cached
        assert (await billing.get()).payments.notifications_per_ip_per_minute == 120
        await put_policy(
            client, root, BILLING, {"payments": {"notifications_per_ip_per_minute": 9}}
        )
        await asyncio.sleep(POLICY_TTL_SECONDS + 0.1)
        assert (await billing.get()).payments.notifications_per_ip_per_minute == 9


async def test_export_limits_apply_to_the_next_export(http):
    client, root, app, _ = http
    org = await create_org(client, root, "Exporting")
    await app.state.process_billing()
    await put_policy(
        client, root, REPORTING, {"exports": {"per_org_minute": 1, "ttl_seconds": 600}}
    )
    base = f"/api/v1/organizations/{org['id']}/exports"
    first = await client.post(base, headers=auth(root), json={"kind": "ledger"})
    assert first.status_code == 202, first.text
    job = first.json()
    lifetime = datetime.fromisoformat(job["expires_at"]) - datetime.fromisoformat(job["created_at"])
    assert 599 <= lifetime.total_seconds() <= 601
    limited = await client.post(base, headers=auth(root), json={"kind": "ledger"})
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "export_rate_limited"
    await put_policy(client, root, REPORTING, {"exports": {"per_org_minute": 2}})
    assert (await client.post(base, headers=auth(root), json={"kind": "ledger"})).status_code == 202


async def test_billing_currency_is_chosen_before_the_first_billing_fact(http):
    client, root, app, _ = http
    policy = await put_policy(client, root, BILLING, {"currency": "EUR"})
    assert policy["currency"] == "EUR"
    org = await create_org(client, root, "Euro tenant")
    await app.state.process_billing()
    account = await client.get(f"/api/v1/organizations/{org['id']}/account", headers=auth(root))
    assert account.json()["currency"] == "EUR"
    locked = await client.put(
        BILLING, headers=auth(root, policy["version"]), json={**body_of(policy), "currency": "USD"}
    )
    assert locked.status_code == 409 and locked.json()["error"]["code"] == "currency_locked"


async def test_resource_counts_are_platform_policies_of_a_whole_organization(http, admin):
    client, root, app, _ = http
    org = await create_org(client, root, "Counted")
    await app.state.process_billing()
    base = f"/api/v1/organizations/{org['id']}/quota-policies"
    for layer, scope in [("customer", "organization"), ("platform", "project")]:
        response = await client.put(
            f"{base}/{layer}/{scope}/{org['id']}/resources_per_key",
            headers=auth(root),
            json={"limit_value": 3},
        )
        assert response.status_code == 422, response.text
    written = await client.put(
        f"{base}/platform/organization/{org['id']}/vector_stores_per_organization",
        headers=auth(root),
        json={"limit_value": 3},
    )
    assert written.status_code == 201, written.text


async def declare_cluster_policy(client, root, admin, cid):
    nid, role, caps = admin.execute(
        "SELECT node_id,database_role,capabilities FROM publication.gateway_principals "
        "WHERE cluster_id=%s",
        (cid,),
    ).fetchone()
    upgraded = await client.put(
        f"/api/v1/gateway-clusters/{cid}/principals/{nid}",
        headers=auth(root),
        json={
            "node_id": str(nid),
            "database_role": role,
            "capabilities": [*caps, "relay.cluster-policy.v1"],
        },
    )
    assert upgraded.status_code == 200, upgraded.text


def latest_manifest(admin, cid):
    return admin.execute(
        "SELECT manifest FROM publication.config_snapshots WHERE cluster_id=%s "
        "ORDER BY revision DESC LIMIT 1",
        (cid,),
    ).fetchone()[0]


RELAY_FIELDS = ["max_attempts", "failover_budget_ms", "purge_grace_seconds"]
RELAY_FIELDS += ["stream_lifetime_seconds"]


async def test_cluster_policy_defaults_and_overrides_reach_the_snapshot(http, admin):
    client, root, _, _ = http
    async with provision_plane(http, admin, auto_publish=False) as plane:
        _, _, org, _, _, _, _, cid = plane
        path = f"/api/v1/gateway-clusters/{cid}/policy"
        view = (await client.get(path, headers=auth(root))).json()
        assert view["overrides"]["relay"] == dict.fromkeys(RELAY_FIELDS)
        assert view["effective"]["relay"]["max_attempts"] == 2
        assert view["effective"]["auto_publish"] == {
            "quiet_seconds": 3.0,
            "max_delay_seconds": 30.0,
        }
        # At the built-in values a release carries no policy and needs no new capability.
        manifest = latest_manifest(admin, cid)
        assert "cluster_policy" not in manifest["resources"]
        assert "resource_limits" not in manifest["resources"]
        assert "relay.cluster-policy.v1" not in manifest["required_capabilities"]
        await put_policy(client, root, DEFAULTS, {"relay": {"failover_budget_ms": 5000}})
        view = await put_overrides(client, root, cid, {"relay": {"max_attempts": 1}})
        assert view["effective"]["relay"] == {
            "max_attempts": 1,
            "failover_budget_ms": 5000,
            "purge_grace_seconds": 604800,
            "stream_lifetime_seconds": 1800,
        }
        # The override must still make sense against the defaults.
        invalid = await client.put(
            path,
            headers=auth(root, view["version"]),
            json=merged(view["overrides"], {"auto_publish": {"quiet_seconds": 60}}),
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "invalid_cluster_policy"
        await put_policy(client, root, QUOTAS, {"resources_per_key": 50})
        quota = await client.put(
            f"/api/v1/organizations/{org['id']}/quota-policies"
            f"/platform/organization/{org['id']}/resources_per_key",
            headers=auth(root),
            json={"limit_value": 4},
        )
        assert quota.status_code == 201, quota.text
        # A node that cannot enforce the policy holds the release back.
        blocked = await client.post(
            f"/api/v1/gateway-clusters/{cid}/releases", headers=headers(root, "old"), json={}
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "unsupported_capability"
        await declare_cluster_policy(client, root, admin, cid)
        released = await client.post(
            f"/api/v1/gateway-clusters/{cid}/releases", headers=headers(root, "new"), json={}
        )
        assert released.status_code == 202, released.text
        snapshot = latest_manifest(admin, cid)
        resources = snapshot["resources"]
        assert resources["cluster_policy"] == view["effective"]["relay"]
        assert resources["resource_limits"]["default"]["resources_per_key"] == 50
        assert resources["resource_limits"]["organizations"] == [
            {
                "organization_id": org["id"],
                "resources_per_key": 4,
                "resources_per_organization": 100000,
                "vector_stores_per_key": 100,
                "vector_stores_per_organization": 1000,
            }
        ]
        assert "relay.cluster-policy.v1" in snapshot["required_capabilities"]
        # Rate quotas keep their shape for the Redis limiter; counts never appear there.
        assert {q["metric"] for q in resources["quota_policies"]} <= {"rpm", "tpm", "concurrency"}
        unknown = await client.get(f"/api/v1/gateway-clusters/{uuid4()}/policy", headers=auth(root))
        assert unknown.status_code == 404


async def test_policy_changes_publish_automatically(http, admin):
    client, root, app, _ = http
    async with provision_plane(http, admin, auto_publish=True) as plane:
        cid = plane[-1]
        await declare_cluster_policy(client, root, admin, cid)
        await put_policy(client, root, DEFAULTS, {"auto_publish": {"quiet_seconds": 0}})
        auto = AutoPublisher(app.state.publication, app.state.publication.tx, instant, poll=30)
        await auto.tick()
        revision = admin.execute(
            "SELECT desired_revision FROM publication.gateway_clusters WHERE id=%s", (cid,)
        ).fetchone()[0]
        # Timing alone changes no snapshot; a Relay value does, and the worker publishes it.
        assert (await auto.tick())[0] == 0
        await put_overrides(client, root, cid, {"relay": {"stream_lifetime_seconds": 60}})
        assert (await auto.tick())[0] == 1
        manifest = latest_manifest(admin, cid)
        assert manifest["revision"] == str(revision + 1)
        assert manifest["resources"]["cluster_policy"]["stream_lifetime_seconds"] == 60
        # A change to the defaults reaches clusters that follow them as well.
        await put_policy(client, root, DEFAULTS, {"relay": {"max_attempts": 3}})
        assert (await auto.tick())[0] == 1
        assert latest_manifest(admin, cid)["resources"]["cluster_policy"]["max_attempts"] == 3
        trigger = admin.execute(
            "SELECT details FROM publication.audit_events WHERE action='config.released' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]
        assert "relay_policy" in trigger["trigger"]


async def test_import_writes_initial_values_once_with_an_audit(http, admin, tmp_path):
    client, root, _, settings = http
    groups = {
        "identity.auth_policy": {"mode": "initial", "values": {"sign_in": {"per_ip": 33}}},
        "billing.policy": {"mode": "initial", "values": {"currency": "CNY"}},
        "reporting.export_policy": {"mode": "initial", "values": {"exports": {"max_rows": 20}}},
        "catalog.quota_defaults": {"mode": "initial", "values": {"vector_stores_per_key": 8}},
        "publication.cluster_defaults": {
            "mode": "initial",
            "values": {"relay": {"max_attempts": 3}},
        },
    }
    path, digest = policy_file(tmp_path, groups)
    dry = await import_policies(settings, path, dry_run=True)
    assert dry["dry_run"] and dry["file_sha256"] == digest
    assert dry["groups"]["identity.auth_policy"] == {
        "status": "applied",
        "mode": "initial",
        "managed": False,
        "changes": {"sign_in.per_ip": [60, 33]},
    }
    assert dry["groups"]["billing.policy"]["changes"] == {"currency": ["USD", "CNY"]}
    assert await versions(client, root) == dict.fromkeys(PATHS, 1), "a dry run writes nothing"
    applied = await import_policies(settings, path)
    assert {g["status"] for g in applied["groups"].values()} == {"applied"}
    assert await versions(client, root) == dict.fromkeys(PATHS, 2)
    assert (await client.get(AUTH, headers=auth(root))).json()["sign_in"]["per_ip"] == 33
    assert (await client.get(BILLING, headers=auth(root))).json()["currency"] == "CNY"
    for _, module in PATHS.values():
        (detail,) = audits(admin, module)
        assert detail["actor"] == "config-import" and detail["file_sha256"] == digest
        assert detail["mode"] == "initial"
    # Initial values are written once: the same file changes nothing, and neither does a new
    # file once an administrator has taken the group over.
    again = await import_policies(settings, path)
    assert {g["status"] for g in again["groups"].values()} == {"unchanged"}
    assert await versions(client, root) == dict.fromkeys(PATHS, 2)
    await put_policy(client, root, REPORTING, {"exports": {"max_rows": 30}})
    groups["reporting.export_policy"]["values"] = {"exports": {"max_rows": 40}}
    path, _ = policy_file(tmp_path, groups, "second.json")
    later = await import_policies(settings, path)
    assert later["groups"]["reporting.export_policy"]["status"] == "skipped"
    assert (await client.get(REPORTING, headers=auth(root))).json()["exports"]["max_rows"] == 30


async def test_an_invalid_group_rejects_the_whole_file(http, tmp_path):
    client, root, _, settings = http
    groups = {
        "identity.auth_policy": {"mode": "managed", "values": {"sign_in": {"per_ip": 33}}},
        "billing.policy": {"mode": "initial", "values": {"payments": {"typo": 1}}},
        "reporting.export_policy": {"mode": "managed", "values": {"exports": {"max_rows": 0}}},
    }
    path, _ = policy_file(tmp_path, groups)
    with pytest.raises(PolicyRejected, match="billing.policy: invalid payments.typo"):
        await import_policies(settings, path)
    del groups["billing.policy"]
    path, _ = policy_file(tmp_path, groups)
    with pytest.raises(PolicyRejected, match="reporting.export_policy: invalid exports.max_rows"):
        await import_policies(settings, path)
    assert await versions(client, root) == dict.fromkeys(PATHS, 1)
    assert (await client.get(AUTH, headers=auth(root))).json()["managed"] is None


async def test_managed_groups_are_written_only_by_imports(http, admin, tmp_path):
    client, root, _, settings = http
    groups = {
        "reporting.export_policy": {"mode": "managed", "values": {"exports": {"max_rows": 20}}},
        "publication.cluster_defaults": {
            "mode": "managed",
            "values": {"relay": {"max_attempts": 3}},
        },
    }
    path, digest = policy_file(tmp_path, groups)
    result = await import_policies(settings, path)
    assert result["groups"]["reporting.export_policy"]["changes"] == {
        "exports.max_rows": [10000, 20],
        "managed": [False, True],
    }
    view = (await client.get(REPORTING, headers=auth(root))).json()
    assert view["managed"]["file_sha256"] == digest and view["exports"]["max_rows"] == 20
    for target in (REPORTING, DEFAULTS):
        current = (await client.get(target, headers=auth(root))).json()
        refused = await client.put(
            target, headers=auth(root, current["version"]), json=body_of(current)
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "policy_managed_externally"
    # The file stays the single source: importing it again changes nothing, editing it does.
    again = await import_policies(settings, path)
    assert again["groups"]["reporting.export_policy"]["status"] == "unchanged"
    groups["reporting.export_policy"]["values"] = {"exports": {"max_rows": 25}}
    path, edited = policy_file(tmp_path, groups, "edited.json")
    changed = await import_policies(settings, path)
    assert changed["groups"]["reporting.export_policy"]["changes"] == {"exports.max_rows": [20, 25]}
    view = (await client.get(REPORTING, headers=auth(root))).json()
    assert view["managed"]["file_sha256"] == edited and view["version"] == 3
    # Declaring the group initial unlocks it without touching its values.
    groups["reporting.export_policy"] = {"mode": "initial", "values": {"exports": {"max_rows": 1}}}
    path, _ = policy_file(tmp_path, groups, "unlock.json")
    unlocked = await import_policies(settings, path)
    assert unlocked["groups"]["reporting.export_policy"]["changes"] == {"managed": [True, False]}
    view = (await client.get(REPORTING, headers=auth(root))).json()
    assert view["managed"] is None and view["exports"]["max_rows"] == 25
    assert (await put_policy(client, root, REPORTING, {"exports": {"max_rows": 26}}))[
        "version"
    ] == 5
    assert [d.get("mode") for d in audits(admin, "reporting")] == [
        "managed",
        "managed",
        "initial",
        None,
    ]


async def test_cli_import_reports_changes_and_refuses_invalid_files(
    service_directory, admin, tmp_path
):
    def run(path, *extra):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "nexus.cli",
                "--env-file",
                str(service_directory / ".env.api"),
                "settings",
                "import",
                "--file",
                path,
                *extra,
            ],
            env=dict(os.environ),
            capture_output=True,
            text=True,
        )

    path = tmp_path / "policies.yaml"
    path.write_text(
        "version: 1\n"
        "groups:\n"
        "  reporting.export_policy:\n"
        "    mode: managed\n"
        "    values: {exports: {per_org_minute: 4}}\n"
    )
    dry = await asyncio.to_thread(run, str(path), "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert json.loads(dry.stdout)["groups"]["reporting.export_policy"]["status"] == "applied"
    assert admin.execute("SELECT per_org_minute FROM reporting.export_policy").fetchone() == (10,)
    done = await asyncio.to_thread(run, str(path))
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["groups"]["reporting.export_policy"]["changes"] == {
        "exports.per_org_minute": [10, 4],
        "managed": [False, True],
    }
    assert admin.execute("SELECT per_org_minute FROM reporting.export_policy").fetchone() == (4,)
    path.write_text("version: 1\ngroups:\n  reporting.export_policy: {mode: managed, values: x}\n")
    refused = await asyncio.to_thread(run, str(path))
    assert refused.returncode == 1 and "nothing was written" in refused.stderr
