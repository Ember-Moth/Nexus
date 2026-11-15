use super::super::{Result, fields, text};
use crate::protocol::{
    ProtocolError,
    history::{Writer, item_id},
};
use serde_json::{Value, json};

/// Accumulate a Claude block until its opaque continuation is complete. Only
/// the provider's displayed thinking streams as Responses summary text.
pub(super) struct PrivateMessage {
    hide_summary: bool,
    body: Value,
    content: Option<String>,
    opaque: Option<String>,
    id: String,
    index: usize,
}
impl PrivateMessage {
    pub fn start(
        v: &Value,
        rid: &str,
        index: usize,
        hide_summary: bool,
    ) -> Result<(Self, Vec<Value>)> {
        let mut body = v.clone();
        let kind = text(v, "type")?;
        let mut events = Vec::new();
        let compact = kind == "compaction";
        let id = item_id(rid, index, compact);
        let (field, secret) = match kind {
            "thinking" => {
                fields(v, &["type", "thinking", "signature"])?;
                ("thinking", "signature")
            }
            "redacted_thinking" => {
                fields(v, &["type", "data"])?;
                ("", "data")
            }
            "compaction" => {
                fields(
                    v,
                    &[
                        "type",
                        "content",
                        "signature",
                        "encrypted_content",
                        "tool_changes",
                    ],
                )?;
                ("content", "encrypted_content")
            }
            _ => return Err(ProtocolError::UnexpectedEvent),
        };
        let mut take = |name: &str| -> Result<Option<String>> {
            match body.get_mut(name).map(Value::take) {
                None | Some(Value::Null) => Ok(None),
                Some(Value::String(s)) => Ok(Some(s)),
                _ => Err(ProtocolError::Invalid("private block field")),
            }
        };
        let content = take(field)?;
        let opaque = take(secret)?;
        if !compact {
            events.push(
                json!({"type":"response.output_item.added","output_index":index,
                "item":{"id":id,"type":"reasoning","status":"in_progress","summary":[]}}),
            );
        }
        if kind == "thinking" && !hide_summary {
            events.push(json!({"type":"response.reasoning_summary_part.added","output_index":index,"summary_index":0,"item_id":id,"part":{"type":"summary_text","text":""}}));
            if let Some(s) = content.as_deref().filter(|s| !s.is_empty()) {
                events.push(summary_delta(&id, index, s));
            }
        }
        Ok((
            Self {
                hide_summary,
                body,
                content,
                opaque,
                id,
                index,
            },
            events,
        ))
    }

    pub fn delta(&mut self, v: &Value) -> Result<Vec<Value>> {
        match (self.body["type"].as_str(), v["type"].as_str()) {
            (Some("thinking"), Some("thinking_delta")) => {
                let s = text(v, "thinking")?;
                if self.opaque.as_ref().is_some_and(|s| !s.is_empty()) {
                    return Err(ProtocolError::UnexpectedEvent);
                }
                self.content.get_or_insert_with(String::new).push_str(s);
                Ok(if self.hide_summary {
                    vec![]
                } else {
                    vec![summary_delta(&self.id, self.index, s)]
                })
            }
            (Some("thinking"), Some("signature_delta")) => {
                self.opaque
                    .get_or_insert_with(String::new)
                    .push_str(text(v, "signature")?);
                Ok(vec![])
            }
            (Some("compaction"), Some("compaction_delta")) => {
                fields(v, &["type", "content", "encrypted_content"])?;
                for (slot, key) in [
                    (&mut self.content, "content"),
                    (&mut self.opaque, "encrypted_content"),
                ] {
                    if v.get(key).is_some_and(|v| !v.is_null()) {
                        slot.get_or_insert_with(String::new).push_str(text(v, key)?);
                    }
                }
                Ok(vec![])
            }
            _ => Err(ProtocolError::UnexpectedEvent),
        }
    }

    pub fn finish(mut self, w: &Writer, rid: &str) -> Result<(Value, Vec<Value>)> {
        let mut events = Vec::new();
        let (field, secret) = match self.body["type"].as_str() {
            Some("thinking") => {
                let s = self
                    .content
                    .as_deref()
                    .ok_or(ProtocolError::Invalid("thinking text"))?;
                if !self.hide_summary {
                    events.push(json!({"type":"response.reasoning_summary_text.done","output_index":self.index,"summary_index":0,"item_id":self.id,"text":s}));
                    events.push(json!({"type":"response.reasoning_summary_part.done","output_index":self.index,"summary_index":0,"item_id":self.id,"part":{"type":"summary_text","text":s}}));
                }
                ("thinking", "signature")
            }
            Some("redacted_thinking") => ("", "data"),
            Some("compaction") => ("content", "encrypted_content"),
            _ => return Err(ProtocolError::UnexpectedEvent),
        };
        if let Some(s) = self.content {
            self.body[field] = s.into();
        }
        if let Some(s) = self.opaque {
            self.body[secret] = s.into();
        }
        let item = w.project(&self.body, rid, self.index)?;
        if item["type"] == "compaction" {
            events.push(
                json!({"type":"response.output_item.added","output_index":self.index,"item":item}),
            );
        }
        events.push(
            json!({"type":"response.output_item.done","output_index":self.index,"item":item}),
        );
        Ok((item, events))
    }
}
fn summary_delta(id: &str, i: usize, s: &str) -> Value {
    json!({"type":"response.reasoning_summary_text.delta","output_index":i,"summary_index":0,"item_id":id,"delta":s})
}
