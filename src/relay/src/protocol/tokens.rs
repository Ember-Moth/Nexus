//! A count for an individual string is an estimate for request planning only.
//! Monetary reservation takes the separately validated deployment and output bounds.
use super::ProtocolError;
use serde::{Deserialize, Serialize};
use serde_json::Value;
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

/// Anthropic's documented default limit of server-side sampling iterations in one request
/// (`pause_turn`). Message Batches have a higher, undocumented limit.
/// https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons
pub const SERVER_LOOP_LIMIT: u32 = 10;
/// The most model calls Nexus reserves for one request. `catalog.v1_eligible` accepts an
/// input bound up to this multiple of the model's input limit.
pub const MAX_MODEL_CALLS: u32 = 64;
/// Written as `max_tool_calls` when a Responses request uses built-in tools without a cap.
pub const DEFAULT_TOOL_CALLS: u32 = 10;

/// How the upstream can loop over its own model calls within one request.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CallLoop {
    /// A single model call, for example Chat Completions or Bedrock Converse.
    Single,
    /// Anthropic Messages server tools and threshold compaction.
    Messages { batch: bool },
    /// OpenAI Responses built-in tools and `context_management` compaction.
    Responses,
}

// Client-executed Anthropic tools return to the caller instead of looping on the server.
fn client_tool(kind: &str) -> bool {
    kind == "custom"
        || ["bash_", "text_editor_", "computer_", "memory_"]
            .iter()
            .any(|p| kind.starts_with(p))
}

fn count(v: &Value, what: &'static str) -> Result<u64, ProtocolError> {
    v.as_u64()
        .filter(|n| (1..=1_000_000_000).contains(n))
        .ok_or(ProtocolError::Invalid(what))
}

/// The most model calls the upstream may bill for `body`, the final upstream request.
///
/// Each server-side tool call can add one sampling iteration that re-reads the context, and
/// threshold compaction can add one summarizing call before each of them. Every call reads
/// at most the deployment's input ceiling, so the input reservation scales with this count.
/// A Responses request with built-in tools and no `max_tool_calls` gets [`DEFAULT_TOOL_CALLS`].
pub fn model_calls(
    kind: CallLoop,
    compact_only: bool,
    body: &mut Value,
) -> Result<u32, ProtocolError> {
    if compact_only {
        return Ok(1);
    }
    let tools = body["tools"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    let (sampling, compaction) = match kind {
        CallLoop::Single => return Ok(1),
        CallLoop::Messages { batch } => {
            let (mut looping, mut capped, mut caps) = (false, true, 0u64);
            for tool in tools {
                let Some(kind) = tool["type"].as_str().filter(|k| !client_tool(k)) else {
                    continue;
                };
                looping = true;
                // Dynamic filtering versions of search and fetch run code between searches,
                // so only the basic search tool's max_uses bounds its sampling iterations.
                match tool.get("max_uses").filter(|v| !v.is_null()) {
                    Some(v) if kind == "web_search_20250305" => {
                        caps += count(v, "server tool max_uses")?;
                    }
                    _ => capped = false,
                }
            }
            if body.get("mcp_servers").is_some_and(|v| !v.is_null()) {
                (looping, capped) = (true, false);
            }
            let sampling = match (looping, capped, batch) {
                (false, ..) => 1,
                (true, true, true) => caps.saturating_add(1),
                (true, true, false) => caps.saturating_add(1).min(SERVER_LOOP_LIMIT.into()),
                (true, false, false) => SERVER_LOOP_LIMIT.into(),
                // The batch loop limit is not documented, so an uncapped loop has no bound.
                (true, false, true) => {
                    return Err(ProtocolError::Unsupported(
                        "uncapped server tool loop in a batch",
                    ));
                }
            };
            let compaction = body["context_management"]["edits"]
                .as_array()
                .is_some_and(|edits| {
                    edits.iter().any(|e| {
                        e["type"]
                            .as_str()
                            .is_some_and(|t| t.starts_with("compact_"))
                    })
                });
            (sampling, compaction)
        }
        CallLoop::Responses => {
            let built_in = tools
                .iter()
                .any(|t| !matches!(t["type"].as_str(), Some("function" | "custom")));
            let sampling = if built_in {
                let cap = match body.get("max_tool_calls").filter(|v| !v.is_null()) {
                    Some(v) => count(v, "max_tool_calls")?,
                    None => {
                        body["max_tool_calls"] = DEFAULT_TOOL_CALLS.into();
                        DEFAULT_TOOL_CALLS.into()
                    }
                };
                cap.saturating_add(1)
            } else {
                1
            };
            let compaction = body["context_management"]
                .as_array()
                .is_some_and(|entries| entries.iter().any(|e| e["type"] == "compaction"));
            (sampling, compaction)
        }
    };
    let calls = if compaction {
        sampling.saturating_mul(2)
    } else {
        sampling
    };
    u32::try_from(calls)
        .ok()
        .filter(|n| *n <= MAX_MODEL_CALLS)
        .ok_or(ProtocolError::Unsupported("model call bound"))
}

impl ReservationBounds {
    /// Reserve input for `calls` model calls of at most the per-call input ceiling.
    pub fn cover_model_calls(&mut self, calls: u32) -> Result<(), ProtocolError> {
        self.inputs = self
            .inputs
            .checked_mul(calls)
            .filter(|n| *n <= 1_000_000_000)
            .ok_or(ProtocolError::Unsupported("input bound"))?;
        Ok(())
    }
}
