use super::{FORMAT, Result};
use crate::protocol::{
    ProtocolError,
    convert::{self, id, text},
    usage::UsageEvidence,
};
use serde_json::{Value, json};
use std::collections::BTreeMap;

pub fn evidence(usage: Option<&Value>) -> UsageEvidence {
    UsageEvidence {
        usage_format: FORMAT,
        raw_usage: usage.and_then(|v| counters(v).ok()),
    }
}

fn counters(v: &Value) -> Result<BTreeMap<&'static str, u32>> {
    let fields = convert::fields(
        v,
        &[
            "inputTokens",
            "outputTokens",
            "totalTokens",
            "cacheReadInputTokens",
            "cacheWriteInputTokens",
            "cacheDetails",
        ],
    )?;
    let number = |n: &Value| {
        n.as_u64()
            .filter(|&n| n <= 1_000_000_000)
            .map(|n| n as u32)
            .ok_or(ProtocolError::Invalid("Bedrock token count"))
    };
    let mut counts = BTreeMap::new();
    for (src, dst) in [
        ("inputTokens", "input_tokens"),
        ("outputTokens", "output_tokens"),
        ("totalTokens", "total_tokens"),
        ("cacheReadInputTokens", "cache_read_input_tokens"),
        ("cacheWriteInputTokens", "cache_creation_input_tokens"),
    ] {
        if let Some(v) = fields.get(src) {
            counts.insert(dst, number(v)?);
        }
    }
    if let Some(details) = fields.get("cacheDetails") {
        for detail in convert::array(details, "cacheDetails")? {
            convert::fields(detail, &["ttl", "inputTokens"])?;
            let field = match text(detail, "ttl")? {
                "5m" => "cache_write_5m_tokens",
                "1h" => "cache_write_1h_tokens",
                _ => return Err(ProtocolError::Unsupported("cache TTL")),
            };
            if counts
                .insert(field, number(&detail["inputTokens"])?)
                .is_some()
            {
                return Err(ProtocolError::Invalid("duplicate cache TTL"));
            }
        }
        // AWS returns only the TTLs with cache creation. The aggregate independently checks their sum.
        counts.entry("cache_write_5m_tokens").or_insert(0);
        counts.entry("cache_write_1h_tokens").or_insert(0);
    }
    Ok(counts)
}

pub fn total(e: &UsageEvidence) -> Option<u64> {
    // A published format binds cache reads and writes as disjoint input categories.
    // If a provider's total does not reconcile, preserve the counters for review.
    let r = e.raw_usage.as_ref()?;
    let get = |k| r.get(k).copied().map(u64::from);
    let writes = get("cache_creation_input_tokens").unwrap_or(0);
    if writes
        != get("cache_write_5m_tokens").unwrap_or(0) + get("cache_write_1h_tokens").unwrap_or(0)
    {
        return None;
    }
    let total = get("input_tokens")?
        + get("output_tokens")?
        + get("cache_read_input_tokens").unwrap_or(0)
        + writes;
    (Some(total) == get("total_tokens")).then_some(total)
}

pub(crate) fn client_usage(client: crate::protocol::Endpoint, e: &UsageEvidence) -> Value {
    use crate::protocol::Endpoint;
    if client == Endpoint::AnthropicMessages {
        return visible(e);
    }
    let Some(total) = total(e) else {
        return Value::Null;
    };
    let r = e.raw_usage.as_ref().expect("validated usage");
    let output = r["output_tokens"];
    json!({"input_tokens":total-u64::from(output),"output_tokens":output,"total_tokens":total,
        "input_tokens_details":{"cached_tokens":r.get("cache_read_input_tokens").copied().unwrap_or(0),
        "cache_write_tokens":r.get("cache_creation_input_tokens").copied().unwrap_or(0)}})
}
pub fn visible(e: &UsageEvidence) -> Value {
    let Some(r) = e.raw_usage.as_ref().filter(|_| total(e).is_some()) else {
        return json!({"input_tokens":0,"output_tokens":0});
    };
    let count = |k| r.get(k).copied().unwrap_or(0);
    json!({"input_tokens":r["input_tokens"],"output_tokens":r["output_tokens"],
        "cache_read_input_tokens":count("cache_read_input_tokens"),"cache_creation_input_tokens":count("cache_creation_input_tokens"),
        "cache_creation":{"ephemeral_5m_input_tokens":count("cache_write_5m_tokens"),"ephemeral_1h_input_tokens":count("cache_write_1h_tokens")}})
}
pub fn reason(s: &str) -> Result<&'static str> {
    match s {
        "end_turn" => Ok("end_turn"),
        "tool_use" => Ok("tool_use"),
        "max_tokens" => Ok("max_tokens"),
        "stop_sequence" => Ok("stop_sequence"),
        "model_context_window_exceeded" => Ok("model_context_window_exceeded"),
        "guardrail_intervened" | "content_filtered" => Ok("refusal"),
        _ => Err(ProtocolError::Unsupported("Bedrock stop reason")),
    }
}
pub(super) fn stop_sequence<'a>(reason: &str, body: &'a Value) -> Result<Option<&'a str>> {
    if reason != "stop_sequence" {
        return Ok(None);
    }
    body["additionalModelResponseFields"]
        .get("stop_sequence")
        .filter(|v| !v.is_null())
        .map(|s| {
            s.as_str()
                .filter(|s| !s.is_empty())
                .ok_or(ProtocolError::Invalid("matched stop sequence"))
        })
        .transpose()
}

pub fn message(body: &Value, rid: &str, model: &str, usage: &UsageEvidence) -> Result<Value> {
    let msg = &body["output"]["message"];
    if msg["role"] != "assistant" {
        return Err(ProtocolError::Invalid("Bedrock output"));
    }
    let mut content = Vec::new();
    for block in convert::array(&msg["content"], "Bedrock content")? {
        if block.as_object().is_none_or(|o| o.len() != 1) {
            return Err(ProtocolError::Invalid("Bedrock content union"));
        }
        if let Some(s) = block.get("text").and_then(Value::as_str) {
            content.push(json!({"type":"text","text":s}));
        } else if let Some(cited) = block.get("citationsContent") {
            content.push(super::citations::block(cited)?);
        } else if let Some(reasoning) = block.get("reasoningContent") {
            if reasoning.as_object().is_none_or(|o| o.len() != 1) {
                return Err(ProtocolError::Invalid("reasoning union"));
            }
            if let Some(r) = reasoning.get("reasoningText") {
                convert::fields(r, &["text", "signature"])?;
                let mut thinking = json!({"type":"thinking","thinking":text(r,"text")?});
                if r.get("signature").is_some() {
                    thinking["signature"] = text(r, "signature")?.into();
                }
                content.push(thinking);
            } else {
                content.push(
                    json!({"type":"redacted_thinking","data":text(reasoning,"redactedContent")?}),
                );
            }
        } else if let Some(tool) = block.get("toolUse") {
            convert::fields(tool, &["name", "toolUseId", "input"])?;
            if !tool["input"].is_object() {
                return Err(ProtocolError::Invalid("Bedrock tool input"));
            }
            content.push(json!({"type":"tool_use","id":id(tool,"toolUseId")?,"name":id(tool,"name")?,"input":tool["input"]}));
        } else {
            return Err(ProtocolError::Unsupported("Bedrock output block"));
        }
    }
    let reason = reason(text(body, "stopReason")?)?;
    Ok(
        json!({"id":rid,"type":"message","role":"assistant","model":model,"content":content,"stop_reason":reason,
        "stop_sequence":stop_sequence(reason,body)?,"usage":visible(usage)}),
    )
}
