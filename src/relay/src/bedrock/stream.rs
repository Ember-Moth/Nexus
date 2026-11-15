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
    Pending,
    Text(super::citations::Delta),
    Thinking(String),
    Redacted(Vec<u8>),
    Tool(String),
}
pub struct Conversation {
    id: String,
    model: String,
    pub guardrail: bool,
    buffer_limit: usize,
    buffer_bytes: usize,
    started: bool,
    reason: Option<&'static str>,
    stop_sequence: Option<String>,
    next: u64,
    active: HashMap<u64, Block>,
    tool_ids: HashSet<String>,
}
impl Conversation {
    pub fn new(id: String, model: String, buffer_limit: usize) -> Self {
        Self {
            id,
            model,
            guardrail: false,
            buffer_limit,
            buffer_bytes: 0,
            started: false,
            reason: None,
            stop_sequence: None,
            next: 0,
            active: HashMap::new(),
            tool_ids: HashSet::new(),
        }
    }
    fn begin(&mut self, index: u64, block: Block) -> Result<()> {
        if index != self.next
            || self.active.len() >= 256
            || index >= 4096
            || self
                .active
                .values()
                .any(|b| matches!(b, Block::Pending | Block::Redacted(_)))
        {
            return Err(ProtocolError::UnexpectedEvent);
        }
        self.next += 1;
        self.active.insert(index, block);
        Ok(())
    }
    fn start_delta(
        &mut self,
        index: u64,
        block: Block,
        content: impl FnOnce() -> Option<Value>,
        events: &mut Vec<Value>,
    ) -> Result<()> {
        match self.active.get(&index) {
            Some(Block::Pending) => {
                self.active.insert(index, block);
            }
            Some(_) => return Ok(()),
            None => self.begin(index, block)?,
        }
        if let Some(content) = content() {
            events
                .push(json!({"type":"content_block_start","index":index,"content_block":content}));
        }
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
                    self.begin(n, Block::Pending)?;
                    return Ok((events, None));
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
                    self.start_delta(
                        n,
                        Block::Text(Default::default()),
                        || Some(json!({"type":"text","text":""})),
                        &mut events,
                    )?;
                    if !matches!(self.active.get(&n), Some(Block::Text(_))) {
                        return Err(ProtocolError::UnexpectedEvent);
                    }
                    json!({"type":"text_delta","text":text})
                } else if let Some(c) = delta.get("citation") {
                    self.start_delta(
                        n,
                        Block::Text(Default::default()),
                        || Some(json!({"type":"text","text":""})),
                        &mut events,
                    )?;
                    let Some(Block::Text(pending)) = self.active.get_mut(&n) else {
                        return Err(ProtocolError::UnexpectedEvent);
                    };
                    if let Some(ready) =
                        pending.push(c, &mut self.buffer_bytes, self.buffer_limit)?
                    {
                        events.push(json!({"type":"content_block_delta","index":n,"delta":{"type":"citations_delta","citation":ready}}));
                    }
                    return Ok((events, None));
                } else if let Some(args) = delta["toolUse"]["input"].as_str() {
                    let Some(Block::Tool(buffer)) = self.active.get_mut(&n) else {
                        return Err(ProtocolError::UnexpectedEvent);
                    };
                    if args.len() > self.buffer_limit - self.buffer_bytes {
                        return Err(ProtocolError::EventTooLarge);
                    }
                    buffer.push_str(args);
                    self.buffer_bytes += args.len();
                    json!({"type":"input_json_delta","partial_json":args})
                } else if let Some(r) = delta.get("reasoningContent") {
                    if r.as_object().is_none_or(|o| o.len() != 1) {
                        return Err(ProtocolError::Invalid("reasoning delta"));
                    }
                    if let Some(data) = r.get("redactedContent").and_then(Value::as_str) {
                        use base64::{Engine, engine::general_purpose::STANDARD};
                        self.start_delta(n, Block::Redacted(Vec::new()), || None, &mut events)?;
                        let Some(Block::Redacted(buffer)) = self.active.get_mut(&n) else {
                            return Err(ProtocolError::UnexpectedEvent);
                        };
                        let bytes = STANDARD
                            .decode(data)
                            .map_err(|_| ProtocolError::Invalid("redacted reasoning bytes"))?;
                        if bytes.len() > self.buffer_limit - self.buffer_bytes {
                            return Err(ProtocolError::EventTooLarge);
                        }
                        buffer.extend_from_slice(&bytes);
                        self.buffer_bytes += bytes.len();
                        return Ok((events, None));
                    }
                    self.start_delta(
                        n,
                        Block::Thinking(String::new()),
                        || Some(json!({"type":"thinking","thinking":""})),
                        &mut events,
                    )?;
                    let Some(Block::Thinking(signature)) = self.active.get_mut(&n) else {
                        return Err(ProtocolError::UnexpectedEvent);
                    };
                    if let Some(t) = r.get("text").and_then(Value::as_str) {
                        if !signature.is_empty() {
                            return Err(ProtocolError::UnexpectedEvent);
                        }
                        json!({"type":"thinking_delta","thinking":t})
                    } else {
                        let piece = convert::text(r, "signature")?;
                        if piece.len() > self.buffer_limit - self.buffer_bytes {
                            return Err(ProtocolError::EventTooLarge);
                        }
                        signature.push_str(piece);
                        self.buffer_bytes += piece.len();
                        return Ok((events, None));
                    }
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
                        self.buffer_bytes -= buffer.len();
                        if !buffer.is_empty()
                            && !serde_json::from_str::<Value>(&buffer).is_ok_and(|v| v.is_object())
                        {
                            return Err(ProtocolError::Invalid("Bedrock tool arguments"));
                        }
                    }
                    Block::Text(mut pending) => {
                        if let Some(ready) = pending.finish(&mut self.buffer_bytes)? {
                            events.push(json!({"type":"content_block_delta","index":n,"delta":{"type":"citations_delta","citation":ready}}));
                        }
                    }
                    Block::Thinking(signature) => {
                        self.buffer_bytes -= signature.len();
                        if !signature.is_empty() {
                            events.push(json!({"type":"content_block_delta","index":n,"delta":{"type":"signature_delta","signature":signature}}));
                        }
                    }
                    Block::Redacted(bytes) => {
                        use base64::{Engine,engine::general_purpose::STANDARD};
                        if bytes.is_empty(){return Err(ProtocolError::Invalid("empty redacted reasoning"));}
                        self.buffer_bytes-=bytes.len();
                        events.push(json!({"type":"content_block_start","index":n,"content_block":{"type":"redacted_thinking","data":STANDARD.encode(bytes)}}));
                    }
                    Block::Pending => events.push(json!({"type":"content_block_start","index":n,"content_block":{"type":"text","text":""}})),
                }
                events.push(json!({"type":"content_block_stop","index":n}));
            }
            "messageStop" if self.active.is_empty() => {
                let reason = wire::reason(convert::text(v, "stopReason")?)?;
                self.stop_sequence = wire::stop_sequence(reason, v)?.map(str::to_owned);
                self.reason = Some(reason);
            }
            "metadata" => {
                let reason = self.reason.ok_or(ProtocolError::UnexpectedEvent)?;
                let usage = super::guardrail::evidence(v, self.guardrail);
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
            json!({"type":"message_delta","delta":{"stop_reason":reason,"stop_sequence":self.stop_sequence},"usage":wire::visible(usage)}),
            json!({"type":"message_stop"}),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn signed_and_redacted_fragments_replay_the_original_bytes() {
        let mut stream = Conversation::new("r".into(), "m".into(), 64);
        stream
            .event("messageStart", &json!({"role":"assistant"}), false)
            .unwrap();
        stream
            .event(
                "contentBlockStart",
                &json!({"contentBlockIndex":0,"start":{}}),
                false,
            )
            .unwrap();
        let mut blocks = Vec::new();
        for (index, parts) in [
            (
                0,
                vec![
                    json!({"text":"summary"}),
                    json!({"signature":"ab"}),
                    json!({"signature":"cd"}),
                ],
            ),
            (
                1,
                vec![
                    json!({"redactedContent":"AQ=="}),
                    json!({"redactedContent":"AgM="}),
                ],
            ),
        ] {
            let mut message = Value::Null;
            for piece in parts {
                let (events, _) = stream
                    .event(
                        "contentBlockDelta",
                        &json!({"contentBlockIndex":index,"delta":{"reasoningContent":piece}}),
                        false,
                    )
                    .unwrap();
                for e in events {
                    if e["type"] == "content_block_start" {
                        message = e["content_block"].clone();
                    } else {
                        assert_eq!(e["delta"]["type"], "thinking_delta");
                        message["thinking"] = e["delta"]["thinking"].clone();
                    }
                }
            }
            let (events, _) = stream
                .event(
                    "contentBlockStop",
                    &json!({"contentBlockIndex":index}),
                    false,
                )
                .unwrap();
            assert_eq!(events.last().unwrap()["type"], "content_block_stop");
            if index == 0 {
                message["signature"] = events[0]["delta"]["signature"].clone();
            } else {
                message = events[0]["content_block"].clone();
            }
            blocks.push(message);
        }
        assert_eq!(
            blocks[0],
            json!({"type":"thinking","thinking":"summary","signature":"abcd"})
        );
        assert_eq!(blocks[1], json!({"type":"redacted_thinking","data":"AQID"}));
        assert_eq!(stream.buffer_bytes, 0);
        let req=super::super::prepare_messages(&json!({"messages":[{"role":"user","content":"a"},{"role":"assistant","content":blocks},{"role":"user","content":"again"}]})).unwrap();
        assert_eq!(
            req["messages"][1]["content"],
            json!([{"reasoningContent":{"reasoningText":{"text":"summary","signature":"abcd"}}},{"reasoningContent":{"redactedContent":"AQID"}}])
        );
    }

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
    fn aws_stop_sequence_survives_until_terminal_metadata() {
        let mut stream = Conversation::new("r".into(), "alias".into(), 1024);
        stream
            .event("messageStart", &json!({"role":"assistant"}), false)
            .unwrap();
        let (pending,done)=stream.event("messageStop",&json!({"stopReason":"stop_sequence","additionalModelResponseFields":{"stop_sequence":"END"}}),false).unwrap();
        assert!(pending.is_empty() && done.is_none());
        let (terminal, done) = stream
            .event(
                "metadata",
                &json!({"usage":{"inputTokens":1,"outputTokens":2,"totalTokens":3}}),
                false,
            )
            .unwrap();
        assert_eq!(terminal[0]["delta"]["stop_sequence"], "END");
        assert_eq!(done.unwrap().stop_reason.as_deref(), Some("stop_sequence"));
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
