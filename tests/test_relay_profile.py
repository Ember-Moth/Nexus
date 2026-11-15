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
