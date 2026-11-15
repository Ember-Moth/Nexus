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
    with_history(source, body, false, false, &Default::default())
}

pub(crate) fn convert_cached(source: Endpoint, body: &Value) -> Result<Value> {
    with_history(source, body, true, false, &Default::default())
}

use crate::protocol::approximation::{Choices, Lifetime, Policy};

type MediaMapper = fn(&Value) -> Result<Value>;

// Tool results with block-level cache hints need their block boundaries preserved.
fn result_parts(
    source: Endpoint,
    parts: &Value,
    cache: bool,
    marks: &mut usize,
    media: MediaMapper,
    policy: &mut Policy,
) -> Result<Vec<Value>> {
    let (from, to, hint) = match source {
        Endpoint::AnthropicMessages => ("text", "input_text", "cache_control"),
        Endpoint::OpenaiResponses => ("input_text", "text", "prompt_cache_breakpoint"),
    };
    array(parts, "tool result content")?
        .iter()
        .map(|part| {
            match (source, part["type"].as_str()) {
                (Endpoint::AnthropicMessages, Some("image" | "document")) => {
                    return media(part);
                }
                (Endpoint::OpenaiResponses, Some("input_image" | "input_file")) => {
                    return media(part);
                }
                _ => {}
            }
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
                policy.breakpoint(source, part, &mut out)?;
                *marks += usize::from(part.get(hint).is_some());
            }
            Ok(out)
        })
        .collect()
}

pub(crate) fn with_history(
    source: Endpoint,
    body: &Value,
    cache: bool,
    converse: bool,
    history: &crate::protocol::history::Restored,
) -> Result<Value> {
    with_options(source, body, cache, converse, history, Choices::default()).map(|(v, _)| v)
}
pub(crate) fn with_options(
    source: Endpoint,
    body: &Value,
    cache: bool,
    converse: bool,
    history: &crate::protocol::history::Restored,
    allow: Choices,
) -> Result<(Value, Choices)> {
    let mut policy = Policy::new(allow);
    let media: MediaMapper = if converse {
        crate::protocol::media::converse_file
    } else {
        crate::protocol::media::to_messages
    };
    let mut out = match source {
        Endpoint::AnthropicMessages => messages(body, cache, history, &mut policy)?,
        Endpoint::OpenaiResponses => responses(body, cache, media, history, &mut policy)?,
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
    reasoning(source, body, &mut out, history)?;
    tools(source, body, &mut out)?;
    if policy.applied.mapped_cache() {
        crate::protocol::approximation::check_messages_order(
            if source == Endpoint::AnthropicMessages {
                body
            } else {
                &out
            },
        )?;
    }
    Ok((out, policy.applied))
}

fn reasoning(
    source: Endpoint,
    body: &Value,
    out: &mut Value,
    history: &crate::protocol::history::Restored,
) -> Result<()> {
    let shared_effort = |v: &Value| {
        matches!(
            v.as_str(),
            Some("low" | "medium" | "high" | "xhigh" | "max")
        )
    };
    match source {
        Endpoint::AnthropicMessages => {
            if let Some(management) = body.get("context_management") {
                fields(management, &["edits"])?;
                let edits = array(&management["edits"], "context edits")?;
                if edits.len() != 1 || edits[0]["type"] != "compact_20260112" {
                    return Err(ProtocolError::Unsupported("context management"));
                }
                let edit = &edits[0];
                fields(edit, &["type", "trigger", "pause_after_compaction"])?;
                if edit
                    .get("pause_after_compaction")
                    .is_some_and(|v| v != false)
                {
                    return Err(ProtocolError::Unsupported("pause after compaction"));
                }
                let trigger = &edit["trigger"];
                fields(trigger, &["type", "value"])?;
                if trigger["type"] != "input_tokens" {
                    return Err(ProtocolError::Unsupported("compaction trigger"));
                }
                let n = trigger["value"]
                    .as_u64()
                    .filter(|&n| n > 0)
                    .ok_or(ProtocolError::Invalid("compact threshold"))?;
                out["context_management"] = json!([{"type":"compaction","compact_threshold":n}]);
            }
            if let Some(config) = body.get("output_config") {
                fields(config, &["effort"])?;
                if !shared_effort(&config["effort"]) {
                    return Err(ProtocolError::Unsupported("reasoning effort"));
                }
                out["reasoning"]["effort"] = config["effort"].clone();
            }
            if let Some(thinking) = body.get("thinking") {
                fields(thinking, &["type", "display"])?;
                match text(thinking, "type")? {
                    "adaptive" => match thinking.get("display").and_then(Value::as_str) {
                        None | Some("summarized")
                            if thinking.get("display").is_none_or(Value::is_string) =>
                        {
                            out["reasoning"]["summary"] = "auto".into()
                        }
                        Some("omitted") => {}
                        _ => return Err(ProtocolError::Unsupported("thinking display")),
                    },
                    "disabled"
                        if thinking.get("display").is_none() && out.get("reasoning").is_none() =>
                    {
                        out["reasoning"] = json!({"effort":"none"})
                    }
                    _ => return Err(ProtocolError::Unsupported("thinking configuration")),
                }
            }
            if body["thinking"]["type"] == "adaptive"
                || body.get("output_config").is_some()
                || !history.is_empty()
            {
                out["include"] = json!(["reasoning.encrypted_content"]);
            }
        }
        Endpoint::OpenaiResponses => {
            if let Some(include) = body.get("include")
                && array(include, "include")?
                    .iter()
                    .any(|v| v != "reasoning.encrypted_content")
            {
                return Err(ProtocolError::Unsupported("Responses include"));
            }
            if let Some(reasoning) = body.get("reasoning") {
                fields(reasoning, &["effort", "summary"])?;
                if let Some(effort) = reasoning.get("effort").filter(|v| !v.is_null()) {
                    if effort == "none" {
                        out["thinking"] = json!({"type":"disabled"});
                    } else if shared_effort(effort) {
                        out["thinking"] = json!({"type":"adaptive"});
                        out["output_config"] = json!({"effort":effort});
                    } else {
                        return Err(ProtocolError::Unsupported("reasoning effort"));
                    }
                }
                if let Some(summary) = reasoning.get("summary").filter(|v| !v.is_null()) {
                    if summary != "auto" || out["thinking"]["type"] == "disabled" {
                        return Err(ProtocolError::Unsupported("reasoning summary"));
                    }
                    out["thinking"] = json!({"type":"adaptive","display":"summarized"});
                } else if out["thinking"]["type"] == "adaptive" {
                    out["thinking"]["display"] = "omitted".into();
                }
            }
            if let Some(management) = body.get("context_management") {
                let edits = array(management, "context_management")?;
                if edits.len() != 1 || edits[0]["type"] != "compaction" {
                    return Err(ProtocolError::Unsupported("context management"));
                }
                fields(&edits[0], &["type", "compact_threshold"])?;
                let n = edits[0]["compact_threshold"]
                    .as_u64()
                    .filter(|&n| n > 0)
                    .ok_or(ProtocolError::Invalid("compact threshold"))?;
                out["context_management"] = json!({"edits":[{"type":"compact_20260112","trigger":{"type":"input_tokens","value":n}}]});
            }
        }
    }
    Ok(())
}

fn messages(
    body: &Value,
    cache: bool,
    history: &crate::protocol::history::Restored,
    policy: &mut Policy,
) -> Result<Value> {
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
            "context_management",
            "thinking",
            "output_config",
            "cache_control",
        ],
    )?;
    let automatic = body.get("cache_control");
    let automatic_target = if let Some(marker) = automatic {
        if !cache {
            return Err(ProtocolError::Unsupported("cache_control"));
        }
        let target = policy.marker(Endpoint::AnthropicMessages, marker, false)?;
        policy.default_ttl = marker
            .get("ttl")
            .map(|_| Lifetime::parse(text(marker, "ttl")?))
            .transpose()?;
        Some(target)
    } else {
        None
    };
    if let Some(stops) = body.get("stop_sequences") {
        crate::protocol::approximation::validate_stops(stops)?;
        if stops.as_array().is_some_and(|v| !v.is_empty()) {
            if !policy.allow.stop {
                return Err(ProtocolError::Unsupported("stop_sequences"));
            }
            policy.applied.stop = true;
        }
    }
    if body["messages"]
        .as_array()
        .and_then(|m| m.last())
        .is_some_and(|m| m["role"] == "assistant")
    {
        if !policy.allow.prefill {
            return Err(ProtocolError::Unsupported("assistant prefill"));
        }
        crate::protocol::approximation::prefill(body)?;
        policy.applied.prefill = true;
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
                policy.breakpoint(Endpoint::AnthropicMessages, part, &mut block)?;
                marks += usize::from(part.get("cache_control").is_some());
                content.push(block);
            }
            input.push(json!({"role":"developer","content":content}));
        } else {
            out["instructions"] = plain_text(system, "text")?.join("\n\n").into();
        }
    }
    let mut calls = Calls::default();
    for (turn, m) in array(&body["messages"], "messages")?.iter().enumerate() {
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
        let mut next = 0;
        for (index, p) in parts.iter().enumerate() {
            if index < next {
                continue;
            }
            if let Some((original, span)) = history.get(turn, index) {
                if role != "assistant" {
                    return Err(ProtocolError::Invalid("private history role"));
                }
                if !text_parts.is_empty() {
                    input.push(json!({"role":role,"content":std::mem::take(&mut text_parts)}));
                }
                input.push(original.clone());
                next = index + span;
                continue;
            }
            if role == "user" && matches!(p["type"].as_str(), Some("image" | "document")) {
                calls.resolved()?;
                text_parts.push(crate::protocol::media::from_messages(p)?);
                continue;
            }
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
                    policy.breakpoint(Endpoint::AnthropicMessages, p, &mut part)?;
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
                                crate::protocol::media::from_messages,
                                policy,
                            )?
                        };
                        if boundary.is_some() {
                            let last = parts
                                .last_mut()
                                .ok_or(ProtocolError::Unsupported("empty cache breakpoint"))?;
                            if last["type"] != "input_text" {
                                return Err(ProtocolError::Unsupported("media cache breakpoint"));
                            }
                            if last.get("prompt_cache_breakpoint").is_none() {
                                marks += 1;
                            }
                            policy.breakpoint(Endpoint::AnthropicMessages, p, last)?;
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
    if let Some(ttl) = automatic_target.as_ref().and_then(|m| m.get("ttl")) {
        out["prompt_cache_options"]["ttl"] = ttl.clone();
    }
    if policy.applied.prefill {
        input.push(json!({"role":"user","content":"Continue the preceding assistant message from its last character. Return only the continuation."}));
    }
    out["input"] = input.into();
    Ok(out)
}

fn responses(
    body: &Value,
    cache: bool,
    media: MediaMapper,
    history: &crate::protocol::history::Restored,
    policy: &mut Policy,
) -> Result<Value> {
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
            "reasoning",
            "include",
            "context_management",
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
        if let Some(ttl) = v.get("ttl") {
            let ttl = Lifetime::parse(
                ttl.as_str()
                    .ok_or(ProtocolError::Invalid("cache lifetime"))?,
            )?;
            if ttl.endpoint() != Endpoint::OpenaiResponses {
                return Err(ProtocolError::Invalid("Responses cache lifetime"));
            }
            policy.default_ttl = Some(ttl);
        }
        if v.get("mode")
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
        for (index, item) in array(&body["input"], "input")?.iter().enumerate() {
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
            if let Some((original, _)) = history.get(0, index) {
                if calls.answering {
                    calls.resolved()?;
                }
                message_block(&mut msgs, "assistant", original.clone());
                continue;
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
                                policy.breakpoint(Endpoint::OpenaiResponses, p, &mut block)?;
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
                            if role == "user"
                                && matches!(
                                    part["type"].as_str(),
                                    Some("input_image" | "input_file")
                                )
                            {
                                message_block(&mut msgs, role, media(part)?);
                                continue;
                            }

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
                                policy.breakpoint(Endpoint::OpenaiResponses, part, &mut block)?;
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
                            media,
                            policy,
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
        if let Some(ttl) = policy.default_ttl {
            out["cache_control"]["ttl"] = policy
                .lifetime(Endpoint::OpenaiResponses, ttl)?
                .name()
                .into();
        }
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
            if let Some(strict) = tool.get("strict").filter(|v| !v.is_null()) {
                t["strict"] = strict
                    .as_bool()
                    .ok_or(ProtocolError::Invalid("strict tool"))?
                    .into();
            } else if !m {
                // Responses closes objects when normalizing a compatible schema. For
                // fully required properties we can reproduce that without changing optionality.
                // Keep the original schema on best-effort fallback, including nested
                // objects already visited by the attempted normalization.
                match normalize_implicit(&mut t["input_schema"]) {
                    Ok(()) => t["strict"] = true.into(),
                    Err(ProtocolError::Unsupported(_)) => {
                        t["input_schema"] = schema.clone();
                        t["strict"] = false.into();
                    }
                    Err(e) => return Err(e),
                }
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

// Schemas outside this normalizable subset retain their constraints in non-strict mode.
pub(crate) fn normalize_implicit(schema: &mut Value) -> Result<()> {
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
