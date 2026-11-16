"""Runtime responses of the late-typed operations match their declared models exactly."""

import base64
from uuid import uuid4

import pytest
from nacl.public import PrivateKey
from test_billing import settle_success, setup_model, setup_wallet
from test_identity_api import auth, create_org

from nexus.modules.billing.application.payments.schemas import GatewayType
from nexus.modules.catalog.application.routing_schemas import (
    QuotaPolicyResult,
    QuotaPolicyView,
    RoutePolicyView,
)
from nexus.modules.publication.application.schemas import ClusterView
from nexus.modules.reporting.application.schemas import ExportJob, RequestRecord

pytestmark = pytest.mark.integration


def exact(model, body):
    """The body carries every declared field and nothing else."""
    assert set(body) == set(model.model_fields), sorted(set(body) ^ set(model.model_fields))
    return model.model_validate(body)


async def pages(client, token, path, limit):
    items, cursor = [], None
    while True:
        params = {"limit": limit} | ({"cursor": cursor} if cursor else {})
        response = await client.get(path, headers=auth(token), params=params)
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"items", "next_cursor"}
        assert len(body["items"]) <= limit
        items += body["items"]
        if not (cursor := body["next_cursor"]):
            return items


async def test_route_policy_put_creates_once_then_updates_conditionally(http):
    client, root, _, _ = http
    org, _, _ = await setup_wallet(http)
    model, dep, _, _, _ = await setup_model(http, org)
    other = await create_org(client, root, "Second routing tenant")
    path = f"/api/v1/route-policies/{org['id']}/{model['id']}"
    body = {"candidates": [{"deployment_id": dep["id"], "priority": 1, "weight": 2}]}

    created = await client.put(path, headers=auth(root), json=body)
    assert created.status_code == 201, created.text
    assert created.headers["etag"] == '"1"'
    view = exact(RoutePolicyView, created.json())
    assert (str(view.organization_id), str(view.model_id), view.version) == (
        org["id"],
        model["id"],
        1,
    )
    again = await client.put(path, headers=auth(root), json=body)
    assert again.status_code == 428, again.text
    assert again.json()["error"]["code"] == "precondition_required"
    stale = await client.put(path, headers=auth(root, 2), json=body)
    assert stale.status_code == 412, stale.text
    updated = await client.put(path, headers=auth(root, 1), json={"candidates": body["candidates"]})
    assert updated.status_code == 200, updated.text
    assert updated.headers["etag"] == '"2"'
    assert exact(RoutePolicyView, updated.json()).version == 2
    absent = f"/api/v1/route-policies/{other['id']}/{uuid4()}"
    assert (await client.put(absent, headers=auth(root, 1), json=body)).status_code == 412
    extra = await client.put(path, headers=auth(root, 2), json=body | {"model_id": model["id"]})
    assert extra.status_code == 422

    second = f"/api/v1/route-policies/{other['id']}/{model['id']}"
    assert (await client.put(second, headers=auth(root), json=body)).status_code == 201
    listed = await pages(client, root, "/api/v1/route-policies", 1)
    assert [(r["organization_id"], r["model_id"]) for r in listed] == sorted(
        [(org["id"], model["id"]), (other["id"], model["id"])]
    )
    for row in listed:
        exact(RoutePolicyView, row)


async def test_quota_policy_put_addresses_the_natural_key(http):
    client, root, _, _ = http
    org = await create_org(client, root, "Quota tenant")
    base = f"/api/v1/organizations/{org['id']}/quota-policies"
    path = f"{base}/platform/organization/{org['id']}/rpm"

    created = await client.put(path, headers=auth(root), json={"limit_value": 5})
    assert created.status_code == 201, created.text
    assert created.headers["etag"] == '"1"'
    result = exact(QuotaPolicyResult, created.json())
    assert (result.layer, result.scope_type, str(result.scope_id), result.metric) == (
        "platform",
        "organization",
        org["id"],
        "rpm",
    )
    assert (await client.put(path, headers=auth(root), json={"limit_value": 6})).status_code == 428
    assert (
        await client.put(path, headers=auth(root, 9), json={"limit_value": 6})
    ).status_code == 412
    updated = await client.put(path, headers=auth(root, 1), json={"limit_value": 6})
    assert updated.status_code == 200, updated.text
    assert exact(QuotaPolicyResult, updated.json()).id == result.id
    assert updated.json()["version"] == 2 and updated.json()["limit_value"] == 6
    foreign = f"{base}/platform/organization/{uuid4()}/rpm"
    assert (
        await client.put(foreign, headers=auth(root), json={"limit_value": 1})
    ).status_code == 404
    invalid = f"{base}/platform/organization/{org['id']}/rps"
    assert (
        await client.put(invalid, headers=auth(root), json={"limit_value": 1})
    ).status_code == 422

    tpm = f"{base}/platform/organization/{org['id']}/tpm"
    assert (await client.put(tpm, headers=auth(root), json={"limit_value": 9})).status_code == 201
    listed = await pages(client, root, base, 1)
    assert sorted(row["metric"] for row in listed) == ["rpm", "tpm"]
    for row in listed:
        view = exact(QuotaPolicyView, row)
        assert str(view.organization_id) == str(view.scope_id) == org["id"]
    malformed = await client.get(base, headers=auth(root), params={"cursor": "not-a-cursor"})
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "invalid_cursor"


async def test_gateway_clusters_paginate_with_the_created_shape(http):
    client, root, _, _ = http
    org, _, _ = await setup_wallet(http)
    _, dep, _, _, _ = await setup_model(http, org)
    created = []
    for name in ("east", "west", "north"):
        response = await client.post(
            "/api/v1/gateway-clusters",
            headers=auth(root),
            json={
                "name": name,
                "region": "test",
                "organization_ids": [org["id"]],
                "deployment_ids": [dep["id"]],
                "public_key": base64.b64encode(bytes(PrivateKey.generate().public_key)).decode(),
            },
        )
        assert response.status_code == 201, response.text
        created.append(exact(ClusterView, response.json()).model_dump(mode="json"))
    listed = await pages(client, root, "/api/v1/gateway-clusters", 2)
    assert listed == sorted(created, key=lambda cluster: cluster["id"])


async def test_report_details_exports_and_gateway_types_match_their_models(http):
    client, root, app, _ = http
    org, _, _ = await setup_wallet(http)
    _, dep, _, _, admission = await setup_model(http, org)
    await app.state.reservations.reserve(admission)
    await settle_success(app, admission, dep)
    await app.state.reports.project()
    base = f"/api/v1/organizations/{org['id']}"
    listed = (await client.get(base + "/requests", headers=auth(root))).json()["items"]
    detail = await client.get(base + "/requests/" + listed[0]["id"], headers=auth(root))
    assert detail.status_code == 200, detail.text
    assert detail.json() == listed[0]
    exact(RequestRecord, detail.json())

    queued = await client.post(base + "/exports", headers=auth(root), json={"kind": "ledger"})
    assert queued.status_code == 202, queued.text
    path = base + "/exports/" + queued.json()["id"]
    assert exact(ExportJob, (await client.get(path, headers=auth(root))).json()).state == "queued"
    await app.state.reports.process_export()
    ready = exact(ExportJob, (await client.get(path, headers=auth(root))).json())
    assert ready.state == "ready" and ready.row_count >= 1

    types = await client.get("/api/v1/payment-gateway-types", headers=auth(root))
    assert types.status_code == 200, types.text
    assert set(types.json()) == {"items"}
    assert {exact(GatewayType, item).adapter_type for item in types.json()["items"]} >= {
        "antom",
        "epay",
        "stripe",
    }
