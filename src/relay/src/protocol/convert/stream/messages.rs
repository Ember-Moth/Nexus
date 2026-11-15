use super::{Result, Stream, position, unexpected, usage};
use crate::protocol::{
    Endpoint, ProtocolError,
    convert::{
        id,
        response::{item_id, response, response_status, validate_message_block},
        text,
    },
    stream::Completion,
};
use serde_json::{Value, json};
use std::collections::HashSet;

#[derive(Default)]
pub(super) struct Messages {
    blocks: Vec<Block>,
    calls: HashSet<String>,
}
struct Block {
    item: Value,
    buffer: String,
    closed: bool,
    private: Option<super::private_message::PrivateMessage>,
    citations: Vec<Value>,
}

impl Stream {
    pub(super) fn messages_event(
        &mut self,
        kind: &str,
        event: &Value,
        done: Option<&Completion>,
    ) -> Result<Vec<Value>> {
        if kind == "ping" {
            return Ok(vec![]);
        }
        if kind == "message_start" {
            if self.rid.is_some() {
                return unexpected();
            }
            let msg = &event["message"];
            let rid = id(msg, "id")?;
            if msg
                .get("content")
                .is_some_and(|v| v.as_array().is_none_or(|a| !a.is_empty()))
            {
                return unexpected();
            }
            self.rid = Some(rid.into());
            let head = response(rid, &self.model, self.created);
            return Ok(vec![
                json!({"type":"response.created","response":head}),
                json!({"type":"response.in_progress","response":head}),
            ]);
        }
        let rid = self.rid.as_deref().ok_or(ProtocolError::UnexpectedEvent)?;
        match kind {
            "content_block_start" => {
                let n = position(event, "index")?;
                if n != self.messages.blocks.len() {
                    return unexpected();
                }
                let content = &event["content_block"];
                if self.history.is_some()
                    && matches!(
                        content["type"].as_str(),
                        Some("thinking" | "redacted_thinking" | "compaction")
                    )
                {
                    let (private, events) = super::private_message::PrivateMessage::start(
                        content,
                        rid,
                        n,
                        self.history.as_ref().expect("history writer").hide_summary,
                    )?;
                    self.account(content.to_string().len())?;
                    self.messages.blocks.push(Block {
                        item: Value::Null,
                        buffer: String::new(),
                        closed: false,
                        private: Some(private),
                        citations: Vec::new(),
                    });
                    return Ok(events);
                }
                let citations = if self.history.is_some() && content["type"] == "text" {
                    super::super::fields(content, &["type", "text", "citations"])?;
                    content
                        .get("citations")
                        .filter(|v| !v.is_null())
                        .map(|v| super::super::array(v, "citations").cloned())
                        .transpose()?
                        .unwrap_or_default()
                } else {
                    validate_message_block(content)?;
                    Vec::new()
                };

                let tool = content["type"] == "tool_use";
                let item_id = item_id(rid, n, tool);
                let mut events = Vec::new();
                let (item, initial) = match content["type"].as_str() {
                    Some("text") => (
                        json!({"id":item_id,"type":"message","status":"in_progress","role":"assistant","content":[]}),
                        text(content, "text")?.to_owned(),
                    ),
                    Some("tool_use") if content["input"].is_object() => {
                        let call = id(content, "id")?;
                        if !self.messages.calls.insert(call.to_owned()) {
                            return unexpected();
                        }
                        let args = if content["input"].as_object().unwrap().is_empty() {
                            String::new()
                        } else {
                            content["input"].to_string()
                        };
                        (
                            json!({"id":item_id,"type":"function_call","status":"in_progress","call_id":call,"name":id(content,"name")?,"arguments":""}),
                            args,
                        )
                    }
                    _ => return Err(ProtocolError::Unsupported("Messages stream block")),
                };
                self.account(
                    initial.len()
                        + item.to_string().len()
                        + citations.iter().map(|v| v.to_string().len()).sum::<usize>(),
                )?;
                events.push(
                    json!({"type":"response.output_item.added","output_index":n,"item":item}),
                );
                if !tool {
                    events.push(json!({"type":"response.content_part.added","output_index":n,"content_index":0,"item_id":item_id,"part":{"type":"output_text","text":"","annotations":[]}}));
                }
                if !initial.is_empty() {
                    events.push(delta_event(n, &item, &initial));
                }
                self.messages.blocks.push(Block {
                    item,
                    buffer: initial,
                    closed: false,
                    private: None,
                    citations,
                });
                Ok(events)
            }
            "content_block_delta" => {
                let n = position(event, "index")?;
                let delta = &event["delta"];
                if self
                    .messages
                    .blocks
                    .get(n)
                    .is_some_and(|b| b.private.is_some())
                {
                    self.account(delta.to_string().len())?;
                    return self.messages.blocks[n]
                        .private
                        .as_mut()
                        .expect("private block")
                        .delta(delta);
                }
                if self.history.is_some() && delta["type"] == "citations_delta" {
                    self.account(delta["citation"].to_string().len())?;
                    let b = self
                        .messages
                        .blocks
                        .get_mut(n)
                        .ok_or(ProtocolError::UnexpectedEvent)?;
                    if b.closed || b.item["type"] != "message" {
                        return unexpected();
                    }
                    b.citations.push(delta["citation"].clone());
                    return Ok(vec![]);
                }
                let (kind, s) = match delta["type"].as_str() {
                    Some("input_json_delta") => ("function_call", text(delta, "partial_json")?),
                    Some("text_delta") => ("message", text(delta, "text")?),
                    _ => return Err(ProtocolError::Unsupported("Messages stream delta")),
                };
                self.account(s.len())?;
                let b = self
                    .messages
                    .blocks
                    .get_mut(n)
                    .ok_or(ProtocolError::UnexpectedEvent)?;
                if b.closed || b.item["type"] != kind {
                    return unexpected();
                }
                b.buffer.push_str(s);
                Ok(vec![delta_event(n, &b.item, s)])
            }
            "content_block_stop" => {
                let n = position(event, "index")?;
                let b = self
                    .messages
                    .blocks
                    .get_mut(n)
                    .ok_or(ProtocolError::UnexpectedEvent)?;
                if b.closed {
                    return unexpected();
                }
                b.closed = true;
                if let Some(private) = b.private.take() {
                    let (item, events) = private.finish(
                        self.history
                            .as_ref()
                            .ok_or(ProtocolError::UnexpectedEvent)?,
                        rid,
                    )?;
                    b.item = item;
                    let size = b.item.to_string().len();
                    self.account(size)?;
                    return Ok(events);
                }
                let mut events = Vec::new();
                if b.item["type"] == "function_call" {
                    let args = if b.buffer.is_empty() {
                        "{}".to_owned()
                    } else {
                        std::mem::take(&mut b.buffer)
                    };
                    if !serde_json::from_str::<Value>(&args).is_ok_and(|v| v.is_object()) {
                        return Err(ProtocolError::Invalid("tool arguments"));
                    }
                    b.item["arguments"] = args.into();
                    events.push(json!({"type":"response.function_call_arguments.done","item_id":b.item["id"],"output_index":n,"name":b.item["name"],"arguments":b.item["arguments"]}));
                } else {
                    let value = std::mem::take(&mut b.buffer);
                    let part = if b.citations.is_empty() {
                        json!({"type":"output_text","text":value,"annotations":[]})
                    } else {
                        let original = json!({"type":"text","text":value,"citations":std::mem::take(&mut b.citations)});
                        b.item = self
                            .history
                            .as_ref()
                            .ok_or(ProtocolError::UnexpectedEvent)?
                            .project(&original, rid, n)?;
                        for (index, a) in b.item["content"][0]["annotations"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .enumerate()
                        {
                            events.push(json!({"type":"response.output_text.annotation.added","output_index":n,"content_index":0,
                                "item_id":b.item["id"],"annotation_index":index,"annotation":a}));
                        }
                        b.item["content"][0].clone()
                    };
                    events.push(json!({"type":"response.output_text.done","output_index":n,"content_index":0,"item_id":b.item["id"],"text":part["text"],"logprobs":[]}));
                    events.push(json!({"type":"response.content_part.done","output_index":n,"content_index":0,"item_id":b.item["id"],"part":part}));
                    b.item["content"] = json!([part]);
                }
                b.item["status"] = "completed".into();
                events.push(
                    json!({"type":"response.output_item.done","output_index":n,"item":b.item}),
                );
                if b.item.get("nexus_history").is_some() {
                    let size = b.item.to_string().len();
                    self.account(size)?;
                }
                Ok(events)
            }
            "message_delta" => Ok(vec![]),
            "message_stop" => {
                let done = done.ok_or(ProtocolError::UnexpectedEvent)?;
                if self.messages.blocks.iter().any(|b| !b.closed) {
                    return unexpected();
                }
                let (status, details) = response_status(
                    done.stop_reason.as_deref().unwrap_or(""),
                    !self.messages.calls.is_empty(),
                )?;
                let mut result = response(rid, &self.model, self.created);
                result["status"] = status.into();
                result["incomplete_details"] = details;
                result["usage"] = usage(Endpoint::OpenaiResponses, &done.usage);
                result["output"] = self
                    .messages
                    .blocks
                    .drain(..)
                    .map(|b| b.item)
                    .collect::<Vec<_>>()
                    .into();
                Ok(vec![
                    json!({"type":format!("response.{status}"),"response":result}),
                ])
            }
            _ => unexpected(),
        }
    }
}

fn delta_event(index: usize, item: &Value, s: &str) -> Value {
    if item["type"] == "function_call" {
        json!({"type":"response.function_call_arguments.delta","item_id":item["id"],"output_index":index,"delta":s})
    } else {
        json!({"type":"response.output_text.delta","item_id":item["id"],"output_index":index,"content_index":0,"delta":s,"logprobs":[]})
    }
}
