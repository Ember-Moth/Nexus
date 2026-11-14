import pytest

from nexus.modules.billing.domain.usage import normalize


def test_token_subsets_are_counted_once():
    _, counts = normalize(
        {
            "usage_format": "fixture.tokens.v1",
            "raw_usage": {
                "input_total_tokens": 1000,
                "cached_input_tokens": 200,
                "cache_write_5m_tokens": 100,
                "output_tokens": 100,
                "reasoning_tokens": 20,
            },
        },
        {
            "input_uncached_tokens",
            "input_cache_read_tokens",
            "input_cache_write_5m_tokens",
            "output_tokens",
        },
    )
    assert counts == {
        "input_uncached_tokens": 700,
        "input_cache_read_tokens": 200,
        "input_cache_write_5m_tokens": 100,
        "output_tokens": 100,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"cached_input_tokens": None},
        {"cached_input_tokens": 3},
        {"reasoning_tokens": 2},
        {"output_tokens": False},
    ],
)
def test_ambiguous_usage_requires_review(change):
    with pytest.raises(ValueError):
        normalize(
            {
                "usage_format": "fixture.tokens.v1",
                "raw_usage": {
                    "input_total_tokens": 2,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    **change,
                },
            },
            {"input_uncached_tokens", "output_tokens"},
        )


@pytest.mark.parametrize("name", ["messages", "responses"])
def test_rust_protocol_fixtures_normalize_only_with_a_matching_binding(name):
    import json
    from pathlib import Path

    directory = Path(__file__).resolve().parents[1] / "src" / "relay" / "tests" / "fixtures" / "v1"
    sample = json.loads((directory / f"{name}.json").read_text())
    payload = sample["evidence"]
    fmt = payload["usage_format"]
    quantities = sample["normalized"]
    assert normalize(payload, quantities, fmt) == (f"{fmt}/1", quantities)
    with pytest.raises(ValueError, match="unknown_usage_format"):
        normalize(payload, quantities)
    with pytest.raises(ValueError, match="usage_format_mismatch"):
        normalize({**payload, "usage_format": "fixture.dimensions.v1"}, quantities, fmt)


@pytest.mark.parametrize(
    ("fmt", "raw"),
    [
        ("openai.responses.v1", {"input_tokens": 0, "output_tokens": 1}),
        ("openai.responses.v1", {"input_tokens": 2, "output_tokens": 1, "cached_input_tokens": 3}),
        (
            "openai.responses.v1",
            {"input_tokens": 2, "output_tokens": 1, "cached_input_tokens": 0, "total_tokens": 4},
        ),
        (
            "anthropic.messages.v1",
            {
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 3,
                "cache_write_5m_tokens": 3,
            },
        ),
    ],
)
def test_missing_or_conflicting_native_evidence_is_not_settled(fmt, raw):
    with pytest.raises(ValueError):
        normalize(
            {"usage_format": fmt, "raw_usage": raw}, {"input_uncached_tokens", "output_tokens"}, fmt
        )
