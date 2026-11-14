//! Each converter consumes the original provider event before the caller writes any translated frames.
mod messages;
mod responses;

use super::Result;
use super::response::{error, usage};
use crate::protocol::{Endpoint, ProtocolError, stream::Completion};
use serde_json::{Value, json};

pub struct Stream {
    client: Endpoint,
    model: String,
    limit: usize,
    bytes: usize,
    pub(super) rid: Option<String>,
    sequence: u64,
    created: i64,
    messages: messages::Messages,
    responses: responses::Responses,
}

impl Stream {
    pub fn new(client: Endpoint, model: &str, limit: usize) -> Self {
        Self {
            client,
            model: model.into(),
            limit,
            bytes: 0,
            rid: None,
            sequence: 0,
            created: chrono::Utc::now().timestamp(),
            messages: Default::default(),
            responses: Default::default(),
        }
    }

    pub fn event(&mut self, event: &Value, done: Option<&Completion>) -> Result<Vec<Value>> {
        let kind = super::text(event, "type")?;
        let mut events = if kind == "error" || kind == "response.failed" {
            let err = if kind == "response.failed" {
                &event["response"]
            } else {
                event
            };
            let detail = &err["error"];
            let status = match detail["code"].as_str().or(detail["type"].as_str()) {
                Some("rate_limit_exceeded" | "rate_limit_error") => 429,
                Some("overloaded_error") => 529,
                Some("invalid_request_error") => 400,
                _ => 502,
            };
            let mapped = error(self.client, err, status);
            if self.client == Endpoint::AnthropicMessages {
                vec![mapped]
            } else {
                vec![
                    json!({"type":"error","code":mapped["error"]["code"],"message":mapped["error"]["message"],"param":null}),
                ]
            }
        } else {
            match self.client {
                Endpoint::AnthropicMessages => self.responses_event(kind, event, done)?,
                Endpoint::OpenaiResponses => self.messages_event(kind, event, done)?,
            }
        };
        if self.client == Endpoint::OpenaiResponses {
            for e in &mut events {
                e["sequence_number"] = self.sequence.into();
                self.sequence += 1;
            }
        }
        Ok(events)
    }
    fn account(&mut self, extra: usize) -> Result<()> {
        if extra > self.limit - self.bytes {
            return Err(ProtocolError::EventTooLarge);
        }
        self.bytes += extra;
        Ok(())
    }
}

fn position(event: &Value, field: &'static str) -> Result<usize> {
    event[field]
        .as_u64()
        .and_then(|i| usize::try_from(i).ok())
        .filter(|i| *i < 4096)
        .ok_or(ProtocolError::Invalid(field))
}
fn unexpected<T>() -> Result<T> {
    Err(ProtocolError::UnexpectedEvent)
}
