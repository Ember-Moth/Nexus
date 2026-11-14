"""Normalize provider usage counters into independent billable dimensions."""

RULE = "fixture.tokens.v1/1"


def normalize(payload, dimensions):
    raw = payload["raw_usage"]
    if raw is None:
        raise ValueError("missing_usage")
    if not isinstance(raw, dict) or any(
        type(v) is not int or not 0 <= v <= 1_000_000_000 for v in raw.values()
    ):
        raise ValueError("invalid_token_count")
    if payload["usage_format"] == "fixture.dimensions.v1":
        if set(raw) != set(dimensions):
            raise ValueError("dimension_mismatch")
        return "fixture.dimensions.v1/1", raw
    if payload["usage_format"] != "fixture.tokens.v1":
        raise ValueError("unknown_usage_format")
    inputs = {
        "cached_input_tokens": "input_cache_read_tokens",
        "cache_write_5m_tokens": "input_cache_write_5m_tokens",
        "cache_write_1h_tokens": "input_cache_write_1h_tokens",
    }
    if not {
        "input_total_tokens",
        "cached_input_tokens",
        "output_tokens",
    } <= raw.keys() or raw.keys() - inputs.keys() - {
        "input_total_tokens",
        "output_tokens",
        "reasoning_tokens",
    }:
        raise ValueError("unsupported_usage_fields")
    result = {
        "output_tokens": raw["output_tokens"],
        "input_uncached_tokens": raw["input_total_tokens"],
    }
    for field, dimension in inputs.items():
        value = raw.get(field, 0)
        if dimension in dimensions:
            if field not in raw:
                raise ValueError("missing_cache_counter")
            result[dimension] = value
        elif value:
            raise ValueError("dimension_mismatch")
        result["input_uncached_tokens"] -= value
    if result["input_uncached_tokens"] < 0 or raw.get("reasoning_tokens", 0) > raw["output_tokens"]:
        raise ValueError("overlapping_token_counts")
    if set(result) != set(dimensions):
        raise ValueError("dimension_mismatch")
    return RULE, result
