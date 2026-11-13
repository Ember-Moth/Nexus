import base64
import json
from uuid import uuid4

import httpx
import psycopg
import pytest
from pydantic import SecretStr
from test_identity_api import auth, create_org, create_user, login
from test_project_credentials import account, developer, key, project

from nexus.api import create_app
from nexus.infrastructure.credential_cipher import CredentialCipher

pytestmark = pytest.mark.integration
UPSTREAM_SECRET = "provider-secret-do-not-echo"


async def make_model(client, root, name, protocols=None):
    response = await client.post(
        "/api/v1/models",
        headers=auth(root),
        json={
            "public_name": name,
            "display_name": name,
            "protocols": protocols or ["openai_compatible"],
            "max_input_tokens": 128000,
            "max_output_tokens": 8192,
            "capabilities": {"streaming": True, "tools": True},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def make_upstream(client, root, name, protocol="openai_compatible"):
    response = await client.post(
        "/api/v1/upstreams",
        headers=auth(root),
        json={
            "name": name,
            "base_url": "https://api.example.com/v1",
            "protocol_family": protocol,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def make_credential(client, root, upstream):
    response = await client.post(
        f"/api/v1/upstreams/{upstream['id']}/credentials",
        headers=auth(root),
        json={"name": "Production", "secret": UPSTREAM_SECRET},
    )
    assert response.status_code == 201, response.text
    assert UPSTREAM_SECRET not in response.text and "ciphertext" not in response.text
    return response.json()


async def make_deployment(client, root, model, upstream, credential, name=None):
    response = await client.post(
        "/api/v1/deployments",
        headers=auth(root),
        json={
            "name": name or str(uuid4()),
            "model_id": model["id"],
            "upstream_id": upstream["id"],
            "credential_id": credential["id"],
            "upstream_model": "vendor/model",
            "region": "global",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def make_policy(
    client, token, org, model_ids, *, layer="platform", scope="organization", scope_id=None
):
    response = await client.post(
        f"/api/v1/organizations/{org['id']}/access-policies",
        headers=auth(token),
        json={
            "layer": layer,
            "scope_type": scope,
            "scope_id": scope_id,
            "allowed_models": model_ids,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def visible(client, token, org, **context):
    response = await client.get(
        "/api/v1/models", headers=auth(token), params={"organization_id": org["id"], **context}
    )
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["items"]}


async def test_catalog_credentials_are_encrypted_and_never_read_back(http, admin):
    client, root, _, settings = http
    model = await make_model(client, root, "model-a")
    assert model["configured"] is False
    upstream = await make_upstream(client, root, "Provider A")
    credential = await make_credential(client, root, upstream)
    deployment = await make_deployment(client, root, model, upstream, credential)
    assert deployment["configured"] is True
    assert (await client.get("/api/v1/models/" + model["id"], headers=auth(root))).json()[
        "configured"
    ] is True
    material = admin.execute(
        "SELECT ciphertext,key_version FROM catalog.upstream_credentials WHERE id=%s",
        (credential["id"],),
    ).fetchone()
    assert UPSTREAM_SECRET.encode() not in bytes(material[0])
    cipher = CredentialCipher(
        1, {1: base64.urlsafe_b64decode(settings.encryption_key.get_secret_value())}
    )
    from uuid import UUID

    assert (
        cipher.decrypt(
            bytes(material[0]), UUID(upstream["id"]), UUID(credential["id"]), material[1]
        )
        == UPSTREAM_SECRET
    )
    paths = [
        "/api/v1/upstreams",
        "/api/v1/deployments",
        f"/api/v1/upstreams/{upstream['id']}/credentials",
        f"/api/v1/upstreams/{upstream['id']}/credentials/{credential['id']}",
    ]
    for path in paths:
        response = await client.get(path, headers=auth(root))
        assert response.status_code == 200
        assert UPSTREAM_SECRET not in response.text and "ciphertext" not in response.text
    assert UPSTREAM_SECRET not in json.dumps(
        admin.execute("SELECT details FROM catalog.audit_events").fetchall()
    )
    assert (await client.get(paths[-1] + "/secret", headers=auth(root))).status_code == 404
    outsider = await create_user(client, root, "ordinary-customer")
    token = await login(client, outsider["user"]["login"])
    for path in ["/api/v1/upstreams", "/api/v1/deployments", paths[-1]]:
        assert (await client.get(path, headers=auth(token))).status_code == 403
    assert (
        await client.post(
            "/api/v1/models",
            headers=auth(token),
            json={
                "public_name": "forbidden",
                "display_name": "Forbidden",
                "protocols": ["openai_compatible"],
                "max_input_tokens": 10,
                "max_output_tokens": 5,
            },
        )
    ).status_code == 403


async def test_model_access_defaults_closed_and_intersects_all_layers(http):
    client, root, _, _ = http
    owner = await create_user(client, root, "model-owner")
    token = await login(client, "model-owner")
    org = await create_org(client, root, "Model permissions", owner["user"]["id"])
    a = await make_model(client, root, "model-a")
    b = await make_model(client, root, "model-b")
    upstream = await make_upstream(client, root, "Provider")
    cred = await make_credential(client, root, upstream)
    await make_deployment(client, root, a, upstream, cred)
    await make_deployment(client, root, b, upstream, cred)
    assert await visible(client, token, org) == set()
    assert (
        await client.get(
            "/api/v1/models/" + a["id"], headers=auth(token), params={"organization_id": org["id"]}
        )
    ).status_code == 404
    baseline = await make_policy(client, root, org, [a["id"], b["id"]])
    assert await visible(client, token, org) == {a["id"], b["id"]}
    custom = await make_policy(client, token, org, [a["id"]], layer="customer")
    assert await visible(client, token, org) == {a["id"]}
    p = await project(client, token, org)
    sa = await account(client, token, org, p["id"])
    application_key = (await key(client, token, org, sa["id"])).json()["key"]
    policy = await make_policy(client, root, org, [a["id"]], scope="project", scope_id=p["id"])
    restricted = await make_policy(
        client, token, org, [], layer="customer", scope="key", scope_id=application_key["id"]
    )
    assert await visible(client, token, org, project_id=p["id"]) == {a["id"]}
    assert await visible(client, token, org, key_id=application_key["id"]) == set()
    path = f"/api/v1/organizations/{org['id']}/access-policies"
    expansion = await client.patch(
        path + "/" + restricted["id"], headers=auth(token, 1), json={"allowed_models": [b["id"]]}
    )
    assert expansion.status_code == 403
    expansion = await client.patch(
        path + "/" + policy["id"], headers=auth(token, 1), json={"allowed_models": [a["id"]]}
    )
    assert expansion.status_code == 403
    changed = await client.patch(
        path + "/" + restricted["id"], headers=auth(token, 1), json={"allowed_models": [a["id"]]}
    )
    assert changed.status_code == 200
    assert await visible(client, token, org, key_id=application_key["id"]) == {a["id"]}
    assert (
        await client.patch(
            path + "/" + baseline["id"], headers=auth(root, 1), json={"allowed_models": [b["id"]]}
        )
    ).status_code == 200
    assert await visible(client, token, org, key_id=application_key["id"]) == set()
    assert (
        await client.patch(
            path + "/" + custom["id"], headers=auth(token, 1), json={"allowed_models": [b["id"]]}
        )
    ).status_code == 200
    assert await visible(client, token, org) == {b["id"]}
    assert await visible(client, token, org, project_id=p["id"]) == set()


async def test_catalog_validates_protocols_bindings_versions_and_urls(http, admin):
    client, root, _, _ = http
    a = await make_model(client, root, "openai-model")
    b = await make_model(client, root, "anthropic-model", ["anthropic"])
    upstream = await make_upstream(client, root, "Primary")
    other = await make_upstream(client, root, "Other")
    cred = await make_credential(client, root, upstream)
    foreign = await make_credential(client, root, other)
    payload = {
        "name": "Mismatch",
        "model_id": a["id"],
        "upstream_id": upstream["id"],
        "credential_id": foreign["id"],
        "upstream_model": "vendor/model",
    }
    assert (
        await client.post("/api/v1/deployments", headers=auth(root), json=payload)
    ).status_code == 404
    payload.update(model_id=b["id"], credential_id=cred["id"])
    assert (
        await client.post("/api/v1/deployments", headers=auth(root), json=payload)
    ).status_code == 409
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            "INSERT INTO catalog.deployments(id,name,model_id,upstream_id,credential_id,"
            "upstream_model,region) VALUES(%s,'invalid',%s,%s,%s,'vendor/model','global')",
            (uuid4(), a["id"], upstream["id"], foreign["id"]),
        )
    deployment = await make_deployment(client, root, a, upstream, cred)
    assert (
        await client.patch(
            "/api/v1/models/" + a["id"], headers=auth(root, 1), json={"protocols": ["anthropic"]}
        )
    ).status_code == 409
    assert (
        await client.patch(
            "/api/v1/models/" + a["id"], headers=auth(root), json={"display_name": "New"}
        )
    ).status_code == 428
    assert (
        await client.patch(
            "/api/v1/models/" + a["id"], headers=auth(root, 99), json={"display_name": "New"}
        )
    ).status_code == 412
    secret_marker = "do-not-echo-secret"
    for url in [
        "http://api.example.com",
        "https://user:" + secret_marker + "@example.com",
        "https://example.com?key=" + secret_marker,
        "https://example.com/#fragment",
    ]:
        result = await client.post(
            "/api/v1/upstreams",
            headers=auth(root),
            json={"name": "Bad URL", "base_url": url, "protocol_family": "openai_compatible"},
        )
        assert result.status_code == 422
        assert secret_marker not in result.text
    assert (
        await client.patch(
            "/api/v1/upstreams/" + upstream["id"],
            headers=auth(root, 1),
            json={"base_url": "https://new.example.com"},
        )
    ).status_code == 409
    assert (
        await client.patch(
            "/api/v1/upstreams/" + upstream["id"],
            headers=auth(root, 1),
            json={"status": "disabled"},
        )
    ).status_code == 200
    changed = await client.patch(
        "/api/v1/upstreams/" + upstream["id"],
        headers=auth(root, 2),
        json={"base_url": "https://new.example.com", "status": "active"},
    )
    assert changed.status_code == 200
    assert (
        await client.patch(
            "/api/v1/deployments/" + deployment["id"],
            headers=auth(root, 1),
            json={"model_id": b["id"]},
        )
    ).status_code == 422


async def test_revocation_rotation_and_disabled_parents_change_configured_state(http):
    client, root, _, _ = http
    org = await create_org(client, root, "Visibility lifecycle")
    model = await make_model(client, root, "lifecycle-model")
    upstream = await make_upstream(client, root, "Lifecycle upstream")
    first = await make_credential(client, root, upstream)
    second = await make_credential(client, root, upstream)
    deployment = await make_deployment(client, root, model, upstream, first)
    await make_policy(client, root, org, [model["id"]])
    assert await visible(client, root, org) == {model["id"]}
    revoked = await client.post(
        f"/api/v1/upstreams/{upstream['id']}/credentials/{first['id']}/revoke",
        headers=auth(root, 1),
    )
    assert revoked.status_code == 200
    assert await visible(client, root, org) == set()
    changed = await client.patch(
        "/api/v1/deployments/" + deployment["id"],
        headers=auth(root, 1),
        json={"credential_id": second["id"]},
    )
    assert changed.status_code == 200, changed.text
    assert await visible(client, root, org) == {model["id"]}
    assert (
        await client.patch(
            "/api/v1/models/" + model["id"], headers=auth(root, 1), json={"status": "disabled"}
        )
    ).status_code == 200
    assert await visible(client, root, org) == set()
    assert (
        await client.patch(
            "/api/v1/models/" + model["id"], headers=auth(root, 2), json={"status": "active"}
        )
    ).status_code == 200
    assert await visible(client, root, org) == {model["id"]}
    assert (
        await client.patch(
            "/api/v1/deployments/" + deployment["id"],
            headers=auth(root, 2),
            json={"status": "archived"},
        )
    ).status_code == 200
    assert (
        await client.patch(
            "/api/v1/deployments/" + deployment["id"],
            headers=auth(root, 3),
            json={"status": "active"},
        )
    ).status_code == 409
    assert await visible(client, root, org) == set()


async def test_policy_tenant_and_project_boundaries(http):
    client, root, _, _ = http
    org = await create_org(client, root, "Policies A")
    other = await create_org(client, root, "Policies B")
    one = await project(client, root, org, "Granted")
    two = await project(client, root, org, "Hidden")
    token, _, _ = await developer(client, root, org, one["id"])
    model = await make_model(client, root, "policy-model")
    upstream = await make_upstream(client, root, "Policy upstream")
    cred = await make_credential(client, root, upstream)
    await make_deployment(client, root, model, upstream, cred)
    await make_policy(client, root, org, [model["id"]])
    hidden = await make_policy(
        client, root, org, [model["id"]], scope="project", scope_id=two["id"]
    )
    own = await make_policy(
        client, token, org, [model["id"]], layer="customer", scope="project", scope_id=one["id"]
    )
    path = f"/api/v1/organizations/{org['id']}/access-policies"
    listed = (await client.get(path, headers=auth(token))).json()["items"]
    assert hidden["id"] not in {p["id"] for p in listed}
    assert own["id"] in {p["id"] for p in listed}
    assert (await client.get(path + "/" + hidden["id"], headers=auth(token))).status_code == 404
    assert (
        await client.get(
            "/api/v1/models", headers=auth(token), params={"organization_id": other["id"]}
        )
    ).status_code == 404
    assert (
        await client.get(
            "/api/v1/models",
            headers=auth(token),
            params={"organization_id": org["id"], "project_id": two["id"]},
        )
    ).status_code == 404
    result = await client.post(
        path,
        headers=auth(token),
        json={"layer": "customer", "scope_type": "organization", "allowed_models": [model["id"]]},
    )
    assert result.status_code == 403
    assert (
        await client.patch(
            path + "/" + own["id"], headers=auth(token, 1), json={"allowed_models": []}
        )
    ).status_code == 200
    assert await visible(client, token, org, project_id=one["id"]) == set()
    assert (await client.get("/api/v1/models", headers=auth(token))).status_code == 403


async def test_unknown_models_and_policy_database_constraints(http, admin):
    client, root, _, _ = http
    org = await create_org(client, root, "Policy integrity")
    path = f"/api/v1/organizations/{org['id']}/access-policies"
    result = await client.post(
        path,
        headers=auth(root),
        json={"layer": "platform", "scope_type": "organization", "allowed_models": [str(uuid4())]},
    )
    assert result.status_code == 422
    model = await make_model(client, root, "known-model")
    policy = await make_policy(client, root, org, [model["id"]])
    duplicate = await client.post(
        path,
        headers=auth(root),
        json={"layer": "platform", "scope_type": "organization", "allowed_models": [model["id"]]},
    )
    assert duplicate.status_code == 409
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        admin.execute(
            "INSERT INTO identity.access_policy_models(policy_id,model_id) VALUES(%s,%s)",
            (policy["id"], uuid4()),
        )
    assert (
        await client.patch(
            path + "/" + policy["id"], headers=auth(root, 99), json={"allowed_models": []}
        )
    ).status_code == 412
    assert admin.execute("SELECT count(*) FROM identity.access_policy_models").fetchone()[0] == 1


async def test_keyring_rollover_and_missing_old_key_affect_configured_deployments(http):
    client, root, _, settings = http
    model = await make_model(client, root, "keyring-model")
    upstream = await make_upstream(client, root, "Keyring upstream")
    old = await make_credential(client, root, upstream)
    dep = await make_deployment(client, root, model, upstream, old)
    second_root = SecretStr(base64.urlsafe_b64encode(b"z" * 32).decode())
    retained = settings.model_copy(
        update={
            "encryption_key": second_root,
            "encryption_key_version": 2,
            "previous_encryption_keys": {1: settings.encryption_key},
        }
    )
    app = create_app(retained)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as c,
    ):
        assert (await c.get("/api/v1/deployments/" + dep["id"], headers=auth(root))).json()[
            "configured"
        ] is True
        new = await make_credential(c, root, upstream)
        assert new["key_version"] == 2
    missing = retained.model_copy(update={"previous_encryption_keys": {}})
    app = create_app(missing)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as c,
    ):
        assert (await c.get("/api/v1/deployments/" + dep["id"], headers=auth(root))).json()[
            "configured"
        ] is False
        response = await c.patch(
            "/api/v1/deployments/" + dep["id"],
            headers=auth(root, 1),
            json={"name": "needs-old-key"},
        )
        assert response.status_code == 409


async def test_invalid_mapping_can_be_disabled_and_audit_is_reconstructable(http, admin):
    client, root, _, _ = http
    model = await make_model(client, root, "audit-model")
    upstream = await make_upstream(client, root, "Audit upstream")
    credential = await make_credential(client, root, upstream)
    deployment = await make_deployment(client, root, model, upstream, credential)
    await client.post(
        f"/api/v1/upstreams/{upstream['id']}/credentials/{credential['id']}/revoke",
        headers=auth(root, 1),
    )
    disabled = await client.patch(
        "/api/v1/deployments/" + deployment["id"],
        headers=auth(root, 1),
        json={"status": "disabled"},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["configured"] is False
    assert (
        await client.patch(
            "/api/v1/deployments/" + deployment["id"],
            headers=auth(root, 2),
            json={"status": "active"},
        )
    ).status_code == 409
    evidence = admin.execute(
        "SELECT details FROM catalog.audit_events WHERE action='deployment.updated'"
    ).fetchone()[0]
    assert evidence["before"]["status"] == "active" and evidence["after"]["status"] == "disabled"
    assert UPSTREAM_SECRET not in json.dumps(evidence)
    org = await create_org(client, root, "Policy audit")
    policy = await make_policy(client, root, org, [model["id"]])
    response = await client.patch(
        f"/api/v1/organizations/{org['id']}/access-policies/{policy['id']}",
        headers=auth(root, 1),
        json={"allowed_models": []},
    )
    assert response.status_code == 200
    details = admin.execute(
        "SELECT details FROM identity.audit_events WHERE action='access_policy.updated'"
    ).fetchone()[0]
    assert (
        details["before"]["allowed_models"] == [model["id"]]
        and details["after"]["allowed_models"] == []
    )
