//! Serialize ordered Chat deltas, including terminal-only Responses output.
use super::{Result, add_urls, finish, usage};
use crate::protocol::{
    ProtocolError,
    convert::{array, text},
};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};

#[derive(Default)]
struct TextPart {
    refusal: bool,
    buffer: String,
    sent: usize,
    closed: bool,
    annotations: Vec<Value>,
}

pub struct Stream {
    id: String,
    created: i64,
    model: String,
    include_usage: bool,
    limit: usize,
    bytes: usize,
    text: HashMap<(u64, u64), TextPart>,
    finished_items: HashSet<u64>,
    next_item: u64,
    next_part: u64,
    calls: HashMap<u64, (usize, String)>,
}
impl Stream {
    pub fn new(model: &str, include_usage: bool, limit: usize) -> Self {
        Self {
            id: String::new(),
            created: 0,
            model: model.into(),
            include_usage,
            limit,
            bytes: 0,
            text: HashMap::new(),
            finished_items: HashSet::new(),
            next_item: 0,
            next_part: 0,
            calls: HashMap::new(),
        }
    }
    fn chunk(&self, delta: Value, end: Value) -> Value {
        json!({"id":self.id,"created":self.created,"object":"chat.completion.chunk","model":self.model,
          "choices":[{"index":0,"delta":delta,"finish_reason":end}]})
    }
    fn part(&mut self, i: u64, p: u64, refusal: bool) -> Result<&mut TextPart> {
        if i >= 4096 || p >= 4096 || !self.text.contains_key(&(i, p)) && self.text.len() >= 4096 {
            return Err(ProtocolError::EventTooLarge);
        }
        if i < self.next_item && !self.text.contains_key(&(i, p)) {
            return Err(ProtocolError::UnexpectedEvent);
        }
        let part = self.text.entry((i, p)).or_insert_with(|| TextPart {
            refusal,
            ..Default::default()
        });
        if part.refusal != refusal {
            return Err(ProtocolError::UnexpectedEvent);
        }
        Ok(part)
    }

    fn observe_text(
        &mut self,
        i: u64,
        p: u64,
        refusal: bool,
        value: &str,
        full: bool,
        closed: bool,
    ) -> Result<()> {
        let available = self.limit - self.bytes;
        let part = self.part(i, p, refusal)?;
        let suffix = if full {
            value
                .strip_prefix(&part.buffer)
                .ok_or(ProtocolError::UnexpectedEvent)?
        } else {
            value
        };
        if part.closed && !suffix.is_empty() {
            return Err(ProtocolError::UnexpectedEvent);
        }
        if suffix.len() > available {
            return Err(ProtocolError::EventTooLarge);
        }
        part.buffer.push_str(suffix);
        part.closed |= closed;
        self.bytes += suffix.len();
        Ok(())
    }

    fn annotation(&mut self, i: u64, p: u64, index: usize, value: &Value) -> Result<()> {
        let available = self.limit - self.bytes;
        let part = self.part(i, p, false)?;
        if let Some(prior) = part.annotations.get(index) {
            return if prior == value {
                Ok(())
            } else {
                Err(ProtocolError::UnexpectedEvent)
            };
        }
        if index != part.annotations.len() || index >= 4096 {
            return Err(ProtocolError::UnexpectedEvent);
        }
        let bytes = value.to_string().len();
        if bytes > available {
            return Err(ProtocolError::EventTooLarge);
        }
        part.annotations.push(value.clone());
        self.bytes += bytes;
        Ok(())
    }

    fn observe_part(&mut self, i: u64, p: u64, v: &Value, closed: bool) -> Result<()> {
        let (refusal, data) = match v["type"].as_str() {
            Some("output_text") => (false, text(v, "text")?),
            Some("refusal") => (true, text(v, "refusal")?),
            _ => return Err(ProtocolError::Unsupported("Chat output part")),
        };
        self.observe_text(i, p, refusal, data, true, closed)?;
        if let Some(annotations) = v.get("annotations").filter(|v| !v.is_null()) {
            let annotations = array(annotations, "output annotations")?;
            if closed && annotations.len() < self.text[&(i, p)].annotations.len() {
                return Err(ProtocolError::Invalid("conflicting URL annotations"));
            }
            for (index, a) in annotations.iter().enumerate() {
                self.annotation(i, p, index, a)?;
            }
        }
        Ok(())
    }

    fn mark_item(&mut self, i: u64) -> Result<()> {
        if i >= 4096 {
            return Err(ProtocolError::EventTooLarge);
        }
        self.finished_items.insert(i);
        Ok(())
    }

    fn finish_message(&mut self, i: u64, item: &Value) -> Result<()> {
        let parts = array(&item["content"], "output content")?;
        for (p, v) in parts.iter().enumerate() {
            self.observe_part(i, p as u64, v, true)?;
        }
        if self.text.contains_key(&(i, parts.len() as u64)) {
            return Err(ProtocolError::UnexpectedEvent);
        }
        self.mark_item(i)
    }

    // Chat flattens Responses text parts. Hold interleaved later parts until
    // the preceding ones close so the visible text and citation coordinates agree.
    fn flush_text(&mut self, out: &mut Vec<Value>) {
        loop {
            let key = (self.next_item, self.next_part);
            let Some(part) = self.text.get_mut(&key) else {
                if self.finished_items.contains(&self.next_item) {
                    self.next_item += 1;
                    self.next_part = 0;
                    continue;
                }
                break;
            };
            let closed = part.closed;
            if part.sent < part.buffer.len() {
                let name = if part.refusal { "refusal" } else { "content" };
                let delta = json!({name:&part.buffer[part.sent..]});
                part.sent = part.buffer.len();
                out.push(self.chunk(delta, Value::Null));
            }
            if !closed {
                break;
            }
            self.next_part += 1;
        }
    }
    fn call(&mut self, i: u64, item: &Value, out: &mut Vec<Value>) -> Result<()> {
        if !self.calls.contains_key(&i) {
            let pos = self.calls.len();
            if pos >= 256 {
                return Err(ProtocolError::EventTooLarge);
            }
            self.calls.insert(i, (pos, String::new()));
            out.push(self.chunk(
                json!({"tool_calls":[{"index":pos,"type":"function","id":text(item,"call_id")?,
                "function":{"name":text(item,"name")?,"arguments":""}}]}),
                Value::Null,
            ));
        }
        Ok(())
    }
    fn args(&mut self, i: u64, value: &str, full: bool, out: &mut Vec<Value>) -> Result<()> {
        let (index, buffer) = self
            .calls
            .get_mut(&i)
            .ok_or(ProtocolError::UnexpectedEvent)?;
        let suffix = if full {
            value
                .strip_prefix(buffer.as_str())
                .ok_or(ProtocolError::UnexpectedEvent)?
        } else {
            value
        };
        self.bytes += suffix.len();
        if self.bytes > self.limit {
            return Err(ProtocolError::EventTooLarge);
        }
        buffer.push_str(suffix);
        let index = *index;
        if !suffix.is_empty() {
            out.push(self.chunk(
                json!({"tool_calls":[{"index":index,"function":{"arguments":suffix}}]}),
                Value::Null,
            ));
        }
        Ok(())
    }
    pub fn frames(&mut self, event: &Value) -> Result<Vec<String>> {
        self.frames_with_stop(event, false)
    }
    pub(crate) fn frames_with_stop(
        &mut self,
        event: &Value,
        local_stop: bool,
    ) -> Result<Vec<String>> {
        let mut out = Vec::new();
        let typ = text(event, "type")?;
        let i = event["output_index"].as_u64().unwrap_or(0);
        let p = event["content_index"].as_u64().unwrap_or(0);
        let r = &event["response"];
        let mut terminal = false;
        match typ {
            "response.created" => {
                self.id = text(r, "id")?.into();
                self.created = r["created_at"].as_i64().unwrap_or(0);
                out.push(self.chunk(json!({"role":"assistant","content":""}), Value::Null));
            }
            "response.output_text.delta" | "response.refusal.delta" => {
                self.observe_text(
                    i,
                    p,
                    typ == "response.refusal.delta",
                    text(event, "delta")?,
                    false,
                    false,
                )?;
            }
            "response.output_text.done" | "response.refusal.done" => {
                let refusal = typ == "response.refusal.done";
                self.observe_text(
                    i,
                    p,
                    refusal,
                    text(event, if refusal { "refusal" } else { "text" })?,
                    true,
                    true,
                )?;
            }
            "response.content_part.added" | "response.content_part.done" => {
                self.observe_part(i, p, &event["part"], typ == "response.content_part.done")?;
            }
            "response.output_text.annotation.added" => {
                let index = event["annotation_index"]
                    .as_u64()
                    .and_then(|n| usize::try_from(n).ok())
                    .ok_or(ProtocolError::Invalid("URL annotation index"))?;
                self.annotation(i, p, index, &event["annotation"])?;
            }
            "response.output_item.added" | "response.output_item.done" => {
                let item = &event["item"];
                match item["type"].as_str() {
                    Some("message") if typ == "response.output_item.done" => {
                        self.finish_message(i, item)?
                    }
                    Some("message") => {
                        for (p, v) in array(&item["content"], "output content")?
                            .iter()
                            .enumerate()
                        {
                            self.observe_part(i, p as u64, v, false)?;
                        }
                    }
                    Some("function_call") => {
                        self.call(i, item, &mut out)?;
                        self.mark_item(i)?;
                    }
                    Some("reasoning" | "web_search_call") => self.mark_item(i)?,
                    _ => return Err(ProtocolError::Unsupported("chat output item")),
                }
            }
            "response.function_call_arguments.delta" => {
                self.args(i, text(event, "delta")?, false, &mut out)?
            }
            "response.completed" | "response.incomplete" => {
                terminal = true;
                let items = array(&r["output"], "output")?;
                let mut annotations = Vec::new();
                let mut offset = 0;
                let mut expected_parts = 0;
                for (index, item) in items.iter().enumerate() {
                    let i = index as u64;
                    match item["type"].as_str() {
                        Some("function_call") => {
                            self.call(i, item, &mut out)?;
                            self.args(i, text(item, "arguments")?, true, &mut out)?;
                            self.mark_item(i)?;
                        }
                        Some("message") => {
                            self.finish_message(i, item)?;
                            for (p, v) in array(&item["content"], "output content")?
                                .iter()
                                .enumerate()
                            {
                                expected_parts += 1;
                                if v["type"] == "output_text" {
                                    let part = &self.text[&(i, p as u64)];
                                    let len = part.buffer.chars().count();
                                    add_urls(&part.annotations, offset, len, &mut annotations)?;
                                    offset += len;
                                }
                            }
                        }
                        Some("reasoning" | "web_search_call") => self.mark_item(i)?,
                        _ => return Err(ProtocolError::Unsupported("chat output item")),
                    }
                }
                if self.text.len() != expected_parts || self.finished_items.len() != items.len() {
                    return Err(ProtocolError::UnexpectedEvent);
                }
                self.flush_text(&mut out);
                if !annotations.is_empty() {
                    out.push(self.chunk(json!({"annotations":annotations}), Value::Null));
                }
                out.push(self.chunk(
                    json!({}),
                    json!(if local_stop {
                        "stop"
                    } else {
                        finish(r, !self.calls.is_empty())?
                    }),
                ));
                if self.include_usage {
                    let mut chunk = self.chunk(json!({}), Value::Null);
                    chunk["choices"] = json!([]);
                    chunk["usage"] = usage(&r["usage"]);
                    out.push(chunk);
                }
            }
            "error" | "response.failed" | "response.cancelled" => {
                out.push(json!({"error":event.get("error").or_else(||r.get("error")).cloned().filter(Value::is_object).unwrap_or_else(||json!({"type":"upstream_error","message":"Upstream generation failed"}))}));
                terminal = true;
            }
            _ => {}
        }
        if !terminal {
            self.flush_text(&mut out);
        }
        let mut frames: Vec<String> = out.into_iter().map(|v| format!("data: {v}\n\n")).collect();
        if terminal {
            frames.push("data: [DONE]\n\n".into());
        }
        Ok(frames)
    }
}
