use super::{Endpoint, ProtocolError, usage::UsageEvidence};
use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Terminal {
    Completed,
    Failed,
    Incomplete,
    Cancelled,
}

#[derive(Debug)]
pub struct Completion {
    pub terminal: Terminal,
    pub response_id: Option<String>,
    pub usage: UsageEvidence,
    pub stop_reason: Option<String>,
}

impl Completion {
    pub fn outcome(&self, endpoint: Endpoint) -> &'static str {
        match self.terminal {
            Terminal::Failed => "failed",
            Terminal::Cancelled => "cancelled",
            Terminal::Completed
                if endpoint == Endpoint::OpenaiResponses
                    || matches!(
                        self.stop_reason.as_deref(),
                        Some(
                            "end_turn"
                                | "max_tokens"
                                | "stop_sequence"
                                | "tool_use"
                                | "pause_turn"
                                | "compaction"
                                | "refusal"
                                | "model_context_window_exceeded"
                        )
                    ) =>
            {
                "succeeded"
            }
            Terminal::Incomplete
                if matches!(
                    self.stop_reason.as_deref(),
                    Some("max_output_tokens" | "content_filter")
                ) =>
            {
                "succeeded"
            }
            _ => "unknown",
        }
    }
}

/// Observes native events before any downstream conversion. Partial streams never
/// receive a synthetic success or an estimated final usage.
pub struct Observer {
    endpoint: Endpoint,
    id: Option<String>,
    model: Option<String>,
    saw_compaction: bool,
    invalid_iterations: bool,
    open_compaction: Option<u64>,
    usage: Option<Value>,
    stop_reason: Option<String>,
    ended: bool,
    search_metered: crate::protocol::hosted::SearchMeters,
    search_items: std::collections::HashMap<String, Option<String>>,
}

impl Observer {
    pub fn new(endpoint: Endpoint) -> Self {
        Self::with_search(endpoint, Default::default())
    }
    pub(crate) fn with_search(
        endpoint: Endpoint,
        search_metered: crate::protocol::hosted::SearchMeters,
    ) -> Self {
        Self {
            endpoint,
            id: None,
            model: None,
            saw_compaction: false,
            invalid_iterations: false,
            open_compaction: None,
            usage: None,
            stop_reason: None,
            ended: false,
            search_items: Default::default(),
            search_metered,
        }
    }

    pub fn observe(
        &mut self,
        name: Option<&str>,
        event: &Value,
    ) -> Result<Option<Completion>, ProtocolError> {
        let kind = event
            .get("type")
            .and_then(Value::as_str)
            .ok_or(ProtocolError::Invalid("event type"))?;
        if self.ended || name.is_some_and(|v| v != kind) {
            return Err(ProtocolError::UnexpectedEvent);
        }
        let terminal = match (self.endpoint, kind) {
            (_, "error") => Terminal::Failed,
            (Endpoint::OpenaiResponses, "keepalive") => return Ok(None),
            (Endpoint::AnthropicMessages, "ping") => return Ok(None),
            (Endpoint::AnthropicMessages, "message_start") => {
                if self.id.is_some() {
                    return Err(ProtocolError::UnexpectedEvent);
                }
                let msg = &event["message"];
                self.set_id(msg)?;
                self.model = msg["model"].as_str().map(str::to_owned);
                let mut start = msg.get("usage").cloned();
                if let Some(obj) = start.as_mut().and_then(Value::as_object_mut)
                    && obj
                        .get("iterations")
                        .is_some_and(|v| v.as_array().is_some_and(Vec::is_empty))
                {
                    obj.remove("iterations");
                }
                if let Err(err) =
                    UsageEvidence::for_model(self.endpoint, start.as_ref(), self.model.as_deref())
                {
                    if start.as_ref().and_then(|v| v.get("iterations")).is_none() {
                        return Err(err);
                    }
                    self.invalid_iterations = true;
                }
                let provisional = msg.get("usage").is_some_and(|u| {
                    u.get("billing_usage").is_none()
                        && [
                            "claude_cache_creation_5_m_tokens",
                            "claude_cache_creation_1_h_tokens",
                            "input_tokens",
                            "cache_read_input_tokens",
                            "cache_creation_input_tokens",
                            "output_tokens",
                        ]
                        .iter()
                        .all(|key| u[key].as_u64() == Some(0))
                });
                // A compatible gateway sends an all-zero start placeholder and a full
                // usage object at message_delta. Discard the placeholder; only terminal
                // facts may establish zero input/cache in this dialect.
                self.usage = if provisional { None } else { start };
                if let Some(usage) = self.usage.as_mut().and_then(Value::as_object_mut) {
                    // This is a provisional output count, even when it is zero.
                    usage.remove("output_tokens");
                }
                return Ok(None);
            }
            (Endpoint::AnthropicMessages, "message_delta") => {
                if self.id.is_none() {
                    return Err(ProtocolError::UnexpectedEvent);
                }
                if let Err(err) = UsageEvidence::for_model(
                    self.endpoint,
                    event.get("usage"),
                    self.model.as_deref(),
                ) {
                    if event["usage"].get("iterations").is_none() && !self.saw_compaction {
                        return Err(err);
                    }
                    self.invalid_iterations = true;
                }
                let delta = event.get("usage").and_then(Value::as_object);
                // Only accepted observations get merged; partial evidence is never
                // padded with zero. The terminal cumulative output supersedes prior values.
                if let Some(delta) = delta
                    && !self.invalid_iterations
                {
                    if self.usage.as_ref().is_none_or(Value::is_null) {
                        self.usage = Some(serde_json::json!({}));
                    }
                    let current = self
                        .usage
                        .as_mut()
                        .and_then(Value::as_object_mut)
                        .ok_or(ProtocolError::Invalid("message usage"))?;
                    for (key, value) in delta {
                        if key == "server_tool_use"
                            && let Some(counters) = value.as_object()
                        {
                            let merged = current
                                .entry(key.clone())
                                .or_insert_with(|| serde_json::json!({}));
                            if merged.is_null() {
                                *merged = serde_json::json!({});
                            }
                            let merged = merged
                                .as_object_mut()
                                .ok_or(ProtocolError::Invalid("server tool usage"))?;
                            for (k, v) in counters {
                                if merged.get(k).and_then(Value::as_u64) > v.as_u64() {
                                    return Err(ProtocolError::Invalid(
                                        "cumulative server tool usage",
                                    ));
                                }
                                merged.insert(k.clone(), v.clone());
                            }
                            continue;
                        }
                        if key == "output_tokens"
                            && delta.get("iterations").is_some_and(Value::is_array)
                            && current
                                .get(key)
                                .and_then(Value::as_u64)
                                .zip(value.as_u64())
                                .is_some_and(|(a, b)| a > b)
                        {
                            self.invalid_iterations = true;
                            break;
                        }
                        if key == "iterations" {
                            if current.get(key).is_some_and(|old| {
                                !super::usage::iterations::cumulative(old, value)
                            }) {
                                self.invalid_iterations = true;
                                break;
                            }
                            current.insert(key.clone(), value.clone());
                            continue;
                        }
                        if delta.get("iterations").is_some_and(Value::is_array)
                            && matches!(
                                key.as_str(),
                                "input_tokens"
                                    | "cache_creation_input_tokens"
                                    | "cache_read_input_tokens"
                                    | "cache_creation"
                                    | "output_tokens"
                            )
                            && (current
                                .get(key)
                                .and_then(Value::as_u64)
                                .zip(value.as_u64())
                                .is_some_and(|(a, b)| a <= b)
                                || key == "cache_creation"
                                    && current.get(key).and_then(Value::as_object).is_some_and(
                                        |o| {
                                            o.iter().all(|(k, a)| {
                                                a.as_u64()
                                                    .zip(value[k].as_u64())
                                                    .is_some_and(|(a, b)| a <= b)
                                            })
                                        },
                                    ))
                        {
                            current.insert(key.clone(), value.clone());
                            continue;
                        }
                        if key == "output_tokens_details"
                            && value["thinking_tokens"].as_u64().is_some()
                            && current.get(key).is_none_or(|old| {
                                old["thinking_tokens"].as_u64() <= value["thinking_tokens"].as_u64()
                            })
                        {
                            current.insert(key.clone(), value.clone());
                            continue;
                        }
                        if key != "output_tokens"
                            && current
                                .get(key)
                                .is_some_and(|old| !old.is_null() && old != value)
                        {
                            if delta.contains_key("iterations") || self.saw_compaction {
                                self.invalid_iterations = true;
                                break;
                            }
                            return Err(ProtocolError::Invalid("conflicting message usage"));
                        }
                        if key == "output_tokens"
                            && let (Some(old), Some(new)) =
                                (current.get(key).and_then(Value::as_u64), value.as_u64())
                            && new < old
                        {
                            return Err(ProtocolError::Invalid("cumulative output"));
                        }
                        current.insert(key.clone(), value.clone());
                    }
                }
                // Earlier deltas cannot prove final output if the last one omits it.
                if delta.is_none_or(|d| !d.contains_key("output_tokens"))
                    && let Some(current) = self.usage.as_mut().and_then(Value::as_object_mut)
                {
                    current.remove("output_tokens");
                }
                if let Some(reason) = event["delta"].get("stop_reason").filter(|v| !v.is_null()) {
                    let reason = reason
                        .as_str()
                        .filter(|r| !r.is_empty())
                        .ok_or(ProtocolError::Invalid("stop reason"))?;
                    if self.stop_reason.as_deref().is_some_and(|old| old != reason) {
                        return Err(ProtocolError::UnexpectedEvent);
                    }
                    self.stop_reason = Some(reason.to_owned());
                }
                return Ok(None);
            }
            (Endpoint::AnthropicMessages, "content_block_start")
                if event["content_block"]["type"] == "compaction" && self.id.is_some() =>
            {
                let index = event["index"]
                    .as_u64()
                    .ok_or(ProtocolError::UnexpectedEvent)?;
                if self.open_compaction.replace(index).is_some() {
                    return Err(ProtocolError::UnexpectedEvent);
                }
                self.saw_compaction = true;
                return Ok(None);
            }
            (Endpoint::AnthropicMessages, "content_block_stop")
                if self.open_compaction.is_some()
                    && event["index"].as_u64() == self.open_compaction =>
            {
                self.open_compaction = None;
                return Ok(None);
            }
            (Endpoint::AnthropicMessages, "message_stop") => {
                if self.id.is_none()
                    || self.stop_reason.is_none()
                    || self.open_compaction.is_some()
                    || self.stop_reason.as_deref() == Some("compaction") && !self.saw_compaction
                {
                    return Err(ProtocolError::UnexpectedEvent);
                }
                Terminal::Completed
            }
            (Endpoint::AnthropicMessages, _) if self.id.is_some() => return Ok(None),
            (Endpoint::OpenaiResponses, "response.created") => {
                if self.id.is_some() {
                    return Err(ProtocolError::UnexpectedEvent);
                }
                self.set_id(&event["response"])?;
                return Ok(None);
            }
            (
                Endpoint::OpenaiResponses,
                "response.completed"
                | "response.failed"
                | "response.incomplete"
                | "response.cancelled",
            ) => {
                let r = &event["response"];
                self.set_id(r)?;
                if r.get("status").and_then(Value::as_str) != kind.strip_prefix("response.") {
                    return Err(ProtocolError::UnexpectedEvent);
                }
                self.usage = r.get("usage").cloned();
                self.stop_reason = r["incomplete_details"]["reason"]
                    .as_str()
                    .map(str::to_owned);
                match kind {
                    "response.completed" => Terminal::Completed,
                    "response.failed" => Terminal::Failed,
                    "response.cancelled" => Terminal::Cancelled,
                    _ => Terminal::Incomplete,
                }
            }
            (
                Endpoint::OpenaiResponses,
                "response.output_item.added" | "response.output_item.done",
            ) if self.id.is_some()
                && matches!(
                    event["item"]["type"].as_str(),
                    Some("web_search_call" | "file_search_call")
                ) =>
            {
                let item = &event["item"];
                self.search_event(
                    item["id"].as_str(),
                    if item["type"] == "file_search_call" {
                        Some("file_search")
                    } else {
                        item["action"]["type"].as_str()
                    },
                )?;
                return Ok(None);
            }
            (
                Endpoint::OpenaiResponses,
                "response.web_search_call.in_progress"
                | "response.web_search_call.searching"
                | "response.web_search_call.completed",
            ) if self.id.is_some() => {
                self.search_event(event["item_id"].as_str(), None)?;
                return Ok(None);
            }
            (
                Endpoint::OpenaiResponses,
                "response.file_search_call.in_progress"
                | "response.file_search_call.searching"
                | "response.file_search_call.completed",
            ) if self.id.is_some() => {
                self.search_event(event["item_id"].as_str(), Some("file_search"))?;
                return Ok(None);
            }
            (Endpoint::OpenaiResponses, _) if self.id.is_some() => return Ok(None),
            _ => return Err(ProtocolError::UnexpectedEvent),
        };
        self.ended = true;
        let usage =
            UsageEvidence::for_model(self.endpoint, self.usage.as_ref(), self.model.as_deref())
                .and_then(|mut u| {
                    if self.invalid_iterations
                        || self.endpoint == Endpoint::AnthropicMessages
                            && !super::usage::iterations::terminal_totals(self.usage.as_ref())
                        || self.saw_compaction
                            && !u
                                .raw_usage
                                .as_ref()
                                .is_some_and(|v| v.contains_key("compaction_output_tokens"))
                    {
                        u.raw_usage = None;
                    }
                    if self.endpoint == Endpoint::OpenaiResponses {
                        u.responses_output(
                            &event["response"],
                            &self.search_items,
                            self.search_metered,
                        )?;
                    }
                    Ok(u)
                })
                .unwrap_or(UsageEvidence {
                    usage_format: self.endpoint.usage_format(),
                    raw_usage: None,
                });
        Ok(Some(Completion {
            terminal,
            response_id: self.id.clone(),
            stop_reason: self.stop_reason.clone(),
            usage,
        }))
    }

    fn search_event(
        &mut self,
        id: Option<&str>,
        action: Option<&str>,
    ) -> Result<(), ProtocolError> {
        let id = id
            .filter(|s| !s.is_empty() && s.len() <= 255)
            .ok_or(ProtocolError::Invalid("web search ID"))?;
        if self.search_items.len() >= 4096 && !self.search_items.contains_key(id) {
            return Err(ProtocolError::EventTooLarge);
        }
        if let Some(prior) = self.search_items.get(id)
            && (prior.as_deref() == Some("file_search")) != (action == Some("file_search"))
        {
            return Err(ProtocolError::Invalid("conflicting hosted search kind"));
        }
        let recorded = self.search_items.entry(id.to_owned()).or_default();
        if let Some(action) = action {
            if !matches!(
                action,
                "search" | "open_page" | "find_in_page" | "file_search"
            ) || recorded.as_deref().is_some_and(|v| v != action)
            {
                return Err(ProtocolError::UnexpectedEvent);
            }
            *recorded = Some(action.to_owned());
        }
        Ok(())
    }
    fn set_id(&mut self, body: &Value) -> Result<(), ProtocolError> {
        let id = body
            .get("id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty() && id.len() <= 255)
            .ok_or(ProtocolError::Invalid("response ID"))?;
        if self.id.as_deref().is_some_and(|old| old != id) {
            return Err(ProtocolError::UnexpectedEvent);
        }
        self.id = Some(id.to_owned());
        Ok(())
    }

    pub fn finish(&self) -> Result<(), ProtocolError> {
        if self.ended {
            Ok(())
        } else {
            Err(ProtocolError::Truncated)
        }
    }
}
