//! Extract only observed counters from the unconverted upstream payload.
//! The Worker binds the format to the attempt and normalizes for the frozen price.
use std::collections::BTreeMap;

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
        let mut counts = BTreeMap::new();
        let Some(value) = usage.filter(|v| !v.is_null()) else {
            return Ok(Self {
                usage_format: endpoint.usage_format(),
                raw_usage: None,
            });
        };
        let obj = object(value)?;
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
                    ],
                )?;
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
                    let total = read("ttl_30m_tokens")? + read("ttl_12h_tokens")?;
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
