import pytest
from pydantic import ValidationError

from nexus.modules.catalog.application.schemas import RelayProfile

BASE = {
    "transports": ["http"],
    "input_ceiling": 4,
    "output_ceiling": 6,
    "count_rule": "deployment_ceiling.v1",
    "bound_reference": "fixture",
}
GUARDRAIL = {"guardrailIdentifier": "abcd", "guardrailVersion": "1"}


def converse(**fields):
    return {**BASE, "endpoint": "bedrock_converse", **fields}


@pytest.mark.parametrize(
    "fields",
    [
        {"bedrock_guardrail": GUARDRAIL},
        {"bedrock_guardrail": GUARDRAIL, "bedrock_guardrail_unit_ceiling": 0},
        {"bedrock_request_options": [{"guardrailConfig": GUARDRAIL}]},
        {"bedrock_guardrail_unit_ceiling": 4},
    ],
)
def test_guardrail_and_unit_ceiling_require_each_other(fields):
    with pytest.raises(ValidationError):
        RelayProfile.model_validate(converse(**fields))


@pytest.mark.parametrize(
    "fields",
    [
        {"bedrock_guardrail": GUARDRAIL, "bedrock_guardrail_unit_ceiling": 4},
        {
            "bedrock_request_options": [{"guardrailConfig": GUARDRAIL}],
            "bedrock_guardrail_unit_ceiling": 1,
        },
        {},
    ],
)
def test_bounded_guardrails_validate(fields):
    RelayProfile.model_validate(converse(**fields))


def native(endpoint, features):
    return {**BASE, "endpoint": endpoint, "provider_features": features}


def test_provider_features_default_to_off():
    assert (
        RelayProfile.model_validate({**BASE, "endpoint": "openai_responses"}).provider_features
        == []
    )


@pytest.mark.parametrize(
    "endpoint, features",
    [
        ("openai_responses", ["stored_responses", "background", "conversations", "files"]),
        ("openai_responses", ["vector_stores", "code_execution", "remote_mcp", "web_search"]),
        ("anthropic_messages", ["files", "batches", "code_execution", "web_fetch"]),
        ("anthropic_messages", ["remote_mcp", "web_search"]),
    ],
)
def test_provider_features_fit_their_endpoint(endpoint, features):
    assert RelayProfile.model_validate(native(endpoint, features)).provider_features == features


@pytest.mark.parametrize(
    "endpoint, features",
    [
        ("openai_responses", ["background"]),
        ("openai_responses", ["batches"]),
        ("openai_responses", ["web_fetch"]),
        ("openai_responses", ["files", "files"]),
        ("anthropic_messages", ["stored_responses"]),
        ("anthropic_messages", ["vector_stores"]),
        ("bedrock_converse", ["files"]),
        ("openai_chat_completions", ["stored_responses"]),
        ("openai_responses", ["unknown"]),
    ],
)
def test_provider_features_reject_unsupported_combinations(endpoint, features):
    with pytest.raises(ValidationError):
        RelayProfile.model_validate(native(endpoint, features))
