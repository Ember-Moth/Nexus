//! Extract only observed counters from the unconverted upstream payload.
//! The Worker binds the format to the attempt and normalizes for the frozen price.
pub(crate) mod iterations;
use std::collections::{BTreeMap, HashMap};

use serde::Serialize;
use serde_json::{Map, Value};

use super::{Endpoint, ProtocolError};

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct UsageEvidence {
    pub usage_format: &'static str,
    pub raw_usage: Option<BTreeMap<&'static str, u32>>,
}

impl UsageEvidence {
    pub fn extract(endpoint: Endpoint, usage: Option<&Value>) -> Result<Self, ProtocolError> {
        Self::for_model(endpoint, usage, None)
    }
    pub(crate) fn for_model(
        endpoint: Endpoint,
        usage: Option<&Value>,
        model: Option<&str>,
    ) -> Result<Self, ProtocolError> {
        let mut counts = BTreeMap::new();
        let Some(value) = usage.filter(|v| !v.is_null()) else {
            return Ok(Self {
                usage_format: endpoint.usage_format(),
                raw_usage: None,
            });
        };
        let raw = object(value)?;
        let normalized;
        let obj = if raw.contains_key("billing_usage") {
            normalized = gateway_usage(endpoint, raw)?;
            &normalized
        } else {
            raw
        };
        match endpoint {
            Endpoint::AnthropicMessages => {
                fields(
                    obj,
                    &[
                        "input_tokens",
                        "output_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                        "cache_creation",
                        "service_tier",
                        "inference_geo",
                        "iterations",
                        "output_tokens_details",
                        "speed",
                        "fallback_credit",
                        "server_tool_use",
                        "claude_cache_creation_5_m_tokens",
                        "claude_cache_creation_1_h_tokens",
                    ],
                )?;
                if let Some(value) = obj.get("server_tool_use").filter(|v| !v.is_null()) {
                    let tools = object(value)?;
                    fields(
                        tools,
                        &[
                            "web_fetch_requests",
                            "web_search_requests",
                            "code_execution_requests",
                        ],
                    )?;
                    counter(
                        tools,
                        "code_execution_requests",
                        "code_execution_requests",
                        &mut counts,
                    )?;
                    if tools
                        .get("web_fetch_requests")
                        .is_some_and(|v| v.as_u64().is_none())
                    {
                        return Err(ProtocolError::Invalid("web fetch counter"));
                    }
                    counter(
                        tools,
                        "web_search_requests",
                        "web_search_requests",
                        &mut counts,
                    )?;
                }
                for key in [
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                ] {
                    counter(obj, key, key, &mut counts)?;
                }
                if let Some(details) = obj.get("cache_creation").filter(|v| !v.is_null()) {
                    let details = object(details)?;
                    fields(
                        details,
                        &["ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"],
                    )?;
                    counter(
                        details,
                        "ephemeral_5m_input_tokens",
                        "cache_write_5m_tokens",
                        &mut counts,
                    )?;
                    counter(
                        details,
                        "ephemeral_1h_input_tokens",
                        "cache_write_1h_tokens",
                        &mut counts,
                    )?;
                }
                for (wire, field) in [
                    ("claude_cache_creation_5_m_tokens", "cache_write_5m_tokens"),
                    ("claude_cache_creation_1_h_tokens", "cache_write_1h_tokens"),
                ] {
                    let before = counts.get(field).copied();
                    counter(obj, wire, field, &mut counts)?;
                    if before.is_some() && counts.get(field).copied() != before {
                        return Err(ProtocolError::Invalid("conflicting cache lifetime"));
                    }
                }
                if obj
                    .get("speed")
                    .is_some_and(|v| !v.is_null() && v != "standard")
                    || obj.get("fallback_credit").is_some_and(|v| !v.is_null())
                {
                    return Err(ProtocolError::Unsupported("different Messages tariff"));
                }
                if let Some(details) = obj.get("output_tokens_details").filter(|v| !v.is_null()) {
                    let details = object(details)?;
                    fields(details, &["thinking_tokens"])?;
                    counter(details, "thinking_tokens", "thinking_tokens", &mut counts)?;
                    if counts
                        .get("thinking_tokens")
                        .zip(counts.get("output_tokens"))
                        .is_some_and(|(n, total)| n > total)
                    {
                        return Err(ProtocolError::Invalid("thinking token total"));
                    }
                }
                if let Some(rows) = obj.get("iterations").filter(|v| !v.is_null()) {
                    iterations::apply(rows, model, &mut counts)?;
                }
            }
            Endpoint::OpenaiResponses => {
                fields(
                    obj,
                    &[
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                        "attribution",
                        "input_tokens_details",
                        "output_tokens_details",
                    ],
                )?;
                for key in ["input_tokens", "output_tokens", "total_tokens"] {
                    counter(obj, key, key, &mut counts)?;
                }
                if let Some(v) = obj.get("input_tokens_details").filter(|v| !v.is_null()) {
                    let details = object(v)?;
                    fields(
                        details,
                        &[
                            "cached_tokens",
                            "cache_write_tokens",
                            "cache_write_tokens_details",
                        ],
                    )?;
                    counter(details, "cached_tokens", "cached_input_tokens", &mut counts)?;
                    counter(
                        details,
                        "cache_write_tokens",
                        "cache_write_tokens",
                        &mut counts,
                    )?;
                }
                if let Some(lifetimes) = obj
                    .get("input_tokens_details")
                    .and_then(|d| d.get("cache_write_tokens_details"))
                    .filter(|v| !v.is_null())
                {
                    let detail = object(lifetimes)?;
                    fields(detail, &["ttl_30m_tokens", "ttl_12h_tokens"])?;
                    let read = |key: &str| {
                        detail
                            .get(key)
                            .and_then(Value::as_u64)
                            .filter(|&n| n <= 1_000_000_000)
                            .ok_or(ProtocolError::Invalid("cache write breakdown"))
                    };
                    let short = read("ttl_30m_tokens")?;
                    let long = read("ttl_12h_tokens")?;
                    counts.insert("cache_write_30m_tokens", short as u32);
                    counts.insert("cache_write_12h_tokens", long as u32);
                    let total = short + long;
                    if counts.get("cache_write_tokens").map(|n| u64::from(*n)) != Some(total) {
                        return Err(ProtocolError::Invalid("cache write breakdown"));
                    }
                }
                if let Some(v) = obj.get("attribution").filter(|v| !v.is_null()) {
                    validate_attribution(v, &counts)?;
                }
                if let Some(v) = obj.get("output_tokens_details").filter(|v| !v.is_null()) {
                    let details = object(v)?;
                    fields(details, &["reasoning_tokens"])?;
                    counter(details, "reasoning_tokens", "reasoning_tokens", &mut counts)?;
                }
            }
        }
        Ok(Self {
            usage_format: endpoint.usage_format(),
            raw_usage: Some(counts),
        })
    }

    /// OpenAI identifies billable searches by output item. A query list belongs
    /// to a single search action. Opening a page and finding text are distinct actions.
    pub(crate) fn responses_output(
        &mut self,
        body: &Value,
        streamed: &HashMap<String, Option<String>>,
        metered: crate::protocol::hosted::SearchMeters,
    ) -> Result<(), ProtocolError> {
        let Some(raw) = self.raw_usage.as_mut() else {
            return Ok(());
        };
        let Some(output) = body["output"].as_array() else {
            if !streamed.is_empty() {
                return Err(ProtocolError::Invalid("missing web search output"));
            }
            return Ok(());
        };
        let mut ids = HashMap::new();
        let mut searches = 0u32;
        let mut files = 0u32;
        let mut web_observed = false;
        for item in output.iter().filter(|item| {
            matches!(
                item["type"].as_str(),
                Some("web_search_call" | "file_search_call")
            )
        }) {
            let id = item["id"]
                .as_str()
                .filter(|id| !id.is_empty() && id.len() <= 255)
                .ok_or(ProtocolError::Invalid("web search ID"))?;
            let action = if item["type"] == "file_search_call" {
                "file_search"
            } else {
                web_observed = true;
                let a = item["action"]["type"]
                    .as_str()
                    .ok_or(ProtocolError::Invalid("web search action"))?;
                if !matches!(a, "search" | "open_page" | "find_in_page") {
                    return Err(ProtocolError::Unsupported("web search action"));
                }
                a
            };
            if item["status"] != "completed" || ids.insert(id, action).is_some() {
                return Err(ProtocolError::Invalid("ambiguous web search outcome"));
            }
            match action {
                "search" => searches += 1,
                "file_search" => files += 1,
                "open_page" | "find_in_page" => {}
                _ => return Err(ProtocolError::Unsupported("web search action")),
            }
        }
        if streamed.iter().any(|(id, action)| {
            ids.get(id.as_str())
                .is_none_or(|seen| match action.as_deref() {
                    Some(expected) => expected != *seen,
                    None => *seen == "file_search",
                })
        }) {
            return Err(ProtocolError::Invalid("conflicting web search output"));
        }
        if metered.files || files > 0 {
            raw.insert("file_search_calls", files);
        }
        if metered.web || web_observed {
            raw.insert("web_search_requests", searches);
        }
        Ok(())
    }
}

// Some compatible gateways return both wire formats and a redundant billing
// breakdown. Check the duplicated counters before using the canonical fields.
fn gateway_usage(
    ep: Endpoint,
    raw: &Map<String, Value>,
) -> Result<Map<String, Value>, ProtocolError> {
    let mut obj = raw.clone();
    let extra = obj
        .remove("billing_usage")
        .ok_or(ProtocolError::Invalid("billing breakdown"))?;
    let extra = object(&extra)?;
    fields(extra, &["source", "semantic", "openai_usage"])?;
    let billed = object(
        extra
            .get("openai_usage")
            .ok_or(ProtocolError::Invalid("billing breakdown"))?,
    )?;
    fields(
        billed,
        &[
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "prompt_tokens_details",
            "completion_tokens_details",
            "input_tokens",
            "output_tokens",
            "input_tokens_details",
            "claude_cache_creation_5_m_tokens",
            "claude_cache_creation_1_h_tokens",
            "cost",
        ],
    )?;
    let n = |o: &Map<String, Value>, k: &str| {
        o.get(k)
            .and_then(Value::as_u64)
            .filter(|v| *v <= 1_000_000_000)
            .ok_or(ProtocolError::Invalid("gateway token count"))
    };
    let prompt = n(billed, "prompt_tokens")?;
    let completion = n(billed, "completion_tokens")?;
    if prompt + completion != n(billed, "total_tokens")?
        || obj
            .get("output_tokens")
            .is_some_and(|v| v.as_u64() != Some(completion))
    {
        return Err(ProtocolError::Invalid("conflicting gateway usage"));
    }
    let details = object(&billed["prompt_tokens_details"])?;
    fields(
        details,
        &[
            "cached_tokens",
            "text_tokens",
            "audio_tokens",
            "image_tokens",
        ],
    )?;
    let cached = n(details, "cached_tokens")?;
    let out = object(&billed["completion_tokens_details"])?;
    fields(
        out,
        &[
            "reasoning_tokens",
            "text_tokens",
            "audio_tokens",
            "image_tokens",
        ],
    )?;
    for sub in [details, out] {
        if ["audio_tokens", "image_tokens"]
            .iter()
            .any(|k| sub.get(*k).is_some_and(|v| v.as_u64() != Some(0)))
        {
            return Err(ProtocolError::Unsupported("gateway media usage"));
        }
    }
    for k in [
        "claude_cache_creation_5_m_tokens",
        "claude_cache_creation_1_h_tokens",
    ] {
        if let Some(v) = obj.remove(k)
            && Some(&v) != billed.get(k)
        {
            return Err(ProtocolError::Invalid("conflicting cache write"));
        }
    }
    for (alias, expected) in [("input_tokens", prompt), ("output_tokens", completion)] {
        if billed
            .get(alias)
            .is_some_and(|v| v.as_u64() != Some(0) && v.as_u64() != Some(expected))
        {
            return Err(ProtocolError::Invalid("conflicting gateway alias"));
        }
    }
    if let Some(input) = billed.get("input_tokens_details").filter(|v| !v.is_null()) {
        let d = object(input)?;
        fields(d, &["cached_tokens"])?;
        if n(d, "cached_tokens")? != cached {
            return Err(ProtocolError::Invalid("conflicting cached tokens"));
        }
    }
    if ep == Endpoint::OpenaiResponses
        && [
            "claude_cache_creation_5_m_tokens",
            "claude_cache_creation_1_h_tokens",
        ]
        .iter()
        .any(|k| billed.get(*k).is_some_and(|v| v.as_u64() != Some(0)))
    {
        return Err(ProtocolError::Unsupported("gateway writes in Responses"));
    }
    match ep {
        Endpoint::OpenaiResponses => {
            if obj
                .get("input_tokens")
                .is_some_and(|v| v.as_u64() != Some(prompt))
                || obj
                    .get("total_tokens")
                    .is_some_and(|v| v.as_u64() != Some(prompt + completion))
            {
                return Err(ProtocolError::Invalid("conflicting input count"));
            }
            for k in [
                "prompt_tokens",
                "completion_tokens",
                "prompt_tokens_details",
                "completion_tokens_details",
                "cost",
            ] {
                if let Some(v) = obj.remove(k)
                    && Some(&v) != billed.get(k)
                {
                    return Err(ProtocolError::Invalid("conflicting gateway duplicate"));
                }
            }
            if let Some(d) = obj.get("input_tokens_details").filter(|v| !v.is_null()) {
                if d["cached_tokens"].as_u64() != Some(cached) {
                    return Err(ProtocolError::Invalid("conflicting cache reads"));
                }
            } else {
                obj.insert(
                    "input_tokens_details".into(),
                    serde_json::json!({"cached_tokens":cached}),
                );
            }
            if let Some(reason) = out.get("reasoning_tokens") {
                if let Some(d) = obj.get("output_tokens_details").filter(|v| !v.is_null()) {
                    if d["reasoning_tokens"] != *reason {
                        return Err(ProtocolError::Invalid("conflicting reasoning count"));
                    }
                } else {
                    obj.insert(
                        "output_tokens_details".into(),
                        serde_json::json!({"reasoning_tokens":reason}),
                    );
                }
            }
        }
        Endpoint::AnthropicMessages => {
            let writes = n(&obj, "cache_creation_input_tokens")?;
            if n(&obj, "cache_read_input_tokens")? != cached
                || n(&obj, "input_tokens")? + cached + writes != prompt
            {
                return Err(ProtocolError::Invalid("conflicting Messages cache counts"));
            }
            if writes > 0 {
                let short = n(billed, "claude_cache_creation_5_m_tokens")?;
                let long = n(billed, "claude_cache_creation_1_h_tokens")?;
                if short + long != writes {
                    return Err(ProtocolError::Invalid("cache write total"));
                }
                let d = serde_json::json!({"ephemeral_5m_input_tokens":short,"ephemeral_1h_input_tokens":long});
                if obj
                    .get("cache_creation")
                    .is_some_and(|v| !v.is_null() && v != &d)
                {
                    return Err(ProtocolError::Invalid("conflicting cache write detail"));
                }
                obj.insert("cache_creation".into(), d);
            }
        }
    }
    Ok(obj)
}

fn object(value: &Value) -> Result<&Map<String, Value>, ProtocolError> {
    value
        .as_object()
        .ok_or(ProtocolError::Invalid("usage object"))
}

fn fields(obj: &Map<String, Value>, allowed: &[&str]) -> Result<(), ProtocolError> {
    if obj.keys().any(|key| !allowed.contains(&key.as_str())) {
        return Err(ProtocolError::Unsupported("usage dimension"));
    }
    Ok(())
}

fn counter(
    obj: &Map<String, Value>,
    source: &str,
    field: &'static str,
    counts: &mut BTreeMap<&'static str, u32>,
) -> Result<(), ProtocolError> {
    if let Some(value) = obj.get(source).filter(|v| !v.is_null()) {
        let n = value
            .as_u64()
            .filter(|&n| n <= 1_000_000_000)
            .ok_or(ProtocolError::Invalid("token count"))?;
        counts.insert(field, n as u32);
    }
    Ok(())
}

// Some native Responses deployments also attribute aggregate counters to input/output items.
// Accept a breakdown of known counters, without adding its item or content rows to billing.
fn validate_attribution(value: &Value, counts: &BTreeMap<&str, u32>) -> Result<(), ProtocolError> {
    let attribution = object(value)?;
    fields(attribution, &["items"])?;
    let items = object(
        attribution
            .get("items")
            .ok_or(ProtocolError::Invalid("attribution items"))?,
    )?;
    let mut sum = [0u64; 4];
    for row in items.values() {
        let row = object(row)?;
        fields(
            row,
            &[
                "input_tokens",
                "cached_tokens",
                "cache_write_tokens",
                "output_tokens",
                "content",
            ],
        )?;
        let keys = [
            "input_tokens",
            "cached_tokens",
            "cache_write_tokens",
            "output_tokens",
        ];
        for (i, key) in keys.iter().enumerate() {
            if let Some(v) = row.get(*key) {
                sum[i] += v
                    .as_u64()
                    .filter(|&n| n <= 1_000_000_000)
                    .ok_or(ProtocolError::Invalid("attribution counter"))?;
            }
        }
        if let Some(parts) = row.get("content") {
            for part in parts
                .as_array()
                .ok_or(ProtocolError::Invalid("attribution content"))?
            {
                let sub = object(part)?;
                fields(sub, &keys)?;
                if sub
                    .values()
                    .any(|v| v.as_u64().is_none_or(|n| n > 1_000_000_000))
                {
                    return Err(ProtocolError::Invalid("attribution counter"));
                }
            }
        }
    }
    for (n, key) in sum.into_iter().zip([
        "input_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
        "output_tokens",
    ]) {
        if n > u64::from(*counts.get(key).unwrap_or(&0)) {
            return Err(ProtocolError::Invalid("attribution exceeds aggregate"));
        }
    }
    Ok(())
}
