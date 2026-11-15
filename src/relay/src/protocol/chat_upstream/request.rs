use super::super::ProtocolError;
use super::super::convert::{Result, array, fields, id, request::Calls, text};
use serde_json::{Value, json};

fn content(value: &Value, role: &str) -> Result<Value> {
    if value.is_string() {
        return Ok(value.clone());
    }
    let mut out = Vec::new();
    for p in array(value, "message content")? {
        let part = match p["type"].as_str() {
            Some("input_text" | "output_text") => {
                fields(p, &["type", "text", "annotations", "logprobs"])?;
                for k in ["annotations", "logprobs"] {
                    if !p[k].is_null() && !p[k].as_array().is_some_and(Vec::is_empty) {
                        return Err(ProtocolError::Unsupported("annotated Chat history"));
                    }
                }
                json!({"type":"text","text":text(p,"text")?})
            }
            Some("refusal") if role == "assistant" => {
                fields(p, &["type", "refusal"])?;
                json!({"type":"refusal","refusal":text(p,"refusal")?})
            }
            Some("input_image") if role == "user" => {
                fields(p, &["type", "image_url", "detail"])?;
                let mut img = json!({"url":text(p,"image_url")?});
                if !p["detail"].is_null() {
                    img["detail"] = p["detail"].clone();
                }
                json!({"type":"image_url","image_url":img})
            }
            Some("input_file") if role == "user" => {
                fields(p, &["type", "filename", "file_data"])?;
                json!({"type":"file","file":{"file_data":text(p,"file_data")?,"filename":text(p,"filename")?}})
            }
            _ => return Err(ProtocolError::Unsupported("Chat message content")),
        };
        out.push(part);
    }
    Ok(out.into())
}

fn messages(body: &Value) -> Result<Vec<Value>> {
    let mut msgs = Vec::new();
    if !body["instructions"].is_null() {
        msgs.push(json!({"role":"system","content":text(body,"instructions")?}));
    }
    if let Some(s) = body["input"].as_str() {
        msgs.push(json!({"role":"user","content":s}));
        return Ok(msgs);
    }
    let mut calls = Calls::default();
    for item in array(&body["input"], "input")? {
        match item["type"].as_str() {
            None | Some("message") => {
                fields(item, &["type", "role", "content", "status", "id"])?;
                calls.resolved()?;
                let role = text(item, "role")?;
                if !matches!(role, "user" | "assistant" | "system" | "developer") {
                    return Err(ProtocolError::Invalid("message role"));
                }
                msgs.push(json!({"role":role,"content":content(&item["content"],role)?}));
            }
            Some("function_call") => {
                fields(
                    item,
                    &["id", "type", "call_id", "name", "arguments", "status"],
                )?;
                let call = id(item, "call_id")?;
                calls.call(call)?;
                if !msgs.last().is_some_and(|m| m["role"] == "assistant") {
                    msgs.push(json!({"role":"assistant","content":null}));
                }
                let tool = json!({
                    "type": "function",
                    "id": call,
                    "function": {"name": id(item,"name")?, "arguments": text(item,"arguments")?}
                });
                let message = msgs.last_mut().unwrap().as_object_mut().unwrap();
                let tools = message.entry("tool_calls").or_insert_with(|| json!([]));
                tools.as_array_mut().unwrap().push(tool);
            }
            Some("function_call_output") => {
                fields(item, &["id", "type", "call_id", "output", "status"])?;
                let call = id(item, "call_id")?;
                calls.result(call)?;
                msgs.push(json!({"role":"tool","tool_call_id":call,"content":content(&item["output"],"tool")?}));
            }
            _ => return Err(ProtocolError::Unsupported("Chat input item")),
        }
    }
    calls.resolved()?;
    if msgs.is_empty() {
        return Err(ProtocolError::Invalid("empty history"));
    }
    Ok(msgs)
}

pub(crate) fn request(body: &Value) -> Result<Value> {
    fields(
        body,
        &[
            "model",
            "input",
            "instructions",
            "stream",
            "store",
            "background",
            "previous_response_id",
            "conversation",
            "include",
            "max_output_tokens",
            "temperature",
            "top_p",
            "tools",
            "parallel_tool_calls",
            "tool_choice",
            "text",
            "reasoning",
            "metadata",
            "user",
            "service_tier",
            "safety_identifier",
            "truncation",
        ],
    )?;
    if body["store"] == true
        || body["background"] == true
        || !body["previous_response_id"].is_null()
        || !body["conversation"].is_null()
        || (!body["include"].is_null() && !body["include"].as_array().is_some_and(Vec::is_empty))
        || (!body["truncation"].is_null() && body["truncation"] != "disabled")
    {
        return Err(ProtocolError::Unsupported("native Responses option"));
    }
    let mut out = json!({"messages":messages(body)?,"store":false});
    for k in [
        "temperature",
        "top_p",
        "stream",
        "parallel_tool_calls",
        "metadata",
        "user",
        "service_tier",
        "safety_identifier",
    ] {
        if !body[k].is_null() {
            out[k] = body[k].clone();
        }
    }
    if body["stream"] == true {
        out["stream_options"] = json!({"include_usage":true});
    }
    if !body["reasoning"].is_null() {
        fields(&body["reasoning"], &["effort"])?;
        if !body["reasoning"]["effort"].is_null() {
            out["reasoning_effort"] = body["reasoning"]["effort"].clone();
        }
    }
    if !body["text"].is_null() {
        fields(&body["text"], &["format", "verbosity"])?;
        if !body["text"]["verbosity"].is_null() {
            out["verbosity"] = body["text"]["verbosity"].clone();
        }
        if let Some(fmt) = body["text"].get("format") {
            out["response_format"] = match text(fmt, "type")? {
                "json_schema" => {
                    fields(fmt, &["type", "name", "description", "strict", "schema"])?;
                    let mut obj = fmt.clone();
                    obj.as_object_mut().unwrap().remove("type");
                    json!({"type":"json_schema","json_schema":obj})
                }
                "text" | "json_object" => {
                    fields(fmt, &["type"])?;
                    fmt.clone()
                }
                _ => return Err(ProtocolError::Unsupported("response format")),
            };
        }
    }
    if let Some(tools) = body.get("tools") {
        let mut mapped = Vec::new();
        for t in array(tools, "tools")? {
            fields(t, &["type", "name", "description", "parameters", "strict"])?;
            if t["type"] != "function" {
                return Err(ProtocolError::Unsupported("Chat tool"));
            }
            id(t, "name")?;
            let mut f = t.clone();
            f.as_object_mut().unwrap().remove("type");
            if f["strict"].is_null() {
                let mut schema = f
                    .get("parameters")
                    .cloned()
                    .unwrap_or_else(|| json!({"type":"object"}));
                match super::super::convert::request::normalize_implicit(&mut schema) {
                    Ok(()) => {
                        f["strict"] = true.into();
                        f["parameters"] = schema;
                    }
                    Err(ProtocolError::Unsupported(_)) => f["strict"] = false.into(),
                    Err(e) => return Err(e),
                }
            }
            mapped.push(json!({"type":"function","function":f}));
        }
        out["tools"] = mapped.into();
    }
    if let Some(t) = body.get("tool_choice") {
        out["tool_choice"] = if t.is_object() {
            fields(t, &["type", "name"])?;
            if t["type"] != "function" {
                return Err(ProtocolError::Unsupported("tool choice"));
            }
            json!({"type":"function","function":{"name":id(t,"name")?}})
        } else {
            t.clone()
        };
    }
    Ok(out)
}
