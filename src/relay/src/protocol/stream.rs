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
                                | "refusal"
                                | "model_context_window_exceeded"
                        )
                    ) =>
            {
                "succeeded"
            }
            Terminal::Incomplete if self.stop_reason.as_deref() == Some("max_output_tokens") => {
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
    usage: Option<Value>,
    stop_reason: Option<String>,
    ended: bool,
}

impl Observer {
    pub fn new(endpoint: Endpoint) -> Self {
        Self {
            endpoint,
            id: None,
            usage: None,
            stop_reason: None,
            ended: false,
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
            (Endpoint::AnthropicMessages, "ping") => return Ok(None),
            (Endpoint::AnthropicMessages, "message_start") => {
                if self.id.is_some() {
                    return Err(ProtocolError::UnexpectedEvent);
                }
                let msg = &event["message"];
                self.set_id(msg)?;
                UsageEvidence::extract(self.endpoint, msg.get("usage"))?;
                self.usage = msg.get("usage").cloned();
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
                UsageEvidence::extract(self.endpoint, event.get("usage"))?;
                let delta = event.get("usage").and_then(Value::as_object);
                // Only accepted observations get merged; partial evidence is never
                // padded with zero. The terminal cumulative output supersedes prior values.
                if let Some(delta) = delta {
                    if self.usage.as_ref().is_none_or(Value::is_null) {
                        self.usage = Some(serde_json::json!({}));
                    }
                    let current = self
                        .usage
                        .as_mut()
                        .and_then(Value::as_object_mut)
                        .ok_or(ProtocolError::Invalid("message usage"))?;
                    for (key, value) in delta {
                        if key != "output_tokens"
                            && current
                                .get(key)
                                .is_some_and(|old| !old.is_null() && old != value)
                        {
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
            (Endpoint::AnthropicMessages, "message_stop") => {
                if self.id.is_none() || self.stop_reason.is_none() {
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
            (Endpoint::OpenaiResponses, _) if self.id.is_some() => return Ok(None),
            _ => return Err(ProtocolError::UnexpectedEvent),
        };
        self.ended = true;
        Ok(Some(Completion {
            terminal,
            response_id: self.id.clone(),
            stop_reason: self.stop_reason.clone(),
            usage: UsageEvidence::extract(self.endpoint, self.usage.as_ref()).unwrap_or(
                UsageEvidence {
                    usage_format: self.endpoint.usage_format(),
                    raw_usage: None,
                },
            ),
        }))
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
