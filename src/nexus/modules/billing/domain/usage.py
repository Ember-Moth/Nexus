"""Normalize provider usage counters into independent billable dimensions."""

RULE = "fixture.tokens.v1/1"


def normalize(payload, dimensions, expected_format=None):
    usage_format = payload["usage_format"]
    if expected_format is not None and usage_format != expected_format:
        raise ValueError("usage_format_mismatch")
    raw = payload["raw_usage"]
    if raw is None:
        raise ValueError("missing_usage")
    if not isinstance(raw, dict) or any(
        type(v) is not int or not 0 <= v <= 1_000_000_000 for v in raw.values()
    ):
        raise ValueError("invalid_token_count")
    native_rules = {
        "anthropic.messages.v1": _messages,
        "openai.responses.v1": _responses,
        "aws.bedrock.converse.v1": _bedrock,
    }
    if expected_format in native_rules:
        counts = native_rules[expected_format](raw)
        if set(dimensions) - counts.keys() or any(
            count for key, count in counts.items() if key not in dimensions
        ):
            raise ValueError("dimension_mismatch")
        return f"{expected_format}/1", {key: counts[key] for key in dimensions}
    if usage_format == "fixture.dimensions.v1":
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


def _require(raw, required, optional):
    if required - raw.keys() or raw.keys() - required - optional:
        raise ValueError("unsupported_usage_fields")


def _responses(raw):
    _require(
        raw,
        {"input_tokens", "cached_input_tokens", "output_tokens"},
        {"total_tokens", "reasoning_tokens", "cache_write_tokens"},
    )
    inputs, cached, outputs = raw["input_tokens"], raw["cached_input_tokens"], raw["output_tokens"]
    if (
        cached > inputs
        or raw.get("reasoning_tokens", 0) > outputs
        or ("total_tokens" in raw and raw["total_tokens"] != inputs + outputs)
    ):
        raise ValueError("overlapping_token_counts")
    if raw.get("cache_write_tokens", 0):
        raise ValueError("unsupported_usage_fields")
    return {
        "input_uncached_tokens": inputs - cached,
        "input_cache_read_tokens": cached,
        "output_tokens": outputs,
    }


def _messages(raw):
    _require(
        raw,
        {"input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"},
        {"cache_write_5m_tokens", "cache_write_1h_tokens"},
    )
    created = raw["cache_creation_input_tokens"]
    if created and not {"cache_write_5m_tokens", "cache_write_1h_tokens"} <= raw.keys():
        raise ValueError("missing_cache_counter")
    short, long = raw.get("cache_write_5m_tokens", 0), raw.get("cache_write_1h_tokens", 0)
    if created != short + long:
        raise ValueError("overlapping_token_counts")
    return {
        "input_uncached_tokens": raw["input_tokens"],
        "input_cache_read_tokens": raw["cache_read_input_tokens"],
        "input_cache_write_5m_tokens": short,
        "input_cache_write_1h_tokens": long,
        "output_tokens": raw["output_tokens"],
    }


def _bedrock(raw):
    _require(
        raw,
        {"input_tokens", "output_tokens", "total_tokens"},
        {
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "cache_write_5m_tokens",
            "cache_write_1h_tokens",
        },
    )
    read = raw.get("cache_read_input_tokens", 0)
    written = raw.get("cache_creation_input_tokens", 0)
    if raw["total_tokens"] != raw["input_tokens"] + raw["output_tokens"] + read + written:
        raise ValueError("conflicting_token_total")
    # Converse omits cache counters on deployments without caching. The
    # provider total above must account for every token before zeros can be inferred.
    return _messages(
        {k: v for k, v in raw.items() if k != "total_tokens"}
        | {
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": written,
        }
    )
