//! Amazon Bedrock Runtime is an upstream transport. Clients use Messages or Responses.
pub(crate) mod stream;
pub(crate) mod transport;
pub mod wire;

use crate::protocol::{Endpoint, ProtocolError, convert, request::Generate};
use serde_json::{Value, json};

pub const FORMAT: &str = "aws.bedrock.converse.v1";
type Result<T> = std::result::Result<T, ProtocolError>;

fn tool_label<'a>(value: &'a Value, key: &'static str, is_name: bool) -> Result<&'a str> {
    let label = convert::id(value, key)?;
    if label.len() > 64
        || !label.bytes().all(|c| {
            c.is_ascii_alphanumeric()
                || c == b'-'
                || c == b'_'
                || (!is_name && matches!(c, b'.' | b':'))
        })
    {
        return Err(ProtocolError::Invalid(key));
    }
    Ok(label)
}

fn text_blocks(v: &Value) -> Result<Vec<Value>> {
    if let Some(s) = v.as_str() {
        return Ok(vec![json!({"text":s})]);
    }
    v.as_array()
        .ok_or(ProtocolError::Invalid("text content"))?
        .iter()
        .map(|b| {
            convert::fields(b, &["type", "text"])?;
            if b["type"] != "text" {
                return Err(ProtocolError::Unsupported("Bedrock content block"));
            }
            Ok(json!({"text":convert::text(b,"text")?}))
        })
        .collect()
}

pub fn prepare(request: &Generate) -> Result<Value> {
    let converted;
    let src = if request.endpoint == Endpoint::OpenaiResponses {
        converted = request.convert()?;
        &converted
    } else {
        request.body()
    };
    prepare_messages(src)
}

fn prepare_messages(src: &Value) -> Result<Value> {
    convert::fields(
        src,
        &[
            "model",
            "messages",
            "max_tokens",
            "stream",
            "system",
            "temperature",
            "top_p",
            "stop_sequences",
            "tools",
            "tool_choice",
        ],
    )?;
    let mut config = json!({});
    for (source, target) in [("temperature", "temperature"), ("top_p", "topP")] {
        if let Some(value) = src.get(source) {
            if !value.as_f64().is_some_and(|x| (0.0..=1.0).contains(&x)) {
                return Err(ProtocolError::Invalid("Bedrock sampling"));
            }
            config[target] = value.clone();
        }
    }
    if let Some(v) = src.get("stop_sequences") {
        let seq = v
            .as_array()
            .ok_or(ProtocolError::Invalid("stop sequences"))?;
        if seq.len() > 4 || seq.iter().any(|s| s.as_str().is_none_or(str::is_empty)) {
            return Err(ProtocolError::Invalid("stop sequences"));
        }
        if !seq.is_empty() {
            config["stopSequences"] = v.clone();
        }
    }
    let mut result = json!({"inferenceConfig":config});
    if let Some(v) = src.get("system") {
        result["system"] = text_blocks(v)?.into();
    }
    let mut messages = Vec::new();
    let mut calls = convert::request::Calls::default();
    for m in convert::array(&src["messages"], "messages")? {
        convert::fields(m, &["role", "content"])?;
        let role = convert::text(m, "role")?;
        if !matches!(role, "user" | "assistant") {
            return Err(ProtocolError::Unsupported("Bedrock role"));
        }
        if role == "assistant" {
            calls.resolved()?;
        }
        let mut parts = Vec::new();
        if m["content"].is_string() {
            calls.resolved()?;
            parts = text_blocks(&m["content"])?;
        } else {
            for b in convert::array(&m["content"], "content")? {
                let block = match (role, b["type"].as_str()) {
                    (_, Some("text")) => {
                        convert::fields(b, &["type", "text"])?;
                        if role == "user" {
                            calls.resolved()?;
                        }
                        json!({"text":convert::text(b,"text")?})
                    }
                    ("assistant", Some("tool_use")) => {
                        convert::fields(b, &["type", "id", "name", "input", "caller"])?;
                        if b.get("caller").is_some_and(|c| {
                            !c.is_null()
                                && (c["type"] != "direct" || convert::fields(c, &["type"]).is_err())
                        }) {
                            return Err(ProtocolError::Unsupported("tool caller"));
                        }
                        let id = tool_label(b, "id", false)?;
                        calls.call(id)?;
                        if !b["input"].is_object() {
                            return Err(ProtocolError::Invalid("tool input"));
                        }
                        json!({"toolUse":{"toolUseId":id,"name":tool_label(b,"name",true)?,"input":b["input"]}})
                    }
                    ("user", Some("tool_result")) => {
                        convert::fields(b, &["type", "tool_use_id", "content", "is_error"])?;
                        let id = tool_label(b, "tool_use_id", false)?;
                        calls.result(id)?;
                        let mut tool =
                            json!({"toolUseId":id,"content":text_blocks(&b["content"])?});
                        if let Some(v) = b.get("is_error") {
                            tool["status"] = if v
                                .as_bool()
                                .ok_or(ProtocolError::Invalid("tool result status"))?
                            {
                                "error"
                            } else {
                                "success"
                            }
                            .into();
                        }
                        json!({"toolResult":tool})
                    }
                    _ => return Err(ProtocolError::Unsupported("Bedrock content block")),
                };
                parts.push(block);
            }
        }
        if parts.is_empty() {
            return Err(ProtocolError::Invalid("empty content"));
        }
        messages.push(json!({"role":role,"content":parts}));
    }
    calls.resolved()?;
    if messages.is_empty() || messages.last().is_some_and(|v| v["role"] == "assistant") {
        return Err(ProtocolError::Unsupported(
            "incomplete history or assistant prefill",
        ));
    }
    result["messages"] = messages.into();
    let mut tools = Vec::new();
    let mut names = std::collections::HashSet::new();
    if let Some(v) = src.get("tools") {
        for t in convert::array(v, "tools")? {
            convert::fields(
                t,
                &["type", "name", "description", "input_schema", "strict"],
            )?;
            let name = tool_label(t, "name", true)?;
            if !names.insert(name) || t["input_schema"]["type"] != "object" {
                return Err(ProtocolError::Invalid("tool definition"));
            }
            let mut spec = json!({"name":name,"inputSchema":{"json":t["input_schema"]}});
            if let Some(s) = t.get("description") {
                spec["description"] = s
                    .as_str()
                    .ok_or(ProtocolError::Invalid("tool description"))?
                    .into();
            }
            if let Some(s) = t.get("strict") {
                spec["strict"] = s
                    .as_bool()
                    .ok_or(ProtocolError::Invalid("tool strictness"))?
                    .into();
            }
            tools.push(json!({"toolSpec":spec}));
        }
    }
    let mut tool_config = json!({"tools":tools});
    if let Some(v) = src.get("tool_choice") {
        convert::fields(v, &["type", "name", "disable_parallel_tool_use"])?;
        if v.get("disable_parallel_tool_use")
            .is_some_and(|v| v != false)
        {
            return Err(ProtocolError::Unsupported("serial tool choice"));
        }
        match convert::text(v, "type")? {
            "none" => return Ok(result),
            "auto" if !names.is_empty() => tool_config["toolChoice"] = json!({"auto":{}}),
            "any" if !names.is_empty() => tool_config["toolChoice"] = json!({"any":{}}),
            "tool" if names.contains(convert::id(v, "name")?) => {
                tool_config["toolChoice"] = json!({"tool":{"name":v["name"]}})
            }
            _ => return Err(ProtocolError::Unsupported("tool choice")),
        }
    }
    if !names.is_empty() {
        result["toolConfig"] = tool_config;
    }
    Ok(result)
}

/// Converse expresses cache boundaries as sibling blocks, including in system and tool lists.
pub fn prepare_cached(request: &Generate) -> Result<Value> {
    let mut src = if request.endpoint == Endpoint::OpenaiResponses {
        crate::protocol::cache::to_messages(request.body())?
    } else {
        request.body().clone()
    };
    let automatic = src.as_object_mut().unwrap().remove("cache_control");
    let mut marks = 0;
    let mut system = Vec::new();
    let mut tools = Vec::new();
    let mut turns = Vec::new();
    fn markers(blocks: &mut Value, count: &mut usize) -> Result<Vec<(usize, Value)>> {
        let mut marks = Vec::new();
        if let Some(blocks) = blocks.as_array_mut() {
            for (i, b) in blocks.iter_mut().enumerate() {
                if let Some(marker) = b.as_object_mut().and_then(|p| p.remove("cache_control")) {
                    marks.push((i, cache_point(&marker)?));
                    *count += 1;
                }
            }
        }
        Ok(marks)
    }
    fn cache_point(marker: &Value) -> Result<Value> {
        convert::fields(marker, &["type", "ttl"])?;
        if marker["type"] != "ephemeral"
            || marker.get("ttl").is_some_and(|v| v != "5m" && v != "1h")
        {
            return Err(ProtocolError::Unsupported("Bedrock cache lifetime"));
        }
        let mut point = json!({"cachePoint":{"type":"default"}});
        if let Some(ttl) = marker.get("ttl") {
            point["cachePoint"]["ttl"] = ttl.clone();
        }
        Ok(point)
    }
    fn insert(target: &mut Value, marks: Vec<(usize, Value)>) -> Result<()> {
        if marks.is_empty() {
            return Ok(());
        }
        let a = target
            .as_array_mut()
            .ok_or(ProtocolError::Unsupported("cache checkpoint location"))?;
        for (i, point) in marks.into_iter().rev() {
            if i >= a.len() {
                return Err(ProtocolError::Invalid("cache checkpoint index"));
            }
            a.insert(i + 1, point);
        }
        Ok(())
    }
    if let Some(v) = src.get_mut("system") {
        system = markers(v, &mut marks)?;
    }
    if let Some(v) = src.get_mut("tools") {
        tools = markers(v, &mut marks)?;
    }
    if let Some(msgs) = src["messages"].as_array_mut() {
        for (i, msg) in msgs.iter_mut().enumerate() {
            let content = msg
                .get_mut("content")
                .ok_or(ProtocolError::Invalid("message content"))?;
            let points = markers(content, &mut marks)?;
            if !points.is_empty() {
                turns.push((i, points));
            }
        }
    }
    let mut body = prepare_messages(&src)?;
    let mut auto_point = automatic.as_ref().map(cache_point).transpose()?;
    if marks == 0 && auto_point.is_none() && request.endpoint == Endpoint::AnthropicMessages {
        auto_point = Some(json!({"cachePoint":{"type":"default"}}));
    }
    // If the last content block already carries an equivalent marker, it also serves
    // as the automatic breakpoint. A conflicting explicit lifetime cannot be preserved.
    if let Some(auto) = &auto_point
        && let Some((i, points)) = turns.last()
        && *i + 1 == body["messages"].as_array().unwrap().len()
        && let Some((j, existing)) = points.last()
        && *j + 1 == body["messages"][*i]["content"].as_array().unwrap().len()
    {
        if existing["cachePoint"]["ttl"].as_str().unwrap_or("5m")
            != auto["cachePoint"]["ttl"].as_str().unwrap_or("5m")
        {
            return Err(ProtocolError::Unsupported("conflicting cache TTL"));
        }
        auto_point = None;
    }
    if marks + usize::from(auto_point.is_some()) > 4 {
        return Err(ProtocolError::Unsupported("too many cache checkpoints"));
    }
    if !system.is_empty() {
        insert(&mut body["system"], system)?;
    }
    if !tools.is_empty() {
        insert(&mut body["toolConfig"]["tools"], tools)?;
    }
    for (i, points) in turns {
        insert(&mut body["messages"][i]["content"], points)?;
    }
    if let Some(p) = auto_point {
        let last = body["messages"].as_array_mut().unwrap().last_mut().unwrap();
        last["content"].as_array_mut().unwrap().push(p);
    }
    Ok(body)
}
