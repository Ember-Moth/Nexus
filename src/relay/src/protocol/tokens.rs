//! A count for an individual string is an estimate for request planning only.
//! Monetary reservation takes the separately validated deployment and output bounds.
use super::ProtocolError;
use serde::{Deserialize, Serialize};
use tiktoken_rs::{cl100k_base_singleton, o200k_base_singleton};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Encoding {
    Cl100kBase,
    O200kBase,
}

#[derive(Debug, PartialEq, Eq)]
pub struct TextEstimate(pub usize);

impl Encoding {
    pub fn estimate(self, text: &str, max_bytes: usize) -> Result<TextEstimate, ProtocolError> {
        if text.len() > max_bytes {
            return Err(ProtocolError::Invalid("text estimate length"));
        }
        let codec = match self {
            Self::Cl100kBase => cl100k_base_singleton(),
            Self::O200kBase => o200k_base_singleton(),
        };
        Ok(TextEstimate(codec.encode_ordinary(text).len()))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct ReservationBounds {
    pub inputs: u32,
    pub outputs: u32,
    #[serde(default)]
    pub guardrail_units: u32,
    pub searches: u32,
    pub file_searches: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub interpreter: Option<super::execution::ContainerTarget>,
    pub execution_mode: super::execution::ExecutionMode,
    pub execution_seconds: u32,
}

impl ReservationBounds {
    pub fn for_deployment(
        input_ceiling: u32,
        output_ceiling: u32,
        requested_output: Option<u32>,
    ) -> Result<Self, ProtocolError> {
        let outputs = requested_output.unwrap_or(output_ceiling);
        if !(1..=1_000_000_000).contains(&input_ceiling)
            || !(1..=1_000_000_000).contains(&output_ceiling)
            || outputs == 0
            || outputs > output_ceiling
        {
            return Err(ProtocolError::Invalid("token bounds"));
        }
        Ok(Self {
            guardrail_units: 0,
            searches: 0,
            file_searches: 0,
            interpreter: None,
            execution_mode: crate::protocol::execution::ExecutionMode::None,
            execution_seconds: 0,
            inputs: input_ceiling,
            outputs,
        })
    }
}
