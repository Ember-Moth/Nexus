//! A native HTTP JSON response supplies its own terminal state and usage.
use super::{
    Endpoint, ProtocolError,
    stream::{Completion, Terminal},
    usage::UsageEvidence,
};
use serde_json::Value;

pub fn observe(endpoint: Endpoint, body: &Value) -> Result<Completion, ProtocolError> {
    if body["type"] == "error"
        || body.get("error").is_some_and(Value::is_object) && body["object"] != "response"
    {
        return Ok(Completion {
            terminal: Terminal::Failed,
            stop_reason: None,
            response_id: None,
            usage: UsageEvidence::extract(endpoint, body.get("usage"))?,
        });
    }
    let (terminal, reason) = match endpoint {
        Endpoint::AnthropicMessages => {
            if body["type"] != "message" || !body["content"].is_array() {
                return Err(ProtocolError::Invalid("Messages response"));
            }
            let reason = body["stop_reason"]
                .as_str()
                .filter(|s| !s.is_empty())
                .ok_or(ProtocolError::Invalid("stop reason"))?;
            (Terminal::Completed, Some(reason))
        }
        Endpoint::OpenaiResponses => {
            if body["object"] != "response" {
                return Err(ProtocolError::Invalid("Responses object"));
            }
            let terminal = match body["status"].as_str() {
                Some("completed") if body["output"].is_array() => Terminal::Completed,
                Some("failed") => Terminal::Failed,
                Some("incomplete") => Terminal::Incomplete,
                _ => return Err(ProtocolError::Invalid("response status")),
            };
            (terminal, body["incomplete_details"]["reason"].as_str())
        }
    };
    let id = body["id"]
        .as_str()
        .filter(|id| !id.is_empty() && id.len() <= 255)
        .ok_or(ProtocolError::Invalid("response ID"))?;
    Ok(Completion {
        terminal,
        stop_reason: reason.map(str::to_owned),
        response_id: Some(id.to_owned()),
        usage: UsageEvidence::extract(endpoint, body.get("usage"))?,
    })
}
