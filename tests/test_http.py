from contextlib import asynccontextmanager

import httpx

from nexus.api import build_server


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
