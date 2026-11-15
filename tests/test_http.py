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


async def test_route_errors_and_request_events_are_correlated_without_raw_inputs(caplog):
    import json
    from uuid import UUID

    from nexus.shared.telemetry import JsonFormatter, request_id

    @asynccontextmanager
    async def lifespan(app):
        yield

    app = build_server(lifespan)

    @app.get("/api/v1/probe/{name}")
    async def probe(name: str):
        return str(request_id.get())

    caplog.set_level("INFO", logger="nexus.api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        ok = await client.get(
            "/api/v1/probe/private-name?secret=private-query",
            headers={"X-Request-ID": "spoofed", "Authorization": "private-token"},
        )
        miss = await client.get("/api/v1/private-missing-name")
        wrong = await client.post("/api/v1/probe/private-name")
    assert str(UUID(ok.headers["x-request-id"])) == ok.json()
    assert wrong.headers["allow"] == "GET"
    for r, code in [(miss, "route_not_found"), (wrong, "method_not_allowed")]:
        assert r.json()["error"]["code"] == code
        assert r.json()["error"]["trace_id"] == r.headers["x-request-id"]
    assert request_id.get() is None
    logs = [JsonFormatter().format(r) for r in caplog.records if r.name == "nexus.api"]
    assert len(logs) == 3
    assert all(
        secret not in "".join(logs)
        for secret in [
            "private-name",
            "private-query",
            "private-token",
            "private-missing-name",
            "spoofed",
        ]
    )
    for line, r in zip(logs, [ok, miss, wrong], strict=True):
        row = json.loads(line)
        assert row["request_id"] == r.headers["x-request-id"] and row["status"] == r.status_code
        assert row["elapsed_ms"] >= 0
    assert json.loads(logs[0])["route"] == "/api/v1/probe/{name}"


def fake_request(peer, headers=()):
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
            "client": (peer, 1234),
        }
    )


def test_forwarded_client_addresses_need_the_bff_signature():
    from ipaddress import ip_address

    from pydantic import SecretStr

    from nexus.http import auth_limit_peer, client_ip_signature

    secret = SecretStr("b" * 32)
    trusted = frozenset({ip_address("127.0.0.1")})

    def peer(source, *headers):
        return auth_limit_peer(fake_request(source, headers), trusted, secret)

    signed = ("X-Nexus-Client-IP-Signature", client_ip_signature("b" * 32, "203.0.113.7"))
    # A same-host proxy relaying a client-chosen header no longer creates a fresh bucket.
    assert peer("127.0.0.1", ("X-Nexus-Client-IP", "203.0.113.7")) == "127.0.0.1"
    assert peer("127.0.0.1", ("X-Nexus-Client-IP", "203.0.113.7"), signed) == "203.0.113.7"
    assert peer("127.0.0.1", ("X-Nexus-Client-IP", "203.0.113.8"), signed) == "127.0.0.1"
    forged = ("X-Nexus-Client-IP-Signature", client_ip_signature("c" * 32, "203.0.113.7"))
    assert peer("127.0.0.1", ("X-Nexus-Client-IP", "203.0.113.7"), forged) == "127.0.0.1"
    assert peer("127.0.0.1", ("X-Nexus-Client-IP", "203.0.113.7"), signed, signed) == "127.0.0.1"
    assert peer("10.0.0.9", ("X-Nexus-Client-IP", "203.0.113.7"), signed) == "10.0.0.9"
    assert (
        auth_limit_peer(
            fake_request("127.0.0.1", [("X-Nexus-Client-IP", "203.0.113.7"), signed]), trusted
        )
        == "127.0.0.1"
    )


def test_ipv6_clients_are_limited_per_64():
    from nexus.http import auth_limit_peer, limit_bucket

    assert limit_bucket("2001:db8:1:2:aaaa::1") == limit_bucket("2001:db8:1:2:ffff:1:2:3")
    assert limit_bucket("2001:db8:1:2::9") == "2001:db8:1:2::/64"
    assert limit_bucket("2001:db8:1:3::9") != limit_bucket("2001:db8:1:2::9")
    assert limit_bucket("::ffff:192.0.2.1") == "192.0.2.1"
    assert limit_bucket("192.0.2.1") == "192.0.2.1"
    assert auth_limit_peer(fake_request("2001:db8::1"), frozenset()) == "2001:db8::/64"


def test_client_ip_signature_matches_the_bff():
    from nexus.http import client_ip_signature

    # Same vector as clientIpSignature() in src/web/src/lib/server/auth-headers.ts.
    assert (
        client_ip_signature("b" * 32, "203.0.113.7")
        == "Sb_Xt0ss0yLjAefla7Bec6e6uqvKbhdHjeLA4a1nQY0"
    )
