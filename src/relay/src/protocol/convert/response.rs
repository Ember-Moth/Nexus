use super::{Exchange, Result, arguments, array, id, text};
use crate::protocol::{Endpoint, ProtocolError, usage::UsageEvidence};
use serde_json::{Value, json};
use std::collections::HashSet;

// IDs of Responses output items and IDs used by tool results are separate namespaces.
pub(crate) fn item_id(response: &str, index: usize, tool: bool) -> String {
    format!("{}_{response}_{index}", if tool { "fc" } else { "msg" })
}

pub(super) fn message(id: &str, model: &str) -> Value {
    json!({"id":id,"type":"message","role":"assistant","model":model,"content":[],"stop_reason":null,"stop_sequence":null,"usage":null})
}
pub(crate) fn response(id: &str, model: &str, time: i64) -> Value {
    json!({"id":id,"object":"response","created_at":time,"model":model,"status":"in_progress","output":[],"error":null,"incomplete_details":null,"usage":null,"store":false})
}
pub(super) fn output_item(block: &Value, rid: &str, index: usize, status: &str) -> Result<Value> {
    validate_message_block(block)?;
    match block["type"].as_str() {
        Some("text") => Ok(
            json!({"id":item_id(rid,index,false),"type":"message","role":"assistant","status":status,"content":[{"type":"output_text","text":text(block,"text")?,"annotations":[]}]}),
        ),
        Some("tool_use") if block["input"].is_object() => Ok(
            json!({"type":"function_call","id":item_id(rid,index,true),"call_id":id(block,"id")?,"name":id(block,"name")?,"arguments":block["input"].to_string(),"status":status}),
        ),
        _ => Err(ProtocolError::Unsupported("Messages output block")),
    }
}
pub(super) fn validate_message_block(block: &Value) -> Result<()> {
    match block["type"].as_str() {
        Some("text") => {
            if block
                .get("citations")
                .is_some_and(|v| !v.is_null() && v.as_array().is_none_or(|a| !a.is_empty()))
            {
                return Err(ProtocolError::Unsupported("text citations"));
            }
        }
        Some("tool_use") => {
            if block
                .get("caller")
                .is_some_and(|v| !v.is_null() && v["type"] != "direct")
            {
                return Err(ProtocolError::Unsupported("tool caller"));
            }
        }
        _ => return Err(ProtocolError::Unsupported("Messages output block")),
    }
    Ok(())
}
pub(super) fn part_text(part: &Value) -> Result<&str> {
    if part["type"] == "refusal" {
        super::fields(part, &["type", "refusal"])?;
        return text(part, "refusal");
    }
    if part["type"] != "output_text"
        || ["annotations", "logprobs"].iter().any(|k| {
            part.get(k)
                .is_some_and(|v| !v.is_null() && v.as_array().is_none_or(|a| !a.is_empty()))
        })
    {
        return Err(ProtocolError::Unsupported("Responses output part"));
    }
    text(part, "text")
}
pub(super) fn blocks(item: &Value) -> Result<Vec<Value>> {
    match item["type"].as_str() {
        Some("message") if item["role"] == "assistant" => {
            array(&item["content"], "output content")?
                .iter()
                .map(|part| Ok(json!({"type":"text","text":part_text(part)?})))
                .collect()
        }
        Some("function_call") => Ok(vec![
            json!({"type":"tool_use","id":id(item,"call_id")?,"name":id(item,"name")?,"input":arguments(item)?}),
        ]),
        _ => Err(ProtocolError::Unsupported("Responses output item")),
    }
}
pub(super) fn message_stop(body: &Value, has_tool: bool) -> Result<&'static str> {
    match body["status"].as_str() {
        Some("completed")
            if body["output"].as_array().into_iter().flatten().any(|v| {
                v["type"] == "message"
                    && v["content"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .any(|p| p["type"] == "refusal")
            }) =>
        {
            Ok("refusal")
        }
        Some("incomplete") if body["incomplete_details"]["reason"] == "content_filter" => {
            Ok("refusal")
        }
        Some("completed") => Ok(if has_tool { "tool_use" } else { "end_turn" }),
        Some("incomplete") if body["incomplete_details"]["reason"] == "max_output_tokens" => {
            Ok("max_tokens")
        }
        _ => Err(ProtocolError::Unsupported("Responses termination")),
    }
}
pub(super) fn response_status(reason: &str, has_tool: bool) -> Result<(&'static str, Value)> {
    match reason {
        "end_turn" | "stop_sequence" | "compaction" if !has_tool => Ok(("completed", Value::Null)),
        "tool_use" if has_tool => Ok(("completed", Value::Null)),
        "refusal" => Ok(("incomplete", json!({"reason":"content_filter"}))),
        "max_tokens" => Ok(("incomplete", json!({"reason":"max_output_tokens"}))),
        _ => Err(ProtocolError::Unsupported("Messages termination")),
    }
}

// Produce client-visible usage only when its dimensions can be expressed from observed facts.
pub(super) fn usage(client: Endpoint, evidence: &UsageEvidence) -> Value {
    if evidence.usage_format == crate::bedrock::FORMAT {
        return crate::bedrock::wire::client_usage(client, evidence);
    }
    let Some(raw) = evidence.raw_usage.as_ref() else {
        return Value::Null;
    };
    let count = |key: &str| raw.get(key).copied().map(u64::from);
    let Some(output) = count("output_tokens") else {
        return Value::Null;
    };
    let Some(input) = count("input_tokens") else {
        return Value::Null;
    };
    match client {
        Endpoint::AnthropicMessages => {
            let Some(cached) = count("cached_input_tokens") else {
                return Value::Null;
            };
            let writes = count("cache_write_tokens").unwrap_or(0);
            let Some(uncached) = input.checked_sub(cached + writes) else {
                return Value::Null;
            };
            json!({"input_tokens":uncached,"cache_read_input_tokens":cached,"cache_creation_input_tokens":writes,"output_tokens":output})
        }
        Endpoint::OpenaiResponses => {
            let (Some(cached), Some(writes)) = (
                count("cache_read_input_tokens"),
                count("cache_creation_input_tokens"),
            ) else {
                return Value::Null;
            };
            let total = input + cached + writes;
            json!({"input_tokens":total,"input_tokens_details":{"cached_tokens":cached,"cache_write_tokens":writes},"output_tokens":output,"total_tokens":total+output})
        }
    }
}
// Messages SDKs mutate message_start.usage when applying message_delta. Both events
// require numeric fields. This projection is exclusively for the client wire format.
pub(super) fn messages_wire_usage(visible: Value) -> Value {
    if visible.is_null() {
        json!({"input_tokens":0,"output_tokens":0})
    } else {
        visible
    }
}

pub fn error(client: Endpoint, body: &Value, status: u16) -> Value {
    let err = &body["error"];
    let msg = err["message"]
        .as_str()
        .unwrap_or("Upstream generation failed");
    let (anthropic, openai) = match status {
        400 | 404 | 422 => ("invalid_request_error", "invalid_request_error"),
        401 => ("authentication_error", "authentication_error"),
        403 => ("permission_error", "permission_error"),
        429 => ("rate_limit_error", "rate_limit_exceeded"),
        503 | 529 => ("overloaded_error", "server_error"),
        _ => ("api_error", "server_error"),
    };
    match client {
        Endpoint::AnthropicMessages => {
            json!({"type":"error","error":{"type":anthropic,"message":msg}})
        }
        Endpoint::OpenaiResponses => {
            json!({"error":{"type":openai,"code":err["type"].as_str().unwrap_or(openai),"message":msg,"param":null}})
        }
    }
}

pub fn convert(
    exchange: Exchange<'_>,
    body: &Value,
    evidence: &UsageEvidence,
    status: u16,
) -> Result<(Value, u16)> {
    with_history(exchange, body, evidence, status, None)
}

pub(crate) fn with_history(
    exchange: Exchange<'_>,
    body: &Value,
    evidence: &UsageEvidence,
    status: u16,
    history: Option<&crate::protocol::history::Writer>,
) -> Result<(Value, u16)> {
    if status >= 400
        || body["type"] == "error"
        || body["status"] == "failed"
        || body["error"].is_object()
    {
        let status = if status >= 400 { status } else { 502 };
        return Ok((error(exchange.client, body, status), status));
    }
    let rid = id(body, "id")?;
    let mut calls = HashSet::new();
    let visible = usage(exchange.client, evidence);
    let out = match exchange.client {
        Endpoint::AnthropicMessages => {
            let mut out = message(rid, exchange.public_model);
            let mut content = Vec::new();
            let mut items = HashSet::new();
            for (index, item) in array(&body["output"], "output")?.iter().enumerate() {
                if !items.insert(id(item, "id")?) {
                    return Err(ProtocolError::Invalid("duplicate output item"));
                }
                if item["type"] == "function_call" && !calls.insert(id(item, "call_id")?) {
                    return Err(ProtocolError::Invalid("duplicate call ID"));
                }
                if (matches!(item["type"].as_str(), Some("reasoning" | "compaction"))
                    || crate::protocol::history::citations::annotated(item))
                    && let Some(h) = history
                {
                    content.extend(h.project_group(item, rid, index)?);
                } else {
                    content.extend(blocks(item)?);
                }
            }
            out["stop_reason"] = message_stop(body, !calls.is_empty())?.into();
            out["content"] = content.into();
            out["usage"] = messages_wire_usage(visible);
            out
        }
        Endpoint::OpenaiResponses => {
            let mut out = response(rid, exchange.public_model, chrono::Utc::now().timestamp());
            let mut items = Vec::new();
            for (index, block) in array(&body["content"], "content")?.iter().enumerate() {
                if block["type"] == "tool_use" && !calls.insert(id(block, "id")?) {
                    return Err(ProtocolError::Invalid("duplicate call ID"));
                }
                let item = if (matches!(
                    block["type"].as_str(),
                    Some("thinking" | "redacted_thinking" | "compaction")
                ) || block["citations"].as_array().is_some_and(|v| !v.is_empty()))
                    && let Some(h) = history
                {
                    h.project(block, rid, index)?
                } else {
                    output_item(block, rid, index, "completed")?
                };
                items.push(item);
            }
            let (status, detail) = response_status(text(body, "stop_reason")?, !calls.is_empty())?;
            out["status"] = status.into();
            out["incomplete_details"] = detail;
            out["output"] = items.into();
            out["usage"] = visible;
            out
        }
    };
    Ok((out, status))
}
