from types import SimpleNamespace

import pytest

from nexus.modules.billing.domain.usage import declined, failed_over, normalize


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


def test_bedrock_counts_uncached_reads_and_writes_once():
    fmt = "aws.bedrock.converse.v1"
    raw = {
        "input_tokens": 11,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 5,
        "cache_write_5m_tokens": 2,
        "cache_write_1h_tokens": 3,
        "output_tokens": 4,
        "total_tokens": 27,
    }
    counts = {
        "input_uncached_tokens": 11,
        "input_cache_read_tokens": 7,
        "input_cache_write_5m_tokens": 2,
        "input_cache_write_1h_tokens": 3,
        "output_tokens": 4,
    }
    fact = {"raw_usage": raw, "usage_format": fmt}
    assert normalize(fact, counts, fmt) == (fmt + "/1", counts)
    for change in ({"total_tokens": 15}, {"cache_write_1h_tokens": 4}, {"extra_tokens": 1}):
        with pytest.raises(ValueError):
            normalize({**fact, "raw_usage": raw | change}, counts, fmt)
    with pytest.raises(ValueError, match="missing_cache_counter"):
        normalize(
            {**fact, "raw_usage": {k: v for k, v in raw.items() if k != "cache_write_5m_tokens"}},
            counts,
            fmt,
        )
    with pytest.raises(ValueError, match="usage_format_mismatch"):
        normalize(fact, counts, "anthropic.messages.v1")


def test_bedrock_no_cache_counters_require_a_matching_total():
    fmt = "aws.bedrock.converse.v1"
    dimensions = {"input_uncached_tokens", "output_tokens"}
    raw = {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16}
    assert normalize({"usage_format": fmt, "raw_usage": raw}, dimensions, fmt)[1] == {
        "input_uncached_tokens": 12,
        "output_tokens": 4,
    }
    with pytest.raises(ValueError, match="conflicting_token_total"):
        normalize({"usage_format": fmt, "raw_usage": raw | {"total_tokens": 17}}, dimensions, fmt)


def test_responses_cache_write_lifetimes_are_disjoint_and_priced():
    fmt = "openai.responses.v1"
    raw = {
        "input_tokens": 12000,
        "cached_input_tokens": 4000,
        "output_tokens": 10,
        "cache_write_tokens": 7000,
        "cache_write_30m_tokens": 3000,
        "cache_write_12h_tokens": 4000,
    }
    expected = {
        "input_uncached_tokens": 1000,
        "input_cache_read_tokens": 4000,
        "output_tokens": 10,
        "input_cache_write_30m_tokens": 3000,
        "input_cache_write_12h_tokens": 4000,
    }

    def check(values, dimensions=expected):
        return normalize({"usage_format": fmt, "raw_usage": values}, dimensions, fmt)[1]

    assert check(raw) == expected
    for key in ("cache_write_30m_tokens", "cache_write_12h_tokens"):
        with pytest.raises(ValueError, match="missing_cache_counter"):
            check({k: v for k, v in raw.items() if k != key})
    with pytest.raises(ValueError, match="overlapping_token_counts"):
        check(raw | {"cache_write_tokens": 3000})
    with pytest.raises(ValueError, match="overlapping_token_counts"):
        check(raw | {"input_tokens": 10000})
    with pytest.raises(ValueError, match="dimension_mismatch"):
        check(raw, set(expected) - {"input_cache_write_12h_tokens"})


def test_guardrail_units_require_frozen_policy_prices_and_complete_evidence():
    base = {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}
    dims = {"input_uncached_tokens", "output_tokens", "guardrail_content_units"}
    fmt = "aws.bedrock.converse.v1"
    for extra, reason in [
        ({"guardrail_usage_unverified": 1}, "guardrail_usage_requires_evidence"),
        ({}, "missing_guardrail_usage"),
        (
            {
                f"guardrail_{k}_units": int(k == "topic")
                for k in ("content", "image", "topic", "sensitive", "grounding", "reasoning")
            },
            "unpriced_guardrail_usage",
        ),
    ]:
        with pytest.raises(ValueError, match=reason):
            normalize(
                {"usage_format": fmt, "raw_usage": base | extra}, dims, fmt, guardrail_limit=4
            )
    rule, counts = normalize(
        {"usage_format": fmt, "raw_usage": base | {"guardrail_word_units": 5}}, dims, fmt
    )
    assert counts == {"input_uncached_tokens": 2, "output_tokens": 3, "guardrail_content_units": 0}


def test_chat_receipt_splits_cached_tokens_only_with_evidence():
    fmt = "openai.chat.v1"
    raw = {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11, "reasoning_tokens": 2}
    receipt = {"raw_usage": raw, "usage_format": fmt}
    base = {"input_uncached_tokens", "output_tokens"}
    _, counts = normalize(receipt, base, fmt)
    assert counts == {"input_uncached_tokens": 8, "output_tokens": 3}
    priced = base | {"input_cache_read_tokens"}
    with pytest.raises(ValueError, match="unsupported_usage_fields"):
        normalize(receipt, priced, fmt)
    _, counts = normalize(receipt | {"raw_usage": raw | {"cached_input_tokens": 5}}, priced, fmt)
    assert counts == {"input_uncached_tokens": 3, "input_cache_read_tokens": 5, "output_tokens": 3}
    with pytest.raises(ValueError, match="usage_format_mismatch"):
        normalize(receipt, priced, "openai.responses.v1")


def test_failed_over_attempt_is_proven_declined():
    first, second, third = (SimpleNamespace(sequence=n) for n in (1, 2, 3))
    attempts = [(first, None), (second, None), (third, None)]
    assert failed_over(first, attempts) and failed_over(second, attempts)
    # The last attempt has no successor: nothing on the wire proves it was not executed.
    assert not failed_over(third, attempts)
    # Attempts registered without a node sequence (the management API) prove nothing.
    unnumbered = SimpleNamespace(sequence=None)
    assert not failed_over(unnumbered, [(unnumbered, None), (second, None)])
    assert not failed_over(first, [(first, None), (unnumbered, None)])


def test_declined_attempt_has_zero_usage_in_every_priced_dimension():
    rule, counts = declined({"input_uncached_tokens": {}, "output_tokens": {}})
    assert rule == "relay.declined/1"
    assert counts == {"input_uncached_tokens": 0, "output_tokens": 0}
