//! Responses clients backed by an HTTP Chat Completions deployment.
mod request;
mod stream;
pub(crate) use request::request;
pub(crate) use stream::Stream;

use super::{
    Endpoint, ProtocolError,
    convert::{Result, array, fields, id, text},
    stream::Completion,
    usage::UsageEvidence,
};
use crate::upstream::{Reply, UpstreamError, read_body};
use serde_json::{Value, json};
use std::collections::HashSet;

pub(crate) const FORMAT: &str = "openai.chat.v1";

fn usage(raw: &Value) -> Result<Value> {
    if raw.is_null() {
        return Ok(Value::Null);
    }
    fields(
        raw,
        &[
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "prompt_tokens_details",
            "completion_tokens_details",
        ],
    )?;
    let mut wire = json!({"input_tokens":raw["prompt_tokens"],"output_tokens":raw["completion_tokens"],"total_tokens":raw["total_tokens"]});
    for (from, to, counter, other) in [
        (
            "prompt_tokens_details",
            "input_tokens_details",
            "cached_tokens",
            &["audio_tokens"][..],
        ),
        (
            "completion_tokens_details",
            "output_tokens_details",
            "reasoning_tokens",
            &[
                "audio_tokens",
                "accepted_prediction_tokens",
                "rejected_prediction_tokens",
            ][..],
        ),
    ] {
        if let Some(d) = raw.get(from).filter(|v| !v.is_null()) {
            let obj = d
                .as_object()
                .ok_or(ProtocolError::Invalid("Chat token details"))?;
            for (k, v) in obj {
                if k == counter {
                    wire[to][counter] = v.clone();
                } else if !other.contains(&k.as_str()) || !(v.is_null() || v.as_u64() == Some(0)) {
                    return Err(ProtocolError::Unsupported("Chat token meter"));
                }
            }
        }
    }
    Ok(wire)
}

fn receipt(raw: &Value) -> (Value, UsageEvidence) {
    let wire = usage(raw).unwrap_or(Value::Null);
    let counts = UsageEvidence::extract(Endpoint::OpenaiResponses, Some(&wire))
        .ok()
        .and_then(|v| v.raw_usage);
    let evidence = UsageEvidence {
        usage_format: FORMAT,
        raw_usage: counts,
    };
    let Some(raw) = &evidence.raw_usage else {
        return (Value::Null, evidence);
    };
    let (Some(&input), Some(&output)) = (raw.get("input_tokens"), raw.get("output_tokens")) else {
        return (Value::Null, evidence);
    };
    let total = input + output;
    let cached = *raw.get("cached_input_tokens").unwrap_or(&0);
    let reasoning = *raw.get("reasoning_tokens").unwrap_or(&0);
    if cached > input || reasoning > output || raw.get("total_tokens").is_some_and(|n| *n != total)
    {
        return (Value::Null, evidence);
    }
    // Responses SDKs require numeric breakdowns on the wire. Keep absent
    // Chat counters absent in the separate, supplier-backed billing evidence.
    (
        json!({
            "input_tokens":input, "output_tokens":output, "total_tokens":total,
            "input_tokens_details":{"cached_tokens":cached,"cache_write_tokens":0},
            "output_tokens_details":{"reasoning_tokens":reasoning}
        }),
        evidence,
    )
}

fn head(chunk: &Value, model: &str, request: &Value) -> Result<Value> {
    let mut r = super::convert::response::response(
        id(chunk, "id")?,
        model,
        chunk["created"]
            .as_i64()
            .ok_or(ProtocolError::Invalid("Chat created"))?,
    );
    r["tools"] = request.get("tools").cloned().unwrap_or(json!([]));
    r["tool_choice"] = request.get("tool_choice").cloned().unwrap_or(json!("auto"));
    r["parallel_tool_calls"] = request
        .get("parallel_tool_calls")
        .cloned()
        .unwrap_or(json!(true));
    for key in [
        "instructions",
        "max_output_tokens",
        "temperature",
        "top_p",
        "reasoning",
        "text",
        "metadata",
        "service_tier",
    ] {
        if let Some(v) = request.get(key) {
            r[key] = v.clone();
        }
    }
    Ok(r)
}

fn finish(response: &mut Value, reason: &str, raw: &Value) -> Result<Completion> {
    let (status, detail) = match reason {
        "tool_calls"
            if !array(&response["output"], "output")?
                .iter()
                .any(|v| v["type"] == "function_call") =>
        {
            return Err(ProtocolError::Invalid("missing Chat tool call"));
        }
        "stop" | "tool_calls" => ("completed", Value::Null),
        "length" => ("incomplete", json!({"reason":"max_output_tokens"})),
        "content_filter" => ("incomplete", json!({"reason":"content_filter"})),
        _ => return Err(ProtocolError::Unsupported("Chat finish reason")),
    };
    response["status"] = status.into();
    response["incomplete_details"] = detail;
    for item in response["output"].as_array_mut().unwrap() {
        item["status"] = status.into();
    }
    let (wire, evidence) = receipt(raw);
    response["usage"] = wire;
    Ok(Completion {
        usage: evidence,
        terminal: if status == "completed" {
            super::stream::Terminal::Completed
        } else {
            super::stream::Terminal::Incomplete
        },
        stop_reason: response["incomplete_details"]["reason"]
            .as_str()
            .map(str::to_owned),
        response_id: Some(text(response, "id")?.into()),
    })
}

fn call_item(call_id: &str, name: &str, args: &str) -> Value {
    json!({"type":"function_call","id":format!("fc_{}",uuid::Uuid::new_v4().simple()),"call_id":call_id,"name":name,"arguments":args,"status":"in_progress"})
}
fn message() -> Value {
    json!({"type":"message","id":format!("msg_{}",uuid::Uuid::new_v4().simple()),"role":"assistant","status":"in_progress","content":[]})
}

fn buffered_value(chat: &Value, model: &str, request: &Value) -> Result<(Value, Completion)> {
    if chat["object"] != "chat.completion" {
        return Err(ProtocolError::Invalid("Chat object"));
    }
    let choices = array(&chat["choices"], "Chat choices")?;
    let [choice] = choices.as_slice() else {
        return Err(ProtocolError::Invalid("Chat choice count"));
    };
    let m = &choice["message"];
    if choice["index"] != 0
        || m["role"] != "assistant"
        || !m["function_call"].is_null()
        || !m["audio"].is_null()
    {
        return Err(ProtocolError::Unsupported("Chat choice"));
    }
    let mut r = head(chat, model, request)?;
    let mut output = Vec::new();
    let parts = super::chat::assistant_parts(m)?;
    if !parts.is_empty() {
        let mut item = message();
        item["content"] = parts.into();
        output.push(item);
    }
    let mut ids = HashSet::new();
    if !m["tool_calls"].is_null() {
        for call in array(&m["tool_calls"], "tool calls")? {
            let call_id = id(call, "id")?;
            if call["type"] != "function" || !ids.insert(call_id) {
                return Err(ProtocolError::Invalid("Chat tool call"));
            }
            let f = &call["function"];
            output.push(call_item(call_id, id(f, "name")?, text(f, "arguments")?));
        }
    }
    r["output"] = output.into();
    let done = finish(&mut r, text(choice, "finish_reason")?, &chat["usage"])?;
    Ok((r, done))
}

pub(crate) async fn buffered(
    res: reqwest::Response,
    model: &str,
    request: &Value,
    limit: usize,
) -> std::result::Result<Reply, UpstreamError> {
    let status = res.status();
    if status.is_redirection() {
        return Err(UpstreamError::Redirect);
    }
    let bytes = read_body(res, limit).await?;
    let chat: Value = serde_json::from_slice(&bytes)?;
    if !status.is_success() || chat["error"].is_object() {
        if !chat["error"].is_object() {
            return Err(ProtocolError::Invalid("Chat error").into());
        }
        let (_, usage) = receipt(&chat["usage"]);
        if !chat["usage"].is_null() && usage.raw_usage.is_none() {
            return Err(ProtocolError::Invalid("Chat error usage").into());
        }
        return Ok(Reply {
            status: if status.is_success() {
                http::StatusCode::BAD_GATEWAY
            } else {
                status
            },
            body: bytes,
            outcome: "failed",
            response_id: None,
            usage,
        });
    }
    let (body, done) = buffered_value(&chat, model, request)?;
    let bytes = serde_json::to_vec(&body)?;
    if bytes.len() > limit {
        return Err(UpstreamError::ResponseTooLarge);
    }
    Ok(Reply {
        status,
        body: bytes,
        outcome: done.outcome(Endpoint::OpenaiResponses),
        response_id: done.response_id,
        usage: done.usage,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn chunk(delta: Value, reason: Value) -> String {
        json!({"object":"chat.completion.chunk","created":10,"id":"c",
          "choices":[{"index":0,"delta":delta,"finish_reason":reason}]})
        .to_string()
    }

    #[test]
    fn incomplete_reasons_and_refusals_have_consistent_json_and_stream_shapes() {
        for (reason, detail) in [
            ("length", "max_output_tokens"),
            ("content_filter", "content_filter"),
        ] {
            let request = json!({"model":"public","input":"hi"});
            let msg = json!({"role":"assistant","refusal":"declined"});
            let normal = json!({"object":"chat.completion","id":"c","created":10,"choices":[{"index":0,"message":msg,"finish_reason":reason}]});
            let (expected, done) = buffered_value(&normal, "public", &request).unwrap();
            assert_eq!(done.stop_reason.as_deref(), Some(detail));
            let mut s = Stream::new("public", &request, 8192);
            let (frames, done) = s.event(&chunk(msg, reason.into())).unwrap();
            assert!(done.is_none());
            assert!(frames.iter().any(|f| f["type"] == "response.refusal.delta"));
            let (frames, done) = s.event("[DONE]").unwrap();
            let done = done.unwrap();
            assert!(done.usage.raw_usage.is_none());
            let terminal = frames.last().unwrap();
            assert_eq!(terminal["type"], "response.incomplete");
            let final_response = &terminal["response"];
            assert_eq!(
                final_response["output"][0]["content"],
                expected["output"][0]["content"]
            );
            assert_eq!(
                final_response["incomplete_details"],
                expected["incomplete_details"]
            );
            assert!(
                frames
                    .iter()
                    .any(|f| f["type"] == "response.refusal.done" && f["refusal"] == "declined")
            );
        }
    }

    #[test]
    fn tool_headers_can_arrive_in_fragments_and_tools_remain_in_index_order() {
        let body = json!({});
        let mut s = Stream::new("m", &body, 8192);
        let tool = |t: Value| chunk(json!({"tool_calls":[t]}), Value::Null);
        s.event(&tool(
            json!({"index":1,"id":"b","function":{"name":"second","arguments":"{}"}}),
        ))
        .unwrap();
        s.event(&tool(json!({"index":0,"id":"a","type":"function"})))
            .unwrap();
        s.event(&tool(json!({"index":0,"function":{"name":"look"}})))
            .unwrap();
        let (added, _) = s
            .event(&tool(
                json!({"index":0,"function":{"name":"up","arguments":"{}"}}),
            ))
            .unwrap();
        let names: Vec<_> = added
            .iter()
            .filter(|e| e["type"] == "response.output_item.added")
            .map(|e| e["item"]["name"].as_str().unwrap())
            .collect();
        assert_eq!(names, ["lookup", "second"]);
        assert!(s.event("[DONE]").is_err()); // No witnessed finish_reason.
    }

    #[test]
    fn invalid_or_unpriced_chat_usage_never_becomes_a_zero_receipt() {
        let raw = json!({"prompt_tokens":2,"completion_tokens":1,"total_tokens":3,"completion_tokens_details":{"audio_tokens":1}});
        assert!(receipt(&raw).1.raw_usage.is_none());
        let raw = json!({"prompt_tokens":2,"completion_tokens":1,"total_tokens":3,"prompt_tokens_details":{"cached_tokens":1},"completion_tokens_details":{"reasoning_tokens":1,"audio_tokens":0}});
        let (_, e) = receipt(&raw);
        let counters = e.raw_usage.unwrap();
        assert_eq!(counters["cached_input_tokens"], 1);
        assert_eq!(counters["reasoning_tokens"], 1);
        assert_eq!(e.usage_format, FORMAT);
    }
}
