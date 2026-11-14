use super::{Result, wire};
use crate::protocol::{
    ProtocolError, convert,
    stream::{Completion, Terminal},
};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};

/// Buffer at most one declared frame. Smithy's reader verifies both CRCs and header types.
#[derive(Default)]
pub struct Decoder {
    buffer: Vec<u8>,
}
impl Decoder {
    pub fn next(
        &mut self,
        input: &mut &[u8],
        limit: usize,
    ) -> Result<Option<(String, Value, bool)>> {
        if self.buffer.len() < 4 {
            let n = (4 - self.buffer.len()).min(input.len());
            self.buffer.extend_from_slice(&input[..n]);
            *input = &input[n..];
            if self.buffer.len() < 4 {
                return Ok(None);
            }
        }
        let size = u32::from_be_bytes(self.buffer[..4].try_into().unwrap()) as usize;
        if size < 16 {
            return Err(ProtocolError::Invalid("EventStream length"));
        }
        if size > limit {
            return Err(ProtocolError::EventTooLarge);
        }
        let n = (size - self.buffer.len()).min(input.len());
        self.buffer.extend_from_slice(&input[..n]);
        *input = &input[n..];
        if size != self.buffer.len() {
            return Ok(None);
        }
        let headers = u32::from_be_bytes(self.buffer[4..8].try_into().unwrap()) as usize;
        if headers > size - 16 || headers > 128 * 1024 {
            return Err(ProtocolError::Invalid("EventStream headers"));
        }
        let msg = aws_smithy_eventstream::frame::read_message_from(&self.buffer[..])
            .map_err(|_| ProtocolError::Invalid("EventStream frame"))?;
        self.buffer.clear();
        let header = |key: &str| -> Result<Option<&str>> {
            let mut matches = msg.headers().iter().filter(|h| h.name().as_str() == key);
            let value = matches
                .next()
                .map(|h| {
                    h.value()
                        .as_string()
                        .map(|v| v.as_str())
                        .map_err(|_| ProtocolError::Invalid("EventStream header"))
                })
                .transpose()?;
            if matches.next().is_some() {
                return Err(ProtocolError::Invalid("duplicate EventStream header"));
            }
            Ok(value)
        };
        let message_type = header(":message-type")?;
        if message_type == Some("error") {
            return Ok(Some((
                header(":error-code")?.unwrap_or("upstream_error").into(),
                json!({"message":"Bedrock stream failed"}),
                true,
            )));
        }
        let exception = message_type == Some("exception");
        if !exception && message_type != Some("event") {
            return Err(ProtocolError::Invalid("EventStream message type"));
        }
        let kind = header(if exception {
            ":exception-type"
        } else {
            ":event-type"
        })?
        .ok_or(ProtocolError::Invalid("EventStream event type"))?;
        if header(":content-type")?.is_some_and(|v| v != "application/json") {
            return Err(ProtocolError::Unsupported("EventStream payload encoding"));
        }
        let payload = serde_json::from_slice(msg.payload())
            .map_err(|_| ProtocolError::Invalid("EventStream JSON"))?;
        Ok(Some((kind.into(), payload, exception)))
    }
}

enum Block {
    Text,
    Tool(String),
}
pub struct Conversation {
    id: String,
    model: String,
    tool_limit: usize,
    tool_bytes: usize,
    started: bool,
    reason: Option<&'static str>,
    next: u64,
    active: HashMap<u64, Block>,
    tool_ids: HashSet<String>,
}
impl Conversation {
    pub fn new(id: String, model: String, tool_limit: usize) -> Self {
        Self {
            id,
            model,
            tool_limit,
            tool_bytes: 0,
            started: false,
            reason: None,
            next: 0,
            active: HashMap::new(),
            tool_ids: HashSet::new(),
        }
    }
    fn begin(&mut self, index: u64, block: Block) -> Result<()> {
        if index != self.next || self.active.len() >= 256 || index >= 4096 {
            return Err(ProtocolError::UnexpectedEvent);
        }
        self.next += 1;
        self.active.insert(index, block);
        Ok(())
    }
    pub fn event(
        &mut self,
        kind: &str,
        v: &Value,
        exception: bool,
    ) -> Result<(Vec<Value>, Option<Completion>)> {
        if exception {
            let err = match kind {
                "throttlingException" => "rate_limit_error",
                "serviceUnavailableException" => "overloaded_error",
                "validationException" => "invalid_request_error",
                _ => "api_error",
            };
            let event = json!({"type":"error","error":{"type":err,"message":v["message"].as_str().unwrap_or("Bedrock stream failed")}});
            return Ok((
                vec![event],
                Some(Completion {
                    terminal: Terminal::Failed,
                    response_id: Some(self.id.clone()),
                    stop_reason: None,
                    usage: wire::evidence(None),
                }),
            ));
        }
        let index = || {
            v["contentBlockIndex"]
                .as_u64()
                .ok_or(ProtocolError::Invalid("Bedrock block index"))
        };
        if kind != "messageStart" && !self.started || self.reason.is_some() && kind != "metadata" {
            return Err(ProtocolError::UnexpectedEvent);
        }
        let mut events = Vec::new();
        match kind {
            "messageStart" if !self.started && v["role"] == "assistant" => {
                self.started = true;
                events.push(json!({"type":"message_start","message":{"type":"message","id":self.id,"model":self.model,"role":"assistant","content":[],"stop_reason":null,"stop_sequence":null,"usage":wire::visible(&wire::evidence(None))}}));
            }
            "contentBlockStart" => {
                let n = index()?;
                let start = v["start"]
                    .as_object()
                    .ok_or(ProtocolError::Invalid("Bedrock block start"))?;
                let content = if let Some(t) = start.get("toolUse").filter(|_| start.len() == 1) {
                    convert::fields(t, &["toolUseId", "name"])?;
                    let id = convert::id(t, "toolUseId")?;
                    if !self.tool_ids.insert(id.into()) {
                        return Err(ProtocolError::Invalid("duplicate tool ID"));
                    }
                    self.begin(n, Block::Tool(String::new()))?;
                    json!({"type":"tool_use","name":convert::id(t,"name")?,"id":id,"input":{}})
                } else if start.is_empty() {
                    self.begin(n, Block::Text)?;
                    json!({"type":"text","text":""})
                } else {
                    return Err(ProtocolError::Unsupported("Bedrock block start"));
                };
                events
                    .push(json!({"type":"content_block_start","index":n,"content_block":content}));
            }
            "contentBlockDelta" => {
                let n = index()?;
                let delta = &v["delta"];
                if delta.as_object().is_none_or(|o| o.len() != 1) {
                    return Err(ProtocolError::Invalid("Bedrock delta union"));
                }
                let translated = if let Some(text) = delta["text"].as_str() {
                    if !self.active.contains_key(&n) {
                        self.begin(n, Block::Text)?;
                        events.push(json!({"type":"content_block_start","index":n,"content_block":{"type":"text","text":""}}));
                    }
                    if !matches!(self.active.get(&n), Some(Block::Text)) {
                        return Err(ProtocolError::UnexpectedEvent);
                    }
                    json!({"type":"text_delta","text":text})
                } else if let Some(args) = delta["toolUse"]["input"].as_str() {
                    let Some(Block::Tool(buffer)) = self.active.get_mut(&n) else {
                        return Err(ProtocolError::UnexpectedEvent);
                    };
                    if args.len() > self.tool_limit - self.tool_bytes {
                        return Err(ProtocolError::EventTooLarge);
                    }
                    buffer.push_str(args);
                    self.tool_bytes += args.len();
                    json!({"type":"input_json_delta","partial_json":args})
                } else {
                    return Err(ProtocolError::Unsupported("Bedrock content delta"));
                };
                events.push(json!({"type":"content_block_delta","index":n,"delta":translated}));
            }
            "contentBlockStop" => {
                let n = index()?;
                match self
                    .active
                    .remove(&n)
                    .ok_or(ProtocolError::UnexpectedEvent)?
                {
                    Block::Tool(buffer) => {
                        self.tool_bytes -= buffer.len();
                        if !buffer.is_empty()
                            && !serde_json::from_str::<Value>(&buffer).is_ok_and(|v| v.is_object())
                        {
                            return Err(ProtocolError::Invalid("Bedrock tool arguments"));
                        }
                    }
                    Block::Text => {}
                }
                events.push(json!({"type":"content_block_stop","index":n}));
            }
            "messageStop" if self.active.is_empty() => {
                self.reason = Some(wire::reason(convert::text(v, "stopReason")?)?);
            }
            "metadata" => {
                let reason = self.reason.ok_or(ProtocolError::UnexpectedEvent)?;
                let usage = wire::evidence(v.get("usage"));
                return Ok((
                    self.terminal(reason, &usage),
                    Some(Completion {
                        terminal: Terminal::Completed,
                        response_id: Some(self.id.clone()),
                        stop_reason: Some(reason.into()),
                        usage,
                    }),
                ));
            }
            _ => return Err(ProtocolError::UnexpectedEvent),
        }
        Ok((events, None))
    }
    fn terminal(&self, reason: &str, usage: &crate::protocol::usage::UsageEvidence) -> Vec<Value> {
        vec![
            json!({"type":"message_delta","delta":{"stop_reason":reason,"stop_sequence":null},"usage":wire::visible(usage)}),
            json!({"type":"message_stop"}),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn length_is_bounded_before_body_allocation() {
        let mut d = Decoder::default();
        let mut first = &[0u8, 0][..];
        assert!(d.next(&mut first, 1024).unwrap().is_none());
        let mut last = &[8, 0][..];
        assert_eq!(
            d.next(&mut last, 1024).unwrap_err(),
            ProtocolError::EventTooLarge
        );
    }
    #[test]
    fn stop_is_not_completion_and_metadata_cannot_precede_it() {
        let mut s = Conversation::new("request".into(), "alias".into(), 1024);
        assert!(s.event("metadata", &json!({}), false).is_err());
        s.event("messageStart", &json!({"role":"assistant"}), false)
            .unwrap();
        assert!(
            s.event("messageStop", &json!({"stopReason":"end_turn"}), false)
                .unwrap()
                .1
                .is_none()
        );
        let (_, done) = s
            .event("metadata", &json!({"metrics":{"latencyMs":1}}), false)
            .unwrap();
        assert!(done.unwrap().usage.raw_usage.is_none());
    }
}
