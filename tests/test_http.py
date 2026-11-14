from contextlib import asynccontextmanager

import httpx

from nexus.api import build_server, create_app
from nexus.http import ErrorResponse, ValidationErrorResponse


async def test_liveness_does_not_claim_dependency_readiness():
    @asynccontextmanager
    async def lifespan(app):
        async def not_ready():
            return {"postgres": "unavailable", "redis": "ok"}

        app.state.readiness = not_ready
        yield

    app = build_server(lifespan)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/health/live")).status_code == 200
        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        assert (await client.get("/docs")).status_code == 404
        assert (await client.post("/api/v1/bootstrap-admin")).status_code == 404


async def test_unexpected_errors_do_not_escape_or_echo_secrets(caplog):
    @asynccontextmanager
    async def lifespan(app):
        yield

    app = build_server(lifespan)

    @app.get("/api/failure")
    async def failure():
        raise RuntimeError("sensitive-dependency-value")

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client,
    ):
        response = await client.get("/api/failure")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "sensitive-dependency-value" not in response.text
    assert "sensitive-dependency-value" not in caplog.text
    assert response.headers["cache-control"] == "no-store"


async def test_error_bodies_match_the_declared_envelopes(settings_factory):
    app = create_app(settings_factory())
    app.state.routing = app.state.publication = object()  # rejected before any service call
    spec = app.openapi()
    responses = spec["paths"]["/api/v1/route-policies"]["post"]["responses"]
    assert responses["401"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorResponse"
    )
    assert responses["422"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ValidationErrorResponse"
    )
    assert "HTTPValidationError" not in spec["components"]["schemas"]
    bearer = {"Authorization": "Bearer unverified"}
    cluster = "/api/v1/gateway-clusters/00000000-0000-0000-0000-000000000001"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        unauthorized = await client.post("/api/v1/route-policies", json={})
        invalid = await client.post("/api/v1/route-policies", headers=bearer, json={"x": 1})
        missing_version = await client.patch(cluster, headers=bearer, json={})
        bad_version = await client.patch(cluster, headers=bearer | {"If-Match": "3"}, json={})
    assert unauthorized.status_code == 401
    ErrorResponse.model_validate(unauthorized.json(), strict=True)
    assert set(unauthorized.json()["error"]) == {"code", "message", "trace_id"}
    assert invalid.status_code == 422
    body = ValidationErrorResponse.model_validate(invalid.json(), strict=True)
    assert body.error.code == "validation_error" and body.error.fields
    assert set(invalid.json()["error"]) == {"code", "message", "trace_id", "fields"}
    assert all(set(field) == {"location", "code"} for field in invalid.json()["error"]["fields"])
    assert missing_version.status_code == 428
    ErrorResponse.model_validate(missing_version.json(), strict=True)
    assert bad_version.status_code == 422
    assert "fields" not in bad_version.json()["error"]
    ValidationErrorResponse.model_validate(bad_version.json(), strict=True)
