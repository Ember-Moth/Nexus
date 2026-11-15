use super::{
    Completion, ProtocolError, Result, array, call_item, finish, head, message, receipt, text,
};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};

#[derive(Default)]
struct PendingCall {
    id: String,
    name: String,
    arguments: String,
}

/// Chat sends the finish reason before its final usage-only chunk. Keep the
/// output items in arrival order and commit the Responses terminal on [DONE].
pub(crate) struct Stream<'a> {
    model: &'a str,
    request: &'a Value,
    response: Option<Value>,
    output: Vec<Value>,
    message: Option<usize>,
    tools: Vec<usize>,
    pending_calls: HashMap<usize, PendingCall>,
    call_ids: HashSet<String>,
    reason: Option<String>,
    usage: Option<Value>,
    sequence: u64,
    left: usize,
}

fn fragment<'v>(v: &'v Value, key: &'static str) -> Result<&'v str> {
    if v[key].is_null() {
        Ok("")
    } else {
        text(v, key)
    }
}

impl<'a> Stream<'a> {
    pub fn new(model: &'a str, request: &'a Value, limit: usize) -> Self {
        Self {
            model,
            request,
            response: None,
            output: vec![],
            message: None,
            tools: Vec::new(),
            pending_calls: HashMap::new(),
            call_ids: HashSet::new(),
            reason: None,
            usage: None,
            sequence: 0,
            left: limit,
        }
    }
    fn account(&mut self, n: usize) -> Result<()> {
        self.left = self
            .left
            .checked_sub(n)
            .ok_or(ProtocolError::EventTooLarge)?;
        Ok(())
    }
    fn add_item(&mut self, item: Value, events: &mut Vec<Value>) -> Result<usize> {
        self.account(item.to_string().len())?;
        let pos = self.output.len();
        events.push(json!({"type":"response.output_item.added","output_index":pos,"item":item}));
        self.output.push(item);
        Ok(pos)
    }
    fn part(&mut self, key: &str, data: &str, events: &mut Vec<Value>) -> Result<()> {
        self.account(data.len())?;
        let pos = match self.message {
            Some(i) => i,
            None => {
                let i = self.add_item(message(), events)?;
                self.message = Some(i);
                i
            }
        };
        let item = &mut self.output[pos];
        let id = item["id"].clone();
        let parts = item["content"].as_array_mut().unwrap();
        let kind = if key == "text" {
            "output_text"
        } else {
            "refusal"
        };
        let idx = match parts.iter().position(|p| p["type"] == kind) {
            Some(i) => i,
            None => {
                let p = if key == "text" {
                    json!({"type":kind,"text":"","annotations":[]})
                } else {
                    json!({"type":kind,"refusal":""})
                };
                let i = parts.len();
                events.push(json!({"type":"response.content_part.added","item_id":id,"output_index":pos,"content_index":i,"part":p}));
                parts.push(p);
                i
            }
        };
        let Value::String(s) = &mut parts[idx][key] else {
            unreachable!()
        };
        s.push_str(data);
        let mut e = json!({"type":format!("response.{kind}.delta"),"output_index":pos,"content_index":idx,"item_id":id,"delta":data});
        if key == "text" {
            e["logprobs"] = json!([]);
        }
        events.push(e);
        Ok(())
    }
    fn tool(&mut self, call: &Value, events: &mut Vec<Value>) -> Result<()> {
        let index = call["index"]
            .as_u64()
            .and_then(|n| usize::try_from(n).ok())
            .ok_or(ProtocolError::Invalid("Chat tool index"))?;
        let f = &call["function"];
        if !call["type"].is_null() && call["type"] != "function" {
            return Err(ProtocolError::Unsupported("Chat tool type"));
        }
        let id = fragment(call, "id")?;
        let name = fragment(f, "name")?;
        let arguments = fragment(f, "arguments")?;
        self.account(id.len() + name.len() + arguments.len())?;
        if let Some(&pos) = self.tools.get(index) {
            let item = &self.output[pos];
            if !id.is_empty() && item["call_id"] != id || !name.is_empty() && item["name"] != name {
                return Err(ProtocolError::Invalid("changed Chat tool"));
            }
            self.arguments(pos, arguments, events);
        } else {
            if !self.pending_calls.contains_key(&index) {
                self.account(std::mem::size_of::<PendingCall>())?;
            }
            let call = self.pending_calls.entry(index).or_default();
            if !id.is_empty() && call.id != id {
                call.id.push_str(id);
            }
            if !name.is_empty() && call.name != name {
                call.name.push_str(name);
            }
            if call.id.len() > 256 || call.name.len() > 256 {
                return Err(ProtocolError::Invalid("Chat tool identity"));
            }
            call.arguments.push_str(arguments);
            self.open_calls(false, events)?;
        }
        Ok(())
    }

    // Providers can split the call header across chunks and interleave several
    // calls. Publish immutable Responses item identities in Chat index order.
    fn open_calls(&mut self, finishing: bool, events: &mut Vec<Value>) -> Result<()> {
        while let Some(call) = self.pending_calls.get(&self.tools.len()) {
            if call.id.is_empty() || call.name.is_empty() || !finishing && call.arguments.is_empty()
            {
                break;
            }
            let call = self.pending_calls.remove(&self.tools.len()).unwrap();
            if !self.call_ids.insert(call.id.clone()) {
                return Err(ProtocolError::Invalid("duplicate Chat call ID"));
            }
            let pos = self.add_item(call_item(&call.id, &call.name, ""), events)?;
            self.tools.push(pos);
            self.arguments(pos, &call.arguments, events);
        }
        if finishing && !self.pending_calls.is_empty() {
            return Err(ProtocolError::Invalid("incomplete Chat tool header"));
        }
        Ok(())
    }

    fn arguments(&mut self, pos: usize, delta: &str, events: &mut Vec<Value>) {
        if !delta.is_empty() {
            let item = &mut self.output[pos];
            let Value::String(args) = &mut item["arguments"] else {
                unreachable!()
            };
            args.push_str(delta);
            events.push(json!({
                "type":"response.function_call_arguments.delta",
                "item_id":item["id"], "output_index":pos, "delta":delta
            }));
        }
    }
    fn end(&mut self, events: &mut Vec<Value>) -> Result<Completion> {
        let mut r = self.response.take().ok_or(ProtocolError::Truncated)?;
        self.open_calls(true, events)?;
        r["output"] = std::mem::take(&mut self.output).into();
        let done = finish(
            &mut r,
            self.reason.as_deref().ok_or(ProtocolError::Truncated)?,
            self.usage.as_ref().unwrap_or(&Value::Null),
        )?;
        for (i, item) in array(&r["output"], "output")?.iter().enumerate() {
            if item["type"] == "message" {
                for (p, part) in array(&item["content"], "content")?.iter().enumerate() {
                    let kind = text(part, "type")?;
                    let key = if kind == "output_text" {
                        "text"
                    } else {
                        "refusal"
                    };
                    let mut e = json!({"type":format!("response.{kind}.done"),"item_id":item["id"],"output_index":i,"content_index":p,key:&part[key]});
                    if key == "text" {
                        e["logprobs"] = json!([]);
                    }
                    events.push(e);
                    events.push(json!({"type":"response.content_part.done","item_id":item["id"],"output_index":i,"content_index":p,"part":part}));
                }
            } else {
                events.push(json!({"type":"response.function_call_arguments.done","item_id":item["id"],"output_index":i,"name":item["name"],"arguments":item["arguments"]}));
            }
            events.push(json!({"type":"response.output_item.done","output_index":i,"item":item}));
        }
        events.push(json!({"type":format!("response.{}",text(&r,"status")?),"response":r}));
        Ok(done)
    }
    fn chunk(&mut self, v: &Value, events: &mut Vec<Value>) -> Result<Option<Completion>> {
        if v["error"].is_object() {
            let mut r = self
                .response
                .take()
                .ok_or(ProtocolError::Invalid("Chat stream error"))?;
            r["status"] = "failed".into();
            r["error"] = json!({"code":"server_error","message":v["error"]["message"].as_str().unwrap_or("Chat upstream failed")});
            r["output"] = std::mem::take(&mut self.output).into();
            let (wire, usage) = receipt(&v["usage"]);
            r["usage"] = wire;
            let done = Completion {
                terminal: crate::protocol::stream::Terminal::Failed,
                usage,
                response_id: r["id"].as_str().map(str::to_owned),
                stop_reason: None,
            };
            events.push(json!({"type":"response.failed","response":r}));
            return Ok(Some(done));
        }
        if v["object"] != "chat.completion.chunk" {
            return Err(ProtocolError::Invalid("Chat stream object"));
        }
        if let Some(r) = &self.response {
            if v["id"] != r["id"] {
                return Err(ProtocolError::Invalid("changed Chat response ID"));
            }
        } else {
            let r = head(v, self.model, self.request)?;
            events.push(json!({"type":"response.created","response":r}));
            events.push(json!({"type":"response.in_progress","response":r}));
            self.response = Some(r);
        }
        let choices = array(&v["choices"], "Chat choices")?;
        if let [c] = choices.as_slice() {
            if self.reason.is_some() || c["index"] != 0 {
                return Err(ProtocolError::Invalid("unexpected Chat choice"));
            }
            let d = &c["delta"];
            if !d.is_object()
                || !d["role"].is_null() && d["role"] != "assistant"
                || !d["audio"].is_null()
                || !d["function_call"].is_null()
            {
                return Err(ProtocolError::Unsupported("Chat delta"));
            }
            for (from, to) in [("content", "text"), ("refusal", "refusal")] {
                if !d[from].is_null() {
                    self.part(to, text(d, from)?, events)?;
                }
            }
            if !d["tool_calls"].is_null() {
                for c in array(&d["tool_calls"], "Chat tool calls")? {
                    self.tool(c, events)?;
                }
            }
            if !c["finish_reason"].is_null() {
                self.reason = Some(text(c, "finish_reason")?.into());
            }
        } else if !choices.is_empty() || self.reason.is_none() || v["usage"].is_null() {
            return Err(ProtocolError::Invalid("Chat usage chunk"));
        }
        if !v["usage"].is_null() {
            if self.reason.is_none() || self.usage.is_some() {
                return Err(ProtocolError::Invalid("unexpected Chat usage"));
            }
            self.usage = Some(v["usage"].clone());
        }
        Ok(None)
    }
    pub fn event(&mut self, data: &str) -> Result<(Vec<Value>, Option<Completion>)> {
        let mut events = Vec::new();
        let done = if data.trim() == "[DONE]" {
            Some(self.end(&mut events)?)
        } else {
            self.chunk(
                &serde_json::from_str(data).map_err(|_| ProtocolError::Invalid("Chat SSE JSON"))?,
                &mut events,
            )?
        };
        for e in &mut events {
            e["sequence_number"] = self.sequence.into();
            self.sequence += 1;
        }
        Ok((events, done))
    }
}
