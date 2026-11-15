"""Normalize provider usage counters into independent billable dimensions."""

from .money import GUARDRAIL_DIMENSIONS, valid_quantity

RULE = "fixture.tokens.v1/1"
DECLINED = "relay.declined/1"


def failed_over(attempt, attempts):
    """Whether the node started a later attempt of the same request after ``attempt``.

    A node fails over only after the upstream provably did not execute the attempt: the vendor
    documents the error as an admission rejection, or the request was never sent
    (docs/plane-protocol.md). The later attempt is the node's report of that proof.
    """
    return attempt.sequence is not None and any(
        other.sequence is not None and other.sequence > attempt.sequence for other, _ in attempts
    )


def declined(dimensions):
    """Zero usage for an attempt that was declined before execution, so it costs nothing."""
    return DECLINED, dict.fromkeys(dimensions, 0)


def normalize(
    payload,
    dimensions,
    expected_format=None,
    *,
    search_limit=0,
    file_limit=0,
    guardrail_limit=0,
    execution_mode="none",
    verified_seconds=None,
    container_unit=None,
):
    usage_format = payload["usage_format"]
    if expected_format is not None and usage_format != expected_format:
        raise ValueError("usage_format_mismatch")
    raw = payload["raw_usage"]
    if raw is None:
        raise ValueError("missing_usage")
    if not isinstance(raw, dict) or any(not valid_quantity(k, v) for k, v in raw.items()):
        raise ValueError("invalid_token_count")
    if expected_format == "openai.container.v1":
        if not container_unit or set(raw) != {container_unit} or container_unit not in dimensions:
            raise ValueError("invalid_container_usage")
        return expected_format + "/1", {k: raw.get(k, 0) for k in dimensions}
    if expected_format == "openai.vector_storage.v1":
        if (
            set(raw) != {"vector_storage_byte_seconds"}
            or "vector_storage_byte_seconds" not in dimensions
        ):
            raise ValueError("invalid_storage_usage")
        return expected_format + "/1", {k: raw.get(k, 0) for k in dimensions}
    native_rules = {
        "anthropic.messages.v1": _messages,
        "openai.responses.v1": _responses,
        "openai.chat.v1": _responses,
        "aws.bedrock.converse.v1": _bedrock,
    }
    if expected_format in native_rules:
        token_counts = raw.copy()
        invocations = token_counts.pop("code_execution_requests", None)
        if invocations and (expected_format != "anthropic.messages.v1" or execution_mode == "none"):
            raise ValueError("unpriced_code_execution")
        seconds = 0
        if execution_mode in {"metered", "preloaded"} and (
            execution_mode == "preloaded" or invocations != 0
        ):
            if verified_seconds is None:
                raise ValueError("execution_time_requires_evidence")
            if not valid_quantity("code_execution_seconds", verified_seconds):
                raise ValueError("invalid_execution_time")
            seconds = verified_seconds
        elif verified_seconds:
            raise ValueError("execution_time_conflicts_with_receipt")
        files = token_counts.pop("file_search_calls", None)
        if files is None and file_limit:
            raise ValueError("missing_file_search_count")
        if files and (
            expected_format != "openai.responses.v1" or "file_search_calls" not in dimensions
        ):
            raise ValueError("unpriced_file_search")
        searches = token_counts.pop("web_search_requests", None)
        if searches is None and search_limit:
            raise ValueError("missing_web_search_count")
        if searches and "web_search_requests" not in dimensions:
            raise ValueError("unpriced_web_search")
        guardrail = {}
        if expected_format == "aws.bedrock.converse.v1":
            if token_counts.pop("guardrail_usage_unverified", 0):
                raise ValueError("guardrail_usage_requires_evidence")
            for dim in GUARDRAIL_DIMENSIONS:
                units = token_counts.pop(dim, None)
                if guardrail_limit and units is None:
                    raise ValueError("missing_guardrail_usage")
                if units and dim not in dimensions:
                    raise ValueError("unpriced_guardrail_usage")
                if dim in dimensions:
                    guardrail[dim] = units or 0
            for observed in (
                "guardrail_word_units",
                "guardrail_sensitive_free_units",
                "guardrail_reasoning_text_units",
                "guardrail_reasoning_policies",
            ):
                token_counts.pop(observed, None)
        elif guardrail_limit:
            raise ValueError("guardrail_usage_format_mismatch")
        else:
            guardrail = dict.fromkeys(GUARDRAIL_DIMENSIONS.intersection(dimensions), 0)
        if expected_format == "anthropic.messages.v1" and "thinking_tokens" in token_counts:
            if token_counts.pop("thinking_tokens") > token_counts.get(
                "output_tokens", 0
            ) - token_counts.get("compaction_output_tokens", 0):
                raise ValueError("conflicting_thinking_usage")
        if expected_format == "anthropic.messages.v1" and any(
            k.startswith("compaction_") for k in token_counts
        ):
            compact = {
                k.removeprefix("compaction_"): token_counts.pop(k)
                for k in list(token_counts)
                if k.startswith("compaction_")
            }
            _messages(compact)
            if any(n > token_counts.get(k, -1) for k, n in compact.items()):
                raise ValueError("conflicting_compaction_usage")
        # Basic Chat providers may report only the totals. A split is required
        # when the contracted model has a separate cached-input rate.
        if expected_format == "openai.chat.v1" and "input_cache_read_tokens" not in dimensions:
            token_counts.setdefault("cached_input_tokens", 0)
        counts = native_rules[expected_format](token_counts) | guardrail
        counts.update((dim, 0) for dim in dimensions if dim.startswith("container_"))
        if "code_execution_seconds" in dimensions:
            counts["code_execution_seconds"] = seconds
        if "file_search_calls" in dimensions:
            counts["file_search_calls"] = files or 0
        if "vector_storage_byte_seconds" in dimensions:
            counts["vector_storage_byte_seconds"] = 0
        if "web_search_requests" in dimensions:
            counts["web_search_requests"] = searches or 0
        for dimension in (
            ("input_cache_write_5m_tokens", "input_cache_write_1h_tokens")
            if expected_format in {"openai.responses.v1", "openai.chat.v1"}
            else ("input_cache_write_30m_tokens", "input_cache_write_12h_tokens")
        ):
            counts[dimension] = 0
        if set(dimensions) - counts.keys() or any(
            count for key, count in counts.items() if key not in dimensions
        ):
            raise ValueError("dimension_mismatch")
        revision = (
            2 if ("compaction_input_tokens" in raw or "guardrail_content_units" in raw) else 1
        )
        return f"{expected_format}/{revision}", {key: counts[key] for key in dimensions}
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
    if "web_search_requests" in dimensions and not search_limit:
        result["web_search_requests"] = 0
    if "code_execution_seconds" in dimensions and execution_mode == "none":
        result["code_execution_seconds"] = 0
    if "file_search_calls" in dimensions and not file_limit:
        result["file_search_calls"] = 0
    if "vector_storage_byte_seconds" in dimensions:
        result["vector_storage_byte_seconds"] = 0
    result.update((dim, 0) for dim in dimensions if dim.startswith("container_"))
    if not guardrail_limit:
        result.update(dict.fromkeys(GUARDRAIL_DIMENSIONS.intersection(dimensions), 0))
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
        {
            "total_tokens",
            "reasoning_tokens",
            "cache_write_tokens",
            "cache_write_30m_tokens",
            "cache_write_12h_tokens",
        },
    )
    inputs, cached, outputs = raw["input_tokens"], raw["cached_input_tokens"], raw["output_tokens"]
    if (
        cached > inputs
        or raw.get("reasoning_tokens", 0) > outputs
        or ("total_tokens" in raw and raw["total_tokens"] != inputs + outputs)
    ):
        raise ValueError("overlapping_token_counts")
    created = raw.get("cache_write_tokens", 0)
    if created and not {"cache_write_30m_tokens", "cache_write_12h_tokens"} <= raw.keys():
        raise ValueError("missing_cache_counter")
    short, long = raw.get("cache_write_30m_tokens", 0), raw.get("cache_write_12h_tokens", 0)
    if created != short + long or cached + created > inputs:
        raise ValueError("overlapping_token_counts")
    return {
        "input_uncached_tokens": inputs - cached - created,
        "input_cache_write_30m_tokens": short,
        "input_cache_write_12h_tokens": long,
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
