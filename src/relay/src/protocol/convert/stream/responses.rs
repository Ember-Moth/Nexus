use super::{Result, Stream, position, unexpected, usage};
use crate::protocol::{
    Endpoint, ProtocolError,
    convert::{
        array, id,
        response::{message, message_stop, messages_wire_usage, valid_output_text},
        text,
    },
    stream::Completion,
};
use serde_json::{Value, json};
use std::collections::HashSet;

#[derive(Default)]
pub(super) struct Responses {
    items: Vec<Item>,
    ids: HashSet<String>,
    calls: HashSet<String>,
    item: usize,
    part: usize,
    index: usize,
}
struct Item {
    id: String,
    tool: Option<(String, String)>,
    parts: Vec<Part>,
    done: bool,
}
#[derive(Default)]
struct Part {
    data: String,
    sent: usize,
    started: bool,
    done: bool,
}

impl Stream {
    pub(super) fn responses_event(
        &mut self,
        kind: &str,
        event: &Value,
        done: Option<&Completion>,
    ) -> Result<Vec<Value>> {
        if kind == "response.created" {
            if self.rid.is_some() {
                return unexpected();
            }
            let rid = id(&event["response"], "id")?.to_string();
            let mut head = message(&rid, &self.model);
            head["usage"] = messages_wire_usage(Value::Null);
            self.rid = Some(rid);
            return Ok(vec![json!({"type":"message_start","message":head})]);
        }
        if self.rid.is_none() {
            return unexpected();
        }
        let terminal = matches!(kind, "response.completed" | "response.incomplete");
        match kind {
            "response.in_progress" | "response.queued" => return Ok(vec![]),
            "response.output_item.added" => {
                let i = position(event, "output_index")?;
                self.new_item(i, &event["item"])?;
            }
            "response.content_part.added" => {
                let (i, p) = self.indices(event, true)?;
                let part = &event["part"];
                valid_output_text(part)?;
                let initial = text(part, "text")?;
                self.account(initial.len() + 1)?;
                let item = &mut self.responses.items[i];
                if item.tool.is_some() || item.parts.len() != p {
                    return unexpected();
                }
                item.parts.push(Part {
                    data: initial.into(),
                    ..Default::default()
                });
            }
            "response.output_text.delta" | "response.function_call_arguments.delta" => {
                let tool = kind == "response.function_call_arguments.delta";
                let (i, p) = self.indices(event, !tool)?;
                let delta = text(event, "delta")?;
                self.account(delta.len())?;
                let item = &mut self.responses.items[i];
                if item.tool.is_some() != tool {
                    return unexpected();
                }
                let part = item
                    .parts
                    .get_mut(p)
                    .ok_or(ProtocolError::UnexpectedEvent)?;
                if part.done {
                    return unexpected();
                }
                part.data.push_str(delta);
            }
            "response.output_text.done"
            | "response.function_call_arguments.done"
            | "response.content_part.done" => {
                let tool = kind == "response.function_call_arguments.done";
                let (i, p) = self.indices(event, !tool)?;
                if self.responses.items[i].tool.is_some() != tool {
                    return unexpected();
                }
                if tool
                    && let Some(name) = event.get("name")
                    && !self.responses.items[i]
                        .tool
                        .as_ref()
                        .is_some_and(|(_, bound)| name == bound.as_str())
                {
                    return Err(ProtocolError::Invalid("function name changed"));
                }
                let full = match kind {
                    "response.function_call_arguments.done" => text(event, "arguments")?,
                    "response.content_part.done" => {
                        valid_output_text(&event["part"])?;
                        text(&event["part"], "text")?
                    }
                    _ => text(event, "text")?,
                };
                self.seal_part(i, p, full)?;
            }
            "response.output_item.done" => {
                let i = position(event, "output_index")?;
                self.seal_item(i, &event["item"])?;
            }
            "response.completed" | "response.incomplete" => {
                let full = &event["response"];
                let items = array(&full["output"], "terminal output")?;
                if items.len() < self.responses.items.len() {
                    return unexpected();
                }
                for (i, item) in items.iter().enumerate() {
                    self.seal_item(i, item)?;
                }
            }
            _ => return Err(ProtocolError::Unsupported("Responses stream event")),
        }
        let mut result = self.responses.flush()?;
        if terminal {
            let done = done.ok_or(ProtocolError::UnexpectedEvent)?;
            let reason = message_stop(&event["response"], !self.responses.calls.is_empty())?;
            let usage = messages_wire_usage(usage(Endpoint::AnthropicMessages, &done.usage));
            result.push(json!({"type":"message_delta","delta":{"stop_reason":reason,"stop_sequence":null},"usage":usage}));
            result.push(json!({"type":"message_stop"}));
        }
        Ok(result)
    }

    fn indices(&self, e: &Value, part: bool) -> Result<(usize, usize)> {
        let i = position(e, "output_index")?;
        let item = self
            .responses
            .items
            .get(i)
            .ok_or(ProtocolError::UnexpectedEvent)?;
        if item.done || text(e, "item_id")? != item.id {
            return unexpected();
        }
        Ok((
            i,
            if part {
                position(e, "content_index")?
            } else {
                0
            },
        ))
    }

    fn new_item(&mut self, i: usize, v: &Value) -> Result<()> {
        if i != self.responses.items.len() || i >= 4096 {
            return unexpected();
        }
        let item_id = id(v, "id")?;
        if !self.responses.ids.insert(item_id.into()) {
            return unexpected();
        }
        let (tool, parts) = match text(v, "type")? {
            "message" if v["role"] == "assistant" => {
                let mut parts = vec![];
                for p in array(&v["content"], "output content")? {
                    valid_output_text(p)?;
                    let s = text(p, "text")?;
                    self.account(s.len() + 1)?;
                    parts.push(Part {
                        data: s.into(),
                        ..Default::default()
                    });
                }
                (None, parts)
            }
            "function_call" => {
                let call = id(v, "call_id")?;
                if !self.responses.calls.insert(call.into()) {
                    return unexpected();
                }
                let name = id(v, "name")?;
                let args = text(v, "arguments")?;
                self.account(args.len() + name.len() + call.len())?;
                (
                    Some((call.into(), name.into())),
                    vec![Part {
                        data: args.into(),
                        ..Default::default()
                    }],
                )
            }
            _ => return Err(ProtocolError::Unsupported("Responses output type")),
        };
        self.account(item_id.len() + 1)?;
        self.responses.items.push(Item {
            id: item_id.into(),
            tool,
            parts,
            done: false,
        });
        Ok(())
    }

    fn seal_part(&mut self, i: usize, p: usize, full: &str) -> Result<()> {
        let part = self.responses.items[i]
            .parts
            .get(p)
            .ok_or(ProtocolError::UnexpectedEvent)?;
        let suffix = full
            .strip_prefix(&part.data)
            .ok_or(ProtocolError::Invalid("inconsistent final content"))?;
        if part.done && !suffix.is_empty() {
            return unexpected();
        }
        self.account(suffix.len())?;
        let item = &mut self.responses.items[i];
        let part = &mut item.parts[p];
        part.data.push_str(suffix);
        if item.tool.is_some()
            && !part.done
            && !serde_json::from_str::<Value>(&part.data).is_ok_and(|v| v.is_object())
        {
            return Err(ProtocolError::Invalid("function arguments"));
        }
        part.done = true;
        Ok(())
    }

    fn seal_item(&mut self, i: usize, v: &Value) -> Result<()> {
        if i == self.responses.items.len() {
            self.new_item(i, v)?;
        }
        let item = self
            .responses
            .items
            .get(i)
            .ok_or(ProtocolError::UnexpectedEvent)?;
        if item.id != id(v, "id")? {
            return unexpected();
        }
        if let Some((call, name)) = &item.tool {
            if v["type"] != "function_call"
                || text(v, "name")? != name
                || text(v, "call_id")? != call
            {
                return unexpected();
            }
            self.seal_part(i, 0, text(v, "arguments")?)?;
        } else {
            if v["type"] != "message" || v["role"] != "assistant" {
                return unexpected();
            }
            let parts = array(&v["content"], "content")?;
            if item.parts.len() > parts.len() || item.done && item.parts.len() != parts.len() {
                return unexpected();
            }
            for (p, part) in parts.iter().enumerate() {
                valid_output_text(part)?;
                if self.responses.items[i].parts.len() == p {
                    self.account(1)?;
                    self.responses.items[i].parts.push(Part::default());
                }
                self.seal_part(i, p, text(part, "text")?)?;
            }
        }
        self.responses.items[i].done = true;
        Ok(())
    }
}
impl Responses {
    // Messages clients consume non-overlapping content blocks. Interleaved later items
    // accumulate within the shared bound until the earlier item's final content is known.
    fn flush(&mut self) -> Result<Vec<Value>> {
        let mut out = vec![];
        while let Some(item) = self.items.get_mut(self.item) {
            let Some(part) = item.parts.get_mut(self.part) else {
                if !item.done {
                    break;
                }
                self.part = 0;
                self.item += 1;
                continue;
            };
            if !part.started {
                let content = if let Some((id, name)) = &item.tool {
                    json!({"type":"tool_use","id":id,"name":name,"input":{}})
                } else {
                    json!({"type":"text","text":""})
                };
                out.push(json!({"type":"content_block_start","index":self.index,"content_block":content}));
                part.started = true;
            }
            if part.sent < part.data.len() {
                let s = &part.data[part.sent..];
                let delta = if item.tool.is_some() {
                    json!({"type":"input_json_delta","partial_json":s})
                } else {
                    json!({"type":"text_delta","text":s})
                };
                out.push(json!({"type":"content_block_delta","index":self.index,"delta":delta}));
                part.sent = part.data.len();
            }
            if !part.done {
                break;
            }
            out.push(json!({"type":"content_block_stop","index":self.index}));
            self.part += 1;
            self.index += 1;
        }
        Ok(out)
    }
}
