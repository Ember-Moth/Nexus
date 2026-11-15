//! Chat Completions client wire format over a metered Responses exchange.
use super::{
    ProtocolError,
    convert::{fields, text},
};
use serde_json::{Value, json};
mod stream;
pub use stream::Stream;

type Result<T> = std::result::Result<T, ProtocolError>;

fn parts(content: &Value, assistant: bool) -> Result<Value> {
    if content.is_string() {
        return Ok(content.clone());
    }
    let array = content
        .as_array()
        .ok_or(ProtocolError::Invalid("chat content"))?;
    let mut out = Vec::new();
    for part in array {
        let v = match part["type"].as_str() {
            Some("text") => {
                fields(part, &["type", "text"])?;
                json!({"type":"input_text","text":text(part,"text")?})
            }
            Some("file") if !assistant => {
                fields(part, &["type", "file"])?;
                let file = &part["file"];
                fields(file, &["file_data", "file_id", "filename"])?;
                let id = file.get("file_id").filter(|v| !v.is_null());
                let data = file.get("file_data").filter(|v| !v.is_null());
                if id.is_some() == data.is_some() {
                    return Err(ProtocolError::Invalid("chat file source"));
                }
                if id.is_some() {
                    json!({"type":"input_file","file_id":text(file,"file_id")?})
                } else {
                    json!({"type":"input_file","filename":text(file,"filename")?,"file_data":text(file,"file_data")?})
                }
            }
            Some("image_url") if !assistant => {
                fields(part, &["type", "image_url"])?;
                let image = &part["image_url"];
                fields(image, &["url", "detail"])?;
                json!({"type":"input_image","image_url":text(image,"url")?,"detail":image.get("detail").cloned().unwrap_or(json!("auto"))})
            }
            _ => return Err(ProtocolError::Unsupported("chat content part")),
        };
        out.push(v);
    }
    Ok(out.into())
}

fn web_search(options: &Value) -> Result<Value> {
    fields(options, &["search_context_size", "user_location"])?;
    let mut tool = json!({"type":"web_search"});
    if let Some(size) = options.get("search_context_size").filter(|v| !v.is_null()) {
        if !matches!(size.as_str(), Some("low" | "medium" | "high")) {
            return Err(ProtocolError::Invalid("web search context size"));
        }
        tool["search_context_size"] = size.clone();
    }
    if let Some(location) = options.get("user_location").filter(|v| !v.is_null()) {
        fields(location, &["type", "approximate"])?;
        if location["type"] != "approximate" {
            return Err(ProtocolError::Invalid("web search location"));
        }
        let approximate = fields(
            &location["approximate"],
            &["city", "country", "region", "timezone"],
        )?;
        if approximate.values().any(|v| !v.is_string()) {
            return Err(ProtocolError::Invalid("approximate location"));
        }
        let mut flat = location["approximate"].clone();
        flat["type"] = "approximate".into();
        tool["user_location"] = flat;
    }
    Ok(tool)
}

fn citation(value: &Value, length: usize, base: usize) -> Result<Value> {
    let start = value["start_index"]
        .as_u64()
        .and_then(|n| usize::try_from(n).ok());
    let end = value["end_index"]
        .as_u64()
        .and_then(|n| usize::try_from(n).ok());
    let (start, end) = start
        .zip(end)
        .filter(|(start, end)| start <= end && *end <= length)
        .ok_or(ProtocolError::Invalid("URL citation range"))?;
    let url = text(value, "url")?;
    if !url::Url::parse(url)
        .is_ok_and(|u| matches!(u.scheme(), "https" | "http") && u.host().is_some())
    {
        return Err(ProtocolError::Invalid("citation URL"));
    }
    Ok(
        json!({"url":url,"title":text(value,"title")?,"start_index":base+start,"end_index":base+end}),
    )
}

fn add_urls(values: &[Value], base: usize, length: usize, out: &mut Vec<Value>) -> Result<()> {
    for a in values {
        fields(a, &["type", "url", "title", "start_index", "end_index"])?;
        if a["type"] != "url_citation" {
            return Err(ProtocolError::Unsupported("Chat annotation"));
        }
        out.push(json!({"type":"url_citation","url_citation":citation(a,length,base)?}));
    }
    Ok(())
}

pub(crate) fn assistant_parts(msg: &Value) -> Result<Vec<Value>> {
    let content = msg
        .get("content")
        .filter(|v| !v.is_null())
        .map(|_| text(msg, "content"))
        .transpose()?;
    let annotations = msg
        .get("annotations")
        .filter(|v| !v.is_null())
        .map(|v| super::convert::array(v, "Chat annotations"))
        .transpose()?;
    let mut parts = Vec::new();
    if let Some(content) = content {
        let length = content.chars().count();
        let mut mapped = Vec::new();
        for a in annotations.into_iter().flatten() {
            fields(a, &["type", "url_citation"])?;
            if a["type"] != "url_citation" {
                return Err(ProtocolError::Unsupported("Chat annotation"));
            }
            fields(
                &a["url_citation"],
                &["url", "title", "start_index", "end_index"],
            )?;
            let mut v = citation(&a["url_citation"], length, 0)?;
            v["type"] = "url_citation".into();
            mapped.push(v);
        }
        parts.push(json!({"type":"output_text","text":content,"annotations":mapped}));
    } else if annotations.is_some_and(|v| !v.is_empty()) {
        return Err(ProtocolError::Invalid("missing annotated text"));
    }
    if msg.get("refusal").is_some_and(|v| !v.is_null()) {
        parts.push(json!({"type":"refusal","refusal":text(msg,"refusal")?}));
    }
    Ok(parts)
}

fn assistant_output(msg: &Value, position: usize) -> Result<Value> {
    use sha2::{Digest, Sha256};
    let parts = assistant_parts(msg)?;
    // Explicit output history requires an item ID. Keep it stable across retries.
    let mut hash = Sha256::new();
    hash.update((position as u64).to_be_bytes());
    hash.update(serde_json::to_vec(&parts).expect("JSON parts"));
    let id = format!("msg_{:x}", hash.finalize());
    Ok(
        json!({"type":"message","id":&id[..36],"role":"assistant","status":"completed","content":parts}),
    )
}

pub fn request(body: &Value) -> Result<(Value, bool)> {
    fields(
        body,
        &[
            "model",
            "bedrock",
            "messages",
            "stream",
            "stream_options",
            "max_tokens",
            "max_completion_tokens",
            "temperature",
            "top_p",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "response_format",
            "web_search_options",
            "reasoning_effort",
            "prompt_cache_key",
            "prompt_cache_retention",
            "user",
            "service_tier",
            "store",
            "n",
            "stop",
            "metadata",
        ],
    )?;
    if body.get("n").is_some_and(|n| n != 1)
        || body.get("stop").is_some_and(|s| !s.is_null())
        || body.get("store").is_some_and(|s| s != false)
    {
        return Err(ProtocolError::Unsupported("chat generation options"));
    }
    let msgs = body["messages"]
        .as_array()
        .filter(|v| !v.is_empty())
        .ok_or(ProtocolError::Invalid("chat messages"))?;
    let mut input = Vec::new();
    for m in msgs {
        fields(
            m,
            &[
                "role",
                "content",
                "tool_calls",
                "tool_call_id",
                "annotations",
                "refusal",
            ],
        )?;
        let annotations = m.get("annotations").filter(|v| !v.is_null());
        let refusal = m.get("refusal").filter(|v| !v.is_null());
        if m["role"] != "assistant" && (annotations.is_some() || refusal.is_some()) {
            return Err(ProtocolError::Invalid("assistant message metadata"));
        }
        let role = text(m, "role")?;
        match role {
            "user" | "developer" | "system" | "assistant" => {
                if m.get("tool_call_id").is_some()
                    || (role != "assistant" && m.get("tool_calls").is_some())
                {
                    return Err(ProtocolError::Invalid("chat message tools"));
                }
                if refusal.is_some()
                    || annotations.is_some_and(|a| a.as_array().is_none_or(|v| !v.is_empty()))
                {
                    input.push(assistant_output(m, input.len())?);
                } else if !m["content"].is_null() {
                    input.push(
                        json!({"role":role,"content":parts(&m["content"],role=="assistant")?}),
                    );
                } else if m["tool_calls"].as_array().is_none_or(|a| a.is_empty()) {
                    return Err(ProtocolError::Invalid("chat content"));
                }
                if let Some(calls) = m.get("tool_calls").filter(|v| !v.is_null()) {
                    for call in calls
                        .as_array()
                        .ok_or(ProtocolError::Invalid("chat tool calls"))?
                    {
                        fields(call, &["id", "type", "function"])?;
                        if call["type"] != "function" {
                            return Err(ProtocolError::Unsupported("chat tool"));
                        }
                        let f = &call["function"];
                        fields(f, &["name", "arguments"])?;
                        input.push(json!({"type":"function_call","call_id":text(call,"id")?,
                            "name":text(f,"name")?,"arguments":text(f,"arguments")?}));
                    }
                }
            }
            "tool" => input.push(
                json!({"type":"function_call_output","call_id":text(m,"tool_call_id")?,
                "output":parts(&m["content"],false)?}),
            ),
            _ => return Err(ProtocolError::Unsupported("chat role")),
        }
    }
    let mut out = json!({"model":body["model"],"input":input});
    for k in [
        "bedrock",
        "temperature",
        "top_p",
        "parallel_tool_calls",
        "stream",
        "prompt_cache_key",
        "prompt_cache_retention",
        "user",
        "service_tier",
        "metadata",
    ] {
        if let Some(v) = body.get(k) {
            out[k] = v.clone();
        }
    }
    if body.get("max_tokens").is_some() && body.get("max_completion_tokens").is_some() {
        return Err(ProtocolError::Invalid("duplicate chat output limit"));
    }
    if let Some(v) = body.get("max_completion_tokens").or(body.get("max_tokens")) {
        out["max_output_tokens"] = v.clone();
    }
    if let Some(v) = body.get("reasoning_effort") {
        out["reasoning"] = json!({"effort":v});
    }
    if let Some(tools) = body.get("tools") {
        let mut mapped = Vec::new();
        for tool in tools
            .as_array()
            .ok_or(ProtocolError::Invalid("chat tools"))?
        {
            fields(tool, &["type", "function"])?;
            if tool["type"] != "function" {
                return Err(ProtocolError::Unsupported("chat tool"));
            }
            let mut f = tool["function"].clone();
            fields(&f, &["name", "description", "parameters", "strict"])?;
            f.as_object_mut()
                .unwrap()
                .entry("strict")
                .or_insert(Value::Bool(false));
            f["type"] = "function".into();
            mapped.push(f);
        }
        out["tools"] = mapped.into();
    }
    if let Some(choice) = body.get("tool_choice") {
        out["tool_choice"] = if choice.is_string() {
            choice.clone()
        } else {
            fields(choice, &["type", "function"])?;
            if choice["type"] != "function" {
                return Err(ProtocolError::Invalid("chat tool choice"));
            }
            json!({"type":"function","name":text(&choice["function"],"name")?})
        };
    }
    if let Some(options) = body.get("web_search_options").filter(|v| !v.is_null()) {
        // Search-enabled Chat requests require a search. Function selection has no
        // single Responses tool_choice that can enforce both operations.
        if out["tools"].as_array().is_some_and(|t| !t.is_empty())
            || out
                .get("tool_choice")
                .is_some_and(|choice| choice != "auto" && choice != "required")
        {
            return Err(ProtocolError::Unsupported("Chat search and tool choice"));
        }
        out["tools"] = json!([web_search(options)?]);
        out["tool_choice"] = json!({"type":"web_search"});
    }
    if let Some(fmt) = body.get("response_format") {
        let typ = text(fmt, "type")?;
        let format = if typ == "json_schema" {
            let mut v = fmt["json_schema"].clone();
            fields(&v, &["name", "description", "schema", "strict"])?;
            v["type"] = typ.into();
            v
        } else if matches!(typ, "text" | "json_object") {
            fields(fmt, &["type"])?;
            fmt.clone()
        } else {
            return Err(ProtocolError::Unsupported("chat response format"));
        };
        out["text"] = json!({"format":format});
    }
    let include_usage = if let Some(v) = body.get("stream_options").filter(|v| !v.is_null()) {
        fields(v, &["include_usage"])?;
        v.get("include_usage")
            .map(|b| b.as_bool().ok_or(ProtocolError::Invalid("include_usage")))
            .transpose()?
            .unwrap_or(false)
    } else {
        false
    };
    Ok((out, include_usage))
}

fn usage(u: &Value) -> Value {
    let Some((input, output)) = u["input_tokens"].as_u64().zip(u["output_tokens"].as_u64()) else {
        return Value::Null;
    };
    let Some(total) = input.checked_add(output) else {
        return Value::Null;
    };
    let mut out = json!({"prompt_tokens":u["input_tokens"],"completion_tokens":u["output_tokens"],"total_tokens":u.get("total_tokens").cloned().unwrap_or(total.into())});
    for (from, to) in [
        ("input_tokens_details", "prompt_tokens_details"),
        ("output_tokens_details", "completion_tokens_details"),
    ] {
        if let Some(v) = u.get(from) {
            out[to] = v.clone();
        }
    }
    out
}
fn finish(r: &Value, tools: bool) -> Result<&'static str> {
    match r["status"].as_str() {
        Some("completed") => Ok(if tools { "tool_calls" } else { "stop" }),
        Some("incomplete") if r["incomplete_details"]["reason"] == "max_output_tokens" => {
            Ok("length")
        }
        Some("incomplete") if r["incomplete_details"]["reason"] == "content_filter" => {
            Ok("content_filter")
        }
        _ => Err(ProtocolError::Unsupported("chat finish reason")),
    }
}
pub fn response(r: &Value, model: &str) -> Result<Value> {
    let mut annotations = Vec::new();
    let mut offset = 0;
    let mut content = String::new();
    let mut refusal = String::new();
    let mut calls = Vec::new();
    for item in r["output"]
        .as_array()
        .ok_or(ProtocolError::Invalid("response output"))?
    {
        match item["type"].as_str() {
            Some("message") => {
                for p in item["content"]
                    .as_array()
                    .ok_or(ProtocolError::Invalid("output content"))?
                {
                    match p["type"].as_str() {
                        Some("output_text") => {
                            let value = text(p, "text")?;
                            let length = value.chars().count();
                            if let Some(a) = p.get("annotations").filter(|v| !v.is_null()) {
                                add_urls(
                                    super::convert::array(a, "output annotations")?,
                                    offset,
                                    length,
                                    &mut annotations,
                                )?;
                            }
                            offset += length;
                            content.push_str(value);
                        }
                        Some("refusal") => refusal.push_str(text(p, "refusal")?),
                        _ => return Err(ProtocolError::Unsupported("chat output part")),
                    }
                }
            }
            Some("function_call") => calls
                .push(json!({"type":"function","id":text(item,"call_id")?,
                "function":{"name":text(item,"name")?,"arguments":text(item,"arguments")?}})),
            Some("reasoning" | "web_search_call") => {}
            _ => return Err(ProtocolError::Unsupported("chat output item")),
        }
    }
    let reason = finish(r, !calls.is_empty())?;
    let mut msg = json!({"role":"assistant","content":if content.is_empty() && !calls.is_empty(){Value::Null}else{content.into()}});
    if !annotations.is_empty() {
        msg["annotations"] = annotations.into();
    }
    if !refusal.is_empty() {
        msg["refusal"] = refusal.into();
    }
    if !calls.is_empty() {
        msg["tool_calls"] = calls.into();
    }
    Ok(
        json!({"id":text(r,"id")?,"created":r["created_at"].as_i64().unwrap_or(0),"model":model,
      "object":"chat.completion","choices":[{"index":0,"message":msg,"finish_reason":reason}],"usage":usage(&r["usage"])}),
    )
}
