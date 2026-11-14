pub mod cache;
pub mod convert;
pub mod request;
pub mod response;
pub mod sse;
pub mod stream;
pub mod tokens;
pub mod tools;
pub mod usage;
pub mod websocket;

use serde::{Deserialize, Serialize};
use thiserror::Error;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Endpoint {
    AnthropicMessages,
    OpenaiResponses,
}

impl Endpoint {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::AnthropicMessages => "anthropic_messages",
            Self::OpenaiResponses => "openai_responses",
        }
    }
    pub fn usage_format(self) -> &'static str {
        match self {
            Self::AnthropicMessages => "anthropic.messages.v1",
            Self::OpenaiResponses => "openai.responses.v1",
        }
    }
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum ProtocolError {
    #[error("invalid {0}")]
    Invalid(&'static str),
    #[error("unsupported {0}")]
    Unsupported(&'static str),
    #[error("event exceeds its configured size limit")]
    EventTooLarge,
    #[error("incomplete event stream")]
    Truncated,
    #[error("unexpected event for the current stream")]
    UnexpectedEvent,
}
