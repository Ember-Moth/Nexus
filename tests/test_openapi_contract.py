"""Guards for the published management API contract; no services are started."""

import pytest

from nexus.api import create_app

METHODS = {"get", "put", "post", "patch", "delete"}
GENERIC_PATH_PARAMETERS = {"cid", "org", "resource_id"}


@pytest.fixture
def spec(settings_factory):
    # create_app does not run the lifespan, so no database or Redis is contacted.
    return create_app(settings_factory()).openapi()


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
