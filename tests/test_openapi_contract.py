"""Guards for the published management API contract; no services are started."""

import json
from pathlib import Path

import pytest

from nexus.api import create_app

METHODS = {"get", "put", "post", "patch", "delete"}
GENERIC_PATH_PARAMETERS = {"cid", "org", "resource_id"}


@pytest.fixture
def spec(settings_factory):
    # create_app does not run the lifespan, so no database or Redis is contacted.
    return create_app(settings_factory()).openapi()


WEB_SNAPSHOT = Path(__file__).parents[1] / "src/web/openapi/openapi.json"


def test_web_snapshot_matches_the_live_contract(spec):
    # The console builds its API types from this committed snapshot, so a schema change that
    # skips the snapshot ships a console that does not know the new values.
    snapshot = json.loads(WEB_SNAPSHOT.read_text())
    assert snapshot == spec, (
        "src/web/openapi/openapi.json is out of date: start the API in development mode and run "
        "`pnpm api:types` in src/web"
    )


def operations(spec):
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method in METHODS:
                yield f"{method.upper()} {path}", operation


def test_every_success_response_declares_a_schema(spec):
    untyped = []
    for name, operation in operations(spec):
        for status, response in operation["responses"].items():
            if not status.startswith("2") or status == "204":
                continue
            schemas = [media.get("schema") for media in response.get("content", {}).values()]
            if not schemas or not all(schemas):
                untyped.append(f"{name} {status}")
    assert not untyped, "\n".join(untyped)


def test_every_secured_operation_declares_401(spec):
    missing = [
        name
        for name, operation in operations(spec)
        if operation.get("security") and "401" not in operation["responses"]
    ]
    assert not missing, "\n".join(missing)


def test_path_parameters_name_their_resource(spec):
    generic = [
        f"{name} {parameter['name']}"
        for name, operation in operations(spec)
        for parameter in operation.get("parameters", [])
        if parameter["in"] == "path" and parameter["name"] in GENERIC_PATH_PARAMETERS
    ]
    assert not generic, "\n".join(generic)


def test_errors_use_the_shared_envelope(spec):
    assert "HTTPValidationError" not in spec["components"]["schemas"]
    for name, operation in operations(spec):
        for status, response in operation["responses"].items():
            if status.startswith(("4", "5")):
                expected = "ValidationErrorResponse" if status == "422" else "ErrorResponse"
                schema = response["content"]["application/json"]["schema"]
                assert schema == {"$ref": f"#/components/schemas/{expected}"}, (name, status)


def test_conditional_headers_use_canonical_names(spec):
    headers = {
        parameter["name"]
        for _, operation in operations(spec)
        for parameter in operation.get("parameters", [])
        if parameter["in"] == "header"
    }
    assert headers == {"If-Match", "Idempotency-Key"}
    for name, operation in operations(spec):
        names = {p["name"] for p in operation.get("parameters", []) if p["in"] == "header"}
        statuses = set(operation["responses"])
        if "If-Match" in names:
            assert {"412", "428"} <= statuses, name
        if "Idempotency-Key" in names:
            assert {"409", "428"} <= statuses, name


def test_catalog_covers_literal_errors_and_sql_admission_errors():
    import ast
    from pathlib import Path

    from nexus.modules.billing.persistence.repository import ADMISSION_ERRORS
    from nexus.shared.error_catalog import CATALOG

    root = Path(__file__).resolve().parents[1] / "src/nexus"
    for path in root.rglob("*.py"):
        for call in ast.walk(ast.parse(path.read_text())):
            if isinstance(call, ast.Call) and getattr(call.func, "id", None) in {
                "ServiceError",
                "IdentityError",
            }:
                if len(call.args) >= 3 and all(
                    isinstance(call.args[i], ast.Constant) for i in (0, 2)
                ):
                    assert (call.args[0].value, call.args[2].value) in CATALOG, (path, call.lineno)
    assert all((code, status) in CATALOG for code, (status, _) in ADMISSION_ERRORS.items())


async def test_published_error_catalog_and_operation_guidance(spec, settings_factory):
    import httpx

    app = create_app(
        settings_factory(
            environment="production",
            database_url="postgresql+psycopg://nexus_api:fixture@localhost/nexus?sslmode=verify-full",
            redis_url="rediss://:fixture@localhost/0",
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        catalog = (await client.get("/api/v1/error-codes")).json()
    assert catalog == spec["x-error-catalog"]
    assert catalog["insufficient_funds"][0]["action"] == "fund_account"
    assert catalog["budget_exceeded"][0]["action"] == "adjust_budget"
    responses = spec["paths"]["/api/v1/auth/sessions"]["post"]["responses"]
    assert "Retry-After" in responses["429"]["headers"]
    assert "turnstile_unavailable" in responses["503"]["x-error-codes"]
    for _, op in operations(spec):
        for status, response in op["responses"].items():
            for code in response.get("x-error-codes", []):
                assert any(row["status"] == int(status) for row in catalog[code])
