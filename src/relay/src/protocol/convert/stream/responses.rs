use super::{Result, Stream, position, unexpected, usage};
use crate::protocol::{
    Endpoint, ProtocolError,
    convert::{
        array, id,
        response::{message, message_stop, messages_wire_usage, part_text},
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
    private: Option<super::private_response::PrivateResponse>,
    cited: Option<[u8; 32]>,
    attachment: Option<Value>,
    id: String,
    tool: Option<(String, String)>,
    parts: Vec<Part>,
    done: bool,
}
#[derive(Default)]
struct Part {
    annotations: Vec<Value>,
    citations: Vec<Value>,
    refusal: bool,
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
            "response.compaction.compacting"
            | "response.reasoning_summary_part.added"
            | "response.reasoning_summary_part.done"
            | "response.reasoning_summary_text.delta"
            | "response.reasoning_summary_text.done"
            | "response.reasoning_text.delta"
            | "response.reasoning_text.done"
            | "response.content_part.added"
            | "response.content_part.done"
                if self
                    .responses
                    .items
                    .get(position(event, "output_index")?)
                    .is_some_and(|i| i.private.is_some()) =>
            {
                let (i, _) = self.indices(event, false)?;
                self.account(event.to_string().len())?;
                self.responses.items[i]
                    .private
                    .as_mut()
                    .expect("reasoning item")
                    .event(kind, event)?;
            }
            "response.content_part.added" => {
                let (i, p) = self.indices(event, true)?;
                let part = &event["part"];
                let initial = self.output_text(part)?;
                self.account(initial.len() + 1)?;
                let item = &mut self.responses.items[i];
                if item.tool.is_some() || item.parts.len() != p {
                    return unexpected();
                }
                item.parts.push(Part {
                    refusal: part["type"] == "refusal",
                    data: initial.into(),
                    ..Default::default()
                });
                self.annotations(i, p, part, false)?;
            }
            "response.output_text.annotation.added" if self.history.is_some() => {
                let (i, p) = self.indices(event, true)?;
                let index = position(event, "annotation_index")?;
                self.open_text_part(i, p, false)?;
                self.annotation(i, p, index, &event["annotation"])?;
            }
            "response.output_text.delta"
            | "response.refusal.delta"
            | "response.function_call_arguments.delta" => {
                let tool = kind == "response.function_call_arguments.delta";
                let (i, p) = self.indices(event, !tool)?;
                let delta = text(event, "delta")?;
                self.account(delta.len())?;
                if !tool {
                    self.open_text_part(i, p, kind == "response.refusal.delta")?;
                }
                let item = &mut self.responses.items[i];
                if item.private.is_some() || item.tool.is_some() != tool {
                    return unexpected();
                }
                let part = item
                    .parts
                    .get_mut(p)
                    .ok_or(ProtocolError::UnexpectedEvent)?;
                if part.done || part.refusal != (kind == "response.refusal.delta") {
                    return unexpected();
                }
                part.data.push_str(delta);
            }
            "response.output_text.done"
            | "response.refusal.done"
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
                let (full, refusal) = match kind {
                    "response.function_call_arguments.done" => (text(event, "arguments")?, false),
                    "response.content_part.done" => (
                        self.output_text(&event["part"])?,
                        event["part"]["type"] == "refusal",
                    ),
                    "response.refusal.done" => (text(event, "refusal")?, true),
                    _ => (text(event, "text")?, false),
                };
                if !tool {
                    self.open_text_part(i, p, refusal)?;
                }
                if kind == "response.content_part.done" {
                    self.annotations(i, p, &event["part"], true)?;
                }
                self.seal_part(i, p, full, refusal)?;
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

    fn output_text<'a>(&self, part: &'a Value) -> Result<&'a str> {
        if self.history.is_some() {
            crate::protocol::history::citations::part_text(part)
        } else {
            part_text(part)
        }
    }
    fn annotation(&mut self, i: usize, p: usize, n: usize, value: &Value) -> Result<()> {
        let seen = &self.responses.items[i].parts[p].annotations;
        if let Some(prior) = seen.get(n) {
            return if prior == value { Ok(()) } else { unexpected() };
        }
        if n != seen.len() || n >= 4096 {
            return unexpected();
        }
        self.account(value.to_string().len())?;
        self.responses.items[i].parts[p]
            .annotations
            .push(value.clone());
        Ok(())
    }
    fn annotations(&mut self, i: usize, p: usize, part: &Value, complete: bool) -> Result<()> {
        if let Some(v) = part.get("annotations").filter(|v| !v.is_null()) {
            let a = array(v, "output annotations")?;
            if complete && a.len() < self.responses.items[i].parts[p].annotations.len() {
                return unexpected();
            }
            for (n, v) in a.iter().enumerate() {
                self.annotation(i, p, n, v)?;
            }
        }
        Ok(())
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
        let mut private = None;
        let (tool, parts) = match text(v, "type")? {
            "reasoning" | "compaction" if self.history.is_some() => {
                self.account(v.to_string().len())?;
                private = Some(super::private_response::PrivateResponse::begin(
                    v,
                    self.history.as_ref().expect("history writer").hide_summary,
                )?);
                (None, vec![])
            }
            "message" if v["role"] == "assistant" => {
                let mut parts = vec![];
                for p in array(&v["content"], "output content")? {
                    let s = self.output_text(p)?;
                    self.account(s.len() + 1)?;
                    parts.push(Part {
                        refusal: p["type"] == "refusal",
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
            private,
            cited: None,
            attachment: None,
            tool,
            parts,
            done: false,
        });
        if v["type"] == "message" {
            for (p, part) in array(&v["content"], "output content")?.iter().enumerate() {
                self.annotations(i, p, part, false)?;
            }
        }
        Ok(())
    }

    // Some Responses-compatible deployments start a part with its first delta.
    // The item binding and contiguous content index are still mandatory.
    fn open_text_part(&mut self, i: usize, p: usize, refusal: bool) -> Result<()> {
        let item = &self.responses.items[i];
        if item.tool.is_some() || item.private.is_some() || p > item.parts.len() {
            return unexpected();
        }
        if p == item.parts.len() {
            self.account(1)?;
            self.responses.items[i].parts.push(Part {
                refusal,
                ..Default::default()
            });
        }
        Ok(())
    }

    fn seal_part(&mut self, i: usize, p: usize, full: &str, refusal: bool) -> Result<()> {
        let part = self.responses.items[i]
            .parts
            .get(p)
            .ok_or(ProtocolError::UnexpectedEvent)?;
        if part.refusal != refusal {
            return unexpected();
        }
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
        if item.private.is_some() {
            let rid = self.rid.as_deref().ok_or(ProtocolError::UnexpectedEvent)?;
            let w = self
                .history
                .as_ref()
                .ok_or(ProtocolError::UnexpectedEvent)?;
            let item = &mut self.responses.items[i];
            let bytes = item
                .private
                .as_mut()
                .expect("reasoning item")
                .finish(v, w, rid, i)?;
            item.done = true;
            self.account(bytes)?;
            return Ok(());
        }
        if let Some((call, name)) = &item.tool {
            if v["type"] != "function_call"
                || text(v, "name")? != name
                || text(v, "call_id")? != call
            {
                return unexpected();
            }
            self.seal_part(i, 0, text(v, "arguments")?, false)?;
        } else {
            if v["type"] != "message" || v["role"] != "assistant" {
                return unexpected();
            }
            let parts = array(&v["content"], "content")?;
            if item.parts.len() > parts.len() || item.done && item.parts.len() != parts.len() {
                return unexpected();
            }
            for (p, part) in parts.iter().enumerate() {
                let full = self.output_text(part)?;
                let refusal = part["type"] == "refusal";
                if self.responses.items[i].parts.len() == p {
                    self.account(1)?;
                    self.responses.items[i].parts.push(Part {
                        refusal,
                        ..Default::default()
                    });
                }
                self.annotations(i, p, part, true)?;
                self.seal_part(i, p, full, refusal)?;
            }
            if self.responses.items[i]
                .parts
                .iter()
                .any(|p| !p.annotations.is_empty())
            {
                let mut final_item = v.clone();
                for (part, state) in final_item["content"]
                    .as_array_mut()
                    .unwrap()
                    .iter_mut()
                    .zip(&self.responses.items[i].parts)
                {
                    if !state.annotations.is_empty() {
                        part["annotations"] = state.annotations.clone().into();
                    }
                }
                use sha2::{Digest, Sha256};
                let digest = Sha256::digest(final_item.to_string().as_bytes()).into();
                if let Some(prior) = self.responses.items[i].cited {
                    if prior != digest {
                        return unexpected();
                    }
                } else {
                    let mut blocks = self
                        .history
                        .as_ref()
                        .ok_or(ProtocolError::Unsupported("Responses annotations"))?
                        .project_group(
                            &final_item,
                            self.rid.as_deref().ok_or(ProtocolError::UnexpectedEvent)?,
                            i,
                        )?;
                    self.account(blocks.iter().map(|v| v.to_string().len()).sum())?;
                    let item = &mut self.responses.items[i];
                    if blocks.len() == item.parts.len() + 1 {
                        item.attachment = blocks.pop();
                    }
                    if blocks.len() != item.parts.len() {
                        return unexpected();
                    }
                    for (state, mut block) in self.responses.items[i].parts.iter_mut().zip(blocks) {
                        if let Some(c) = block.get_mut("citations") {
                            state.citations =
                                c.as_array_mut().map(std::mem::take).unwrap_or_default();
                        }
                    }
                    self.responses.items[i].cited = Some(digest);
                }
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
            if let Some(private) = &mut item.private {
                if !private.flush(self.index, &mut out)? {
                    break;
                }
                self.item += 1;
                self.index += 1;
                continue;
            }
            let Some(part) = item.parts.get_mut(self.part) else {
                if !item.done {
                    break;
                }
                if let Some(block) = item.attachment.take() {
                    out.push(json!({"type":"content_block_start","index":self.index,"content_block":block}));
                    out.push(json!({"type":"content_block_stop","index":self.index}));
                    self.index += 1;
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
            // A final output item can attach annotations after output_text.done.
            if !part.done || item.tool.is_none() && !item.done {
                break;
            }
            for c in &part.citations {
                out.push(json!({"type":"content_block_delta","index":self.index,"delta":{"type":"citations_delta","citation":c}}));
            }
            out.push(json!({"type":"content_block_stop","index":self.index}));
            self.part += 1;
            self.index += 1;
        }
        Ok(out)
    }
}
