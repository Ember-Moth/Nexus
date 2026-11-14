use super::{Result, arguments, array, fields, id, text};
use crate::protocol::{Endpoint, ProtocolError};
use serde_json::{Value, json};
use std::collections::HashSet;

#[derive(Default)]
pub(crate) struct Calls<'a> {
    seen: HashSet<&'a str>,
    pending: HashSet<&'a str>,
    answering: bool,
}
impl<'a> Calls<'a> {
    pub(crate) fn call(&mut self, id: &'a str) -> Result<()> {
        if self.answering && !self.pending.is_empty() {
            return Err(ProtocolError::Invalid("interrupted tool results"));
        }
        self.answering = false;
        if !self.seen.insert(id) {
            return Err(ProtocolError::Invalid("duplicate call ID"));
        }
        self.pending.insert(id);
        Ok(())
    }
    pub(crate) fn result(&mut self, id: &str) -> Result<()> {
        self.answering = true;
        if !self.pending.remove(id) {
            return Err(ProtocolError::Invalid("unmatched tool result"));
        }
        Ok(())
    }
    pub(crate) fn resolved(&self) -> Result<()> {
        if self.pending.is_empty() {
            Ok(())
        } else {
            Err(ProtocolError::Invalid("missing tool result"))
        }
    }
}

// Adjacent blocks belonging to a single Anthropic turn must stay together (in particular, parallel tool results).
fn message_block(out: &mut Vec<Value>, role: &str, block: Value) {
    if let Some(last) = out.last_mut()
        && last["role"] == role
        && let Some(parts) = last["content"].as_array_mut()
    {
        parts.push(block);
    } else {
        out.push(json!({"role":role,"content":[block]}));
    }
}

fn plain_text(v: &Value, kind: &str) -> Result<Vec<String>> {
    if let Some(s) = v.as_str() {
        return Ok(vec![s.to_owned()]);
    }
    array(v, "text content")?
        .iter()
        .map(|part| {
            fields(part, &["type", "text", "annotations"])?;
            if part["type"] != kind
                || part
                    .get("annotations")
                    .is_some_and(|a| a.as_array().is_none_or(|a| !a.is_empty()))
            {
                return Err(ProtocolError::Unsupported("nonportable text block"));
            }
            Ok(text(part, "text")?.to_string())
        })
        .collect()
}

pub fn convert(source: Endpoint, body: &Value) -> Result<Value> {
    translate(source, body, false)
}

pub(crate) fn convert_cached(source: Endpoint, body: &Value) -> Result<Value> {
    translate(source, body, true)
}

// Tool results with block-level cache hints need their block boundaries preserved.
fn result_parts(
    source: Endpoint,
    parts: &Value,
    cache: bool,
    marks: &mut usize,
) -> Result<Vec<Value>> {
    let (from, to, hint) = match source {
        Endpoint::AnthropicMessages => ("text", "input_text", "cache_control"),
        Endpoint::OpenaiResponses => ("input_text", "text", "prompt_cache_breakpoint"),
    };
    array(parts, "tool result content")?
        .iter()
        .map(|part| {
            if cache {
                fields(part, &["type", "text", "annotations", hint])?;
            } else {
                fields(part, &["type", "text", "annotations"])?;
            }
            if part["type"] != from
                || part
                    .get("annotations")
                    .is_some_and(|a| !a.is_null() && a.as_array().is_none_or(|v| !v.is_empty()))
            {
                return Err(ProtocolError::Unsupported("nonportable tool result"));
            }
            let mut out = json!({"type":to,"text":text(part,"text")?});
            if cache {
                crate::protocol::cache::text_breakpoint(source, part, &mut out)?;
                *marks += usize::from(part.get(hint).is_some());
            }
            Ok(out)
        })
        .collect()
}

fn translate(source: Endpoint, body: &Value, cache: bool) -> Result<Value> {
    let mut out = match source {
        Endpoint::AnthropicMessages => messages(body, cache)?,
        Endpoint::OpenaiResponses => responses(body, cache)?,
    };
    if let Some(stream) = body.get("stream") {
        out["stream"] = stream.clone();
    }
    if let Some(top_p) = body.get("top_p") {
        if !top_p.as_f64().is_some_and(|x| (0.0..=1.0).contains(&x)) {
            return Err(ProtocolError::Invalid("top_p"));
        }
        out["top_p"] = top_p.clone();
    }
    if let Some(temp) = body.get("temperature") {
        if !temp.as_f64().is_some_and(|x| (0.0..=1.0).contains(&x)) {
            return Err(ProtocolError::Unsupported("temperature"));
        }
        out["temperature"] = temp.clone();
    }
    tools(source, body, &mut out)?;
    Ok(out)
}

fn messages(body: &Value, cache: bool) -> Result<Value> {
    fields(
        body,
        &[
            "model",
            "messages",
            "system",
            "tools",
            "tool_choice",
            "max_tokens",
            "stream",
            "temperature",
            "top_p",
            "stop_sequences",
            "cache_control",
        ],
    )?;
    let automatic = body.get("cache_control");
    if let Some(marker) = automatic {
        if !cache {
            return Err(ProtocolError::Unsupported("cache_control"));
        }
        crate::protocol::cache::check_marker(marker, Endpoint::AnthropicMessages)?;
    }
    if body
        .get("stop_sequences")
        .is_some_and(|v| v.as_array().is_none_or(|a| !a.is_empty()))
    {
        return Err(ProtocolError::Unsupported("stop_sequences"));
    }
    if body["messages"]
        .as_array()
        .and_then(|m| m.last())
        .is_some_and(|m| m["role"] == "assistant")
    {
        return Err(ProtocolError::Unsupported("assistant prefill"));
    }
    let mut out = json!({"store":false});
    let mut input = Vec::new();
    let mut marks = 0;
    if let Some(system) = body.get("system") {
        if cache
            && system
                .as_array()
                .is_some_and(|a| a.iter().any(|b| b.get("cache_control").is_some()))
        {
            let mut content = Vec::new();
            for part in array(system, "system")? {
                fields(part, &["type", "text", "cache_control"])?;
                if part["type"] != "text" {
                    return Err(ProtocolError::Unsupported("system block"));
                }
                let mut block = json!({"type":"input_text","text":text(part,"text")?});
                crate::protocol::cache::text_breakpoint(
                    Endpoint::AnthropicMessages,
                    part,
                    &mut block,
                )?;
                marks += usize::from(part.get("cache_control").is_some());
                content.push(block);
            }
            input.push(json!({"role":"developer","content":content}));
        } else {
            out["instructions"] = plain_text(system, "text")?.join("\n\n").into();
        }
    }
    let mut calls = Calls::default();
    for m in array(&body["messages"], "messages")? {
        fields(m, &["role", "content"])?;
        let role = text(m, "role")?;
        if !matches!(role, "assistant" | "user") {
            return Err(ProtocolError::Unsupported("message role"));
        }
        if role == "assistant" {
            calls.resolved()?;
        }
        if let Some(s) = m["content"].as_str() {
            calls.resolved()?;
            input.push(json!({"role":role,"content":s}));
            continue;
        }
        let parts = array(&m["content"], "message content")?;
        if parts.is_empty() {
            return Err(ProtocolError::Invalid("empty content"));
        }
        let mut text_parts = Vec::new();
        for p in parts {
            if p["type"] == "text" {
                fields(
                    p,
                    if cache {
                        &["type", "text", "cache_control"][..]
                    } else {
                        &["type", "text"]
                    },
                )?;
                if role == "user" {
                    calls.resolved()?;
                }
                // EasyInputMessage accepts input_text for explicit user and assistant history.
                let mut part = json!({"type":"input_text","text":text(p,"text")?});
                if cache {
                    crate::protocol::cache::text_breakpoint(
                        Endpoint::AnthropicMessages,
                        p,
                        &mut part,
                    )?;
                    marks += usize::from(p.get("cache_control").is_some());
                }
                text_parts.push(part);
                continue;
            }
            if !text_parts.is_empty() {
                input.push(json!({"role":role,"content":std::mem::take(&mut text_parts)}));
            }
            match (role, p["type"].as_str()) {
                ("assistant", Some("tool_use")) => {
                    fields(p, &["type", "id", "name", "input", "caller"])?;
                    if p.get("caller").is_some_and(|v| {
                        !v.is_null() && (v["type"] != "direct" || fields(v, &["type"]).is_err())
                    }) {
                        return Err(ProtocolError::Unsupported("tool caller"));
                    }

                    calls.call(id(p, "id")?)?;
                    if !p["input"].is_object() {
                        return Err(ProtocolError::Invalid("tool input"));
                    }
                    input.push(json!({"type":"function_call","call_id":p["id"],"name":id(p,"name")?,"arguments":p["input"].to_string()}));
                }
                ("user", Some("tool_result")) => {
                    fields(
                        p,
                        if cache {
                            &[
                                "type",
                                "tool_use_id",
                                "content",
                                "is_error",
                                "cache_control",
                            ][..]
                        } else {
                            &["type", "tool_use_id", "content", "is_error"]
                        },
                    )?;
                    if p.get("is_error").is_some_and(|v| v != false) {
                        return Err(ProtocolError::Unsupported("tool result error flag"));
                    }
                    calls.result(id(p, "tool_use_id")?)?;
                    let boundary = p.get("cache_control");
                    let output = if p["content"].is_string() && boundary.is_none() {
                        p["content"].clone()
                    } else {
                        let mut parts = if let Some(value) = p["content"].as_str() {
                            vec![json!({"type":"input_text","text":value})]
                        } else {
                            result_parts(
                                Endpoint::AnthropicMessages,
                                &p["content"],
                                cache,
                                &mut marks,
                            )?
                        };
                        if boundary.is_some() {
                            let last = parts
                                .last_mut()
                                .ok_or(ProtocolError::Unsupported("empty cache breakpoint"))?;
                            if last.get("prompt_cache_breakpoint").is_none() {
                                marks += 1;
                            }
                            crate::protocol::cache::text_breakpoint(
                                Endpoint::AnthropicMessages,
                                p,
                                last,
                            )?;
                        }
                        Value::Array(parts)
                    };
                    input.push(json!({"type":"function_call_output","call_id":p["tool_use_id"],"output":output}));
                }
                _ => return Err(ProtocolError::Unsupported("message content block")),
            }
        }
        if !text_parts.is_empty() {
            input.push(json!({"role":role,"content":text_parts}));
        }
    }
    calls.resolved()?;
    if cache {
        if marks + usize::from(automatic.is_some()) > 4 {
            return Err(ProtocolError::Invalid("cache breakpoint count"));
        }
        if marks > 0 || automatic.is_some() {
            out["prompt_cache_options"] =
                json!({"mode":if automatic.is_some() {"implicit"} else {"explicit"}});
        }
    }
    out["input"] = input.into();
    Ok(out)
}

fn responses(body: &Value, cache: bool) -> Result<Value> {
    fields(
        body,
        &[
            "model",
            "input",
            "instructions",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "max_output_tokens",
            "stream",
            "temperature",
            "top_p",
            "store",
            "background",
            "previous_response_id",
            "prompt_cache_options",
            "prompt_cache_key",
        ],
    )?;
    let opts = body.get("prompt_cache_options");
    if !cache && (opts.is_some() || body.get("prompt_cache_key").is_some()) {
        return Err(ProtocolError::Unsupported("Responses cache options"));
    }
    if body.get("prompt_cache_key").is_some_and(|v| {
        v.as_str()
            .is_none_or(|s| !crate::protocol::cache::valid_key(s))
    }) {
        return Err(ProtocolError::Invalid("prompt_cache_key"));
    }
    if let Some(v) = opts {
        fields(v, &["mode", "ttl"])?;
        if v.get("ttl").is_some()
            || v.get("mode")
                .is_some_and(|v| v != "implicit" && v != "explicit")
        {
            return Err(ProtocolError::Unsupported("nonportable cache options"));
        }
    }
    let automatic = cache && opts.is_none_or(|v| v["mode"] != "explicit");
    let mut marks = 0;
    let mut marked_system = None;
    let mut out = json!({});
    let mut system = Vec::new();
    if let Some(s) = body.get("instructions") {
        system.push(
            s.as_str()
                .ok_or(ProtocolError::Invalid("instructions"))?
                .to_string(),
        );
    }
    let mut msgs = Vec::new();
    let mut calls = Calls::default();
    let mut item_ids = HashSet::new();
    if let Some(s) = body["input"].as_str() {
        msgs.push(json!({"role":"user","content":s}));
    } else {
        for item in array(&body["input"], "input")? {
            if !item.is_object() {
                return Err(ProtocolError::Invalid("input item"));
            }
            if let Some(i) = item.get("id")
                && !item_ids.insert(
                    i.as_str()
                        .filter(|s| !s.is_empty())
                        .ok_or(ProtocolError::Invalid("item ID"))?,
                )
            {
                return Err(ProtocolError::Invalid("duplicate item ID"));
            }
            if item.get("status").is_some_and(|v| v != "completed") {
                return Err(ProtocolError::Unsupported("unfinished history"));
            }
            match item.get("type").and_then(Value::as_str) {
                None | Some("message") => {
                    fields(item, &["type", "id", "role", "content", "status"])?;
                    let role = text(item, "role")?;
                    // Assistant text can follow a parallel call in the same output.
                    if role != "assistant" || calls.answering {
                        calls.resolved()?;
                    }
                    if matches!(role, "system" | "developer") {
                        if !msgs.is_empty() || !system.is_empty() || marked_system.is_some() {
                            return Err(ProtocolError::Unsupported("instruction hierarchy"));
                        }
                        if cache
                            && item["content"].as_array().is_some_and(|a| {
                                a.iter().any(|p| p.get("prompt_cache_breakpoint").is_some())
                            })
                        {
                            let mut blocks = Vec::new();
                            for p in array(&item["content"], "developer content")? {
                                fields(p, &["type", "text", "prompt_cache_breakpoint"])?;
                                if p["type"] != "input_text" {
                                    return Err(ProtocolError::Unsupported("developer content"));
                                }
                                let mut block = json!({"type":"text","text":text(p,"text")?});
                                crate::protocol::cache::text_breakpoint(
                                    Endpoint::OpenaiResponses,
                                    p,
                                    &mut block,
                                )?;
                                marks += usize::from(p.get("prompt_cache_breakpoint").is_some());
                                blocks.push(block);
                            }
                            marked_system = Some(blocks);
                        } else {
                            system.extend(plain_text(&item["content"], "input_text")?);
                        }

                        continue;
                    }
                    if !matches!(role, "assistant" | "user") {
                        return Err(ProtocolError::Unsupported("message role"));
                    }
                    if let Some(s) = item["content"].as_str() {
                        message_block(&mut msgs, role, json!({"type":"text","text":s}));
                    } else {
                        let parts = array(&item["content"], "content")?;
                        if parts.is_empty() {
                            return Err(ProtocolError::Invalid("empty content"));
                        }
                        for part in parts {
                            fields(
                                part,
                                if cache {
                                    &[
                                        "type",
                                        "text",
                                        "annotations",
                                        "logprobs",
                                        "prompt_cache_breakpoint",
                                    ][..]
                                } else {
                                    &["type", "text", "annotations", "logprobs"]
                                },
                            )?;
                            if !(part["type"] == "input_text"
                                || role == "assistant" && part["type"] == "output_text")
                                || ["annotations", "logprobs"].iter().any(|k| {
                                    part.get(k).is_some_and(|v| {
                                        !v.is_null() && v.as_array().is_none_or(|a| !a.is_empty())
                                    })
                                })
                            {
                                return Err(ProtocolError::Unsupported("nonportable content"));
                            }
                            let mut block = json!({"type":"text","text":text(part,"text")?});
                            if cache {
                                crate::protocol::cache::text_breakpoint(
                                    Endpoint::OpenaiResponses,
                                    part,
                                    &mut block,
                                )?;
                                marks += usize::from(part.get("prompt_cache_breakpoint").is_some());
                            }
                            message_block(&mut msgs, role, block);
                        }
                    }
                }
                Some("function_call") => {
                    fields(
                        item,
                        &["type", "id", "call_id", "name", "arguments", "status"],
                    )?;
                    calls.call(id(item, "call_id")?)?;
                    message_block(
                        &mut msgs,
                        "assistant",
                        json!({"type":"tool_use","id":item["call_id"],"name":id(item,"name")?,"input":arguments(item)?}),
                    );
                }
                Some("function_call_output") => {
                    fields(item, &["type", "id", "call_id", "output", "status"])?;
                    calls.result(id(item, "call_id")?)?;
                    let mut block = json!({"type":"tool_result","tool_use_id":item["call_id"]});
                    block["content"] = if let Some(s) = item["output"].as_str() {
                        json!(s)
                    } else {
                        let mut parts = result_parts(
                            Endpoint::OpenaiResponses,
                            &item["output"],
                            cache,
                            &mut marks,
                        )?;
                        // Messages can express the result boundary on the outer tool_result.
                        if let Some(last) = parts.last_mut()
                            && let Some(marker) =
                                last.as_object_mut().unwrap().remove("cache_control")
                        {
                            block["cache_control"] = marker;
                        }
                        if parts.iter().any(|v| v.get("cache_control").is_some()) {
                            return Err(ProtocolError::Unsupported(
                                "cache breakpoint inside tool result",
                            ));
                        }
                        Value::Array(parts)
                    };
                    message_block(&mut msgs, "user", block);
                }
                _ => return Err(ProtocolError::Unsupported("nonportable input item")),
            }
        }
    }
    calls.resolved()?;
    if msgs.last().is_some_and(|m| m["role"] == "assistant") {
        return Err(ProtocolError::Unsupported("assistant prefill"));
    }
    if msgs.is_empty() {
        return Err(ProtocolError::Invalid("empty history"));
    }
    if !system.is_empty() {
        out["system"] = system.join("\n\n").into();
    }
    if let Some(blocks) = marked_system {
        out["system"] = blocks.into();
    }
    if marks + usize::from(automatic) > 4 {
        return Err(ProtocolError::Unsupported("too many cache breakpoints"));
    }
    if automatic {
        out["cache_control"] = json!({"type":"ephemeral"});
    }
    out["messages"] = msgs.into();
    Ok(out)
}

fn tools(source: Endpoint, body: &Value, out: &mut Value) -> Result<()> {
    let mut names = HashSet::new();
    let m = source == Endpoint::AnthropicMessages;
    if let Some(v) = body.get("tools") {
        let list = array(v, "tools")?;
        let mut mapped = Vec::new();
        for tool in list {
            let name = id(tool, "name")?;
            if !names.insert(name) {
                return Err(ProtocolError::Invalid("duplicate tool name"));
            }
            if (m && tool.get("type").is_some_and(|v| v != "custom"))
                || (!m && tool["type"] != "function")
            {
                return Err(ProtocolError::Unsupported("nonportable tool"));
            }
            let (schema, mut t) = if m {
                fields(
                    tool,
                    &["name", "type", "description", "input_schema", "strict"],
                )?;
                (
                    &tool["input_schema"],
                    json!({"type":"function","name":name,"strict":false}),
                )
            } else {
                fields(
                    tool,
                    &["name", "type", "description", "parameters", "strict"],
                )?;
                (&tool["parameters"], json!({"name":name}))
            };
            if !schema.is_object() || schema["type"] != "object" {
                return Err(ProtocolError::Invalid("tool schema"));
            }
            t[if m { "parameters" } else { "input_schema" }] = schema.clone();
            if let Some(strict) = tool.get("strict") {
                t["strict"] = strict
                    .as_bool()
                    .ok_or(ProtocolError::Invalid("strict tool"))?
                    .into();
            } else if !m {
                // Responses closes objects when normalizing a compatible schema. For
                // fully required properties we can reproduce that without changing optionality.
                normalize_implicit(&mut t["input_schema"])?;
                t["strict"] = true.into();
            }
            if let Some(desc) = tool.get("description") {
                t["description"] = desc
                    .as_str()
                    .ok_or(ProtocolError::Invalid("description"))?
                    .into();
            }
            mapped.push(t);
        }
        out["tools"] = mapped.into();
    }
    if m {
        if let Some(choice) = body.get("tool_choice") {
            fields(choice, &["type", "name", "disable_parallel_tool_use"])?;
            if names.is_empty() && choice["type"] == "none" {
                return Ok(());
            }
            if names.is_empty() && choice["type"] != "none" {
                return Err(ProtocolError::Invalid("tool_choice without tools"));
            }
            out["tool_choice"] = match text(choice, "type")? {
                "auto" => json!("auto"),
                "none" => json!("none"),
                "any" => json!("required"),
                "tool" if names.contains(id(choice, "name")?) => {
                    json!({"type":"function","name":choice["name"]})
                }
                _ => return Err(ProtocolError::Unsupported("tool_choice")),
            };
            if choice["type"] != "tool" && choice.get("name").is_some() {
                return Err(ProtocolError::Invalid("tool choice name"));
            }
            if let Some(v) = choice.get("disable_parallel_tool_use") {
                out["parallel_tool_calls"] = (!v
                    .as_bool()
                    .ok_or(ProtocolError::Invalid("parallel tool use"))?)
                .into();
            }
        }
    } else {
        let mut choice = match body.get("tool_choice") {
            None => None,
            Some(v) if v == "auto" => Some(json!({"type":"auto"})),
            Some(v) if v == "required" => Some(json!({"type":"any"})),
            Some(v) if v == "none" => Some(json!({"type":"none"})),
            Some(v) => {
                fields(v, &["type", "name"])?;
                if v["type"] != "function" || !names.contains(id(v, "name")?) {
                    return Err(ProtocolError::Unsupported("tool_choice"));
                }
                Some(json!({"type":"tool","name":v["name"]}))
            }
        };
        if let Some(parallel) = body.get("parallel_tool_calls") {
            let flag = parallel
                .as_bool()
                .ok_or(ProtocolError::Invalid("parallel_tool_calls"))?;
            if !names.is_empty() {
                choice.get_or_insert(json!({"type":"auto"}))["disable_parallel_tool_use"] =
                    (!flag).into();
            }
        }
        if let Some(c) = choice {
            if names.is_empty() && c["type"] == "none" {
                return Ok(());
            }
            if names.is_empty() && c["type"] != "none" {
                return Err(ProtocolError::Invalid("tool_choice without tools"));
            }
            out["tool_choice"] = c;
        }
    }
    Ok(())
}

// Leave more complex or optional schemas to an explicit strict setting.
fn normalize_implicit(schema: &mut Value) -> Result<()> {
    let bad = ProtocolError::Unsupported("implicit tool schema normalization");
    let obj = schema
        .as_object_mut()
        .ok_or(ProtocolError::Invalid("tool schema"))?;
    if obj.keys().any(|k| {
        ![
            "type",
            "properties",
            "required",
            "additionalProperties",
            "items",
            "description",
            "enum",
            "title",
        ]
        .contains(&k.as_str())
    }) {
        return Err(bad);
    }
    match obj.get("type").and_then(Value::as_str) {
        Some("object") => {
            if obj.get("additionalProperties").is_some_and(|v| v != false) {
                return Err(bad);
            }
            obj.entry("properties").or_insert_with(|| json!({}));
            let props = obj["properties"]
                .as_object()
                .ok_or(ProtocolError::Invalid("properties"))?;
            let required = obj
                .get("required")
                .map(|v| array(v, "required"))
                .transpose()?;
            if props.len() != required.map_or(0, Vec::len)
                || props
                    .keys()
                    .any(|k| !required.is_some_and(|r| r.iter().any(|v| v == k.as_str())))
            {
                return Err(bad);
            }
            for prop in obj
                .get_mut("properties")
                .and_then(Value::as_object_mut)
                .expect("validated properties")
                .values_mut()
            {
                normalize_implicit(prop)?;
            }
            obj.insert("additionalProperties".into(), false.into());
            obj.entry("required").or_insert_with(|| json!([]));
        }
        Some("array") => normalize_implicit(
            obj.get_mut("items")
                .ok_or(ProtocolError::Invalid("array items"))?,
        )?,
        Some("string" | "integer" | "number" | "boolean" | "null") => {}
        _ => return Err(bad),
    }
    Ok(())
}
