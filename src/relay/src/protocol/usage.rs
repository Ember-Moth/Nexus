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
                        "input_tokens_details",
                        "output_tokens_details",
                    ],
                )?;
                for key in ["input_tokens", "output_tokens", "total_tokens"] {
                    counter(obj, key, key, &mut counts)?;
                }
                if let Some(v) = obj.get("input_tokens_details").filter(|v| !v.is_null()) {
                    let details = object(v)?;
                    fields(details, &["cached_tokens", "cache_write_tokens"])?;
                    counter(details, "cached_tokens", "cached_input_tokens", &mut counts)?;
                    counter(
                        details,
                        "cache_write_tokens",
                        "cache_write_tokens",
                        &mut counts,
                    )?;
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
