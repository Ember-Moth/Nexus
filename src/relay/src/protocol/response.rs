//! A native HTTP JSON response supplies its own terminal state and usage.
use super::{
    Endpoint, ProtocolError,
    request::Operation,
    stream::{Completion, Terminal},
    usage::UsageEvidence,
};
use serde_json::Value;

pub fn observe(endpoint: Endpoint, body: &Value) -> Result<Completion, ProtocolError> {
    observe_operation(endpoint, Operation::Generate, body, Default::default())
}

pub(crate) fn observe_operation(
    endpoint: Endpoint,
    operation: Operation,
    body: &Value,
    search_metered: crate::protocol::hosted::SearchMeters,
) -> Result<Completion, ProtocolError> {
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
        Endpoint::OpenaiResponses if operation == Operation::Compact => {
            if body["object"] != "response.compaction"
                || !body["output"].as_array().is_some_and(|items| {
                    items.iter().all(Value::is_object)
                        && items.iter().any(|item| {
                            item["type"] == "compaction"
                                && item["encrypted_content"]
                                    .as_str()
                                    .is_some_and(|s| !s.is_empty())
                        })
                })
            {
                return Err(ProtocolError::Invalid("compaction response"));
            }
            (Terminal::Completed, None)
        }
        Endpoint::OpenaiResponses => {
            if body["object"] != "response" {
                return Err(ProtocolError::Invalid("Responses object"));
            }
            let terminal = match body["status"].as_str() {
                Some("completed") if body["output"].is_array() => Terminal::Completed,
                Some("failed") => Terminal::Failed,
                Some("cancelled") => Terminal::Cancelled,
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
    let mut usage = UsageEvidence::for_model(endpoint, body.get("usage"), body["model"].as_str())?;
    if endpoint == Endpoint::AnthropicMessages {
        let compacted = body["content"]
            .as_array()
            .is_some_and(|v| v.iter().any(|b| b["type"] == "compaction"));
        if reason == Some("compaction") && !compacted {
            return Err(ProtocolError::Invalid("missing compaction block"));
        }
        if !super::usage::iterations::terminal_totals(body.get("usage"))
            || compacted
                && !usage
                    .raw_usage
                    .as_ref()
                    .is_some_and(|v| v.contains_key("compaction_output_tokens"))
        {
            usage.raw_usage = None;
        }
    }
    if endpoint == Endpoint::OpenaiResponses && operation == Operation::Generate {
        usage.responses_output(body, &Default::default(), search_metered)?;
    }
    Ok(Completion {
        terminal,
        stop_reason: reason.map(str::to_owned),
        response_id: Some(id.to_owned()),
        usage,
    })
}
