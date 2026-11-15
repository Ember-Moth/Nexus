//! Keep only active tool indices/IDs and cumulative sizes. Argument bytes stay in their native frames.
use super::{Endpoint, ProtocolError};
use serde_json::Value;
use std::collections::HashMap;

pub struct ToolLimits {
    endpoint: Endpoint,
    bound: usize,
    bytes: usize,
    active: HashMap<String, usize>,
}

impl ToolLimits {
    pub fn new(endpoint: Endpoint, bound: usize) -> Self {
        Self {
            endpoint,
            bound,
            bytes: 0,
            active: HashMap::new(),
        }
    }
    pub fn observe(&mut self, event: &Value) -> Result<(), ProtocolError> {
        let kind = event["type"]
            .as_str()
            .ok_or(ProtocolError::UnexpectedEvent)?;
        match (self.endpoint, kind) {
            (Endpoint::AnthropicMessages, "content_block_start")
                if matches!(
                    event["content_block"]["type"].as_str(),
                    Some("tool_use" | "server_tool_use" | "mcp_tool_use")
                ) =>
            {
                let id = index(event)?;
                self.start(id)?;
            }
            (Endpoint::AnthropicMessages, "content_block_delta")
                if event["delta"]["type"] == "input_json_delta" =>
            {
                let id = index(event)?;
                self.add(
                    &id,
                    event["delta"]["partial_json"]
                        .as_str()
                        .ok_or(ProtocolError::Invalid("tool delta"))?
                        .len(),
                )?;
            }
            (Endpoint::AnthropicMessages, "content_block_stop") => self.end(&index(event)?),
            (Endpoint::OpenaiResponses, "response.output_item.added")
                if matches!(
                    event["item"]["type"].as_str(),
                    Some("function_call" | "custom_tool_call")
                ) =>
            {
                self.start(item(&event["item"]["id"])?.to_owned())?;
            }
            (
                Endpoint::OpenaiResponses,
                "response.function_call_arguments.delta" | "response.custom_tool_call_input.delta",
            ) => {
                let id = item(&event["item_id"])?;
                if !self.active.contains_key(id) {
                    self.start(id.to_owned())?;
                }
                self.add(
                    id,
                    event["delta"]
                        .as_str()
                        .ok_or(ProtocolError::Invalid("tool delta"))?
                        .len(),
                )?;
            }
            (
                Endpoint::OpenaiResponses,
                "response.function_call_arguments.done" | "response.custom_tool_call_input.done",
            ) => self.end(item(&event["item_id"])?),
            (Endpoint::OpenaiResponses, "response.output_item.done")
                if matches!(
                    event["item"]["type"].as_str(),
                    Some("function_call" | "custom_tool_call")
                ) =>
            {
                self.end(item(&event["item"]["id"])?)
            }
            _ => {}
        }
        Ok(())
    }
    fn start(&mut self, id: String) -> Result<(), ProtocolError> {
        if self.active.len() >= 256 || self.active.insert(id, 0).is_some() {
            return Err(ProtocolError::Invalid("active tools"));
        }
        Ok(())
    }
    fn add(&mut self, id: &str, n: usize) -> Result<(), ProtocolError> {
        if n > self.bound - self.bytes {
            return Err(ProtocolError::EventTooLarge);
        }
        *self
            .active
            .get_mut(id)
            .ok_or(ProtocolError::Invalid("tool index"))? += n;
        self.bytes += n;
        Ok(())
    }
    fn end(&mut self, id: &str) {
        self.bytes -= self.active.remove(id).unwrap_or(0);
    }
}
fn index(event: &Value) -> Result<String, ProtocolError> {
    event["index"]
        .as_u64()
        .map(|i| i.to_string())
        .ok_or(ProtocolError::Invalid("tool index"))
}
fn item(value: &Value) -> Result<&str, ProtocolError> {
    value
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= 256)
        .ok_or(ProtocolError::Invalid("tool item ID"))
}
