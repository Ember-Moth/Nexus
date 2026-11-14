use super::tokens::ReservationBounds;
use super::{Endpoint, ProtocolError};
use http::{HeaderMap, Method};
use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Route {
    Generate(Endpoint),
    ResponsesWebSocket,
}

pub fn route(method: &Method, path: &str) -> Option<Route> {
    match (method.as_str(), path) {
        ("POST", "/v1/messages") => Some(Route::Generate(Endpoint::AnthropicMessages)),
        ("POST", "/v1/responses") => Some(Route::Generate(Endpoint::OpenaiResponses)),
        ("GET", "/v1/responses") => Some(Route::ResponsesWebSocket),
        _ => None,
    }
}

fn single_header<'a>(headers: &'a HeaderMap, name: &'static str) -> Result<&'a str, ProtocolError> {
    let mut all = headers.get_all(name).iter();
    let first = all
        .next()
        .and_then(|h| h.to_str().ok())
        .ok_or(ProtocolError::Invalid(name))?;
    if all.next().is_some() {
        return Err(ProtocolError::Invalid(name));
    }
    Ok(first)
}

impl Route {
    pub fn key(self, headers: &HeaderMap) -> Result<&str, ProtocolError> {
        let key = match self {
            Self::Generate(Endpoint::AnthropicMessages) => single_header(headers, "x-api-key")?,
            _ => {
                let value = single_header(headers, "authorization")?;
                let (scheme, token) = value
                    .split_once(' ')
                    .ok_or(ProtocolError::Invalid("bearer credential"))?;
                if !scheme.eq_ignore_ascii_case("bearer") {
                    return Err(ProtocolError::Invalid("bearer credential"));
                }
                token
            }
        };
        if key.is_empty()
            || key
                .bytes()
                .any(|byte| !byte.is_ascii_graphic() || byte == b',')
        {
            return Err(ProtocolError::Invalid("API key"));
        }
        Ok(key)
    }
}

/// Keep the native body intact until a specific authorized deployment is selected.
#[derive(Debug)]
pub struct Generate {
    pub model: String,
    pub endpoint: Endpoint,
    pub stream: bool,
    pub requested_output: Option<u32>,
    body: Value,
}

impl Generate {
    pub fn parse(endpoint: Endpoint, body: Value, websocket: bool) -> Result<Self, ProtocolError> {
        let obj = body
            .as_object()
            .ok_or(ProtocolError::Invalid("request object"))?;
        let model = obj
            .get("model")
            .and_then(Value::as_str)
            .filter(|v| !v.is_empty() && v.len() <= 200)
            .ok_or(ProtocolError::Invalid("model"))?
            .to_owned();
        let stream = match obj.get("stream") {
            Some(_) if websocket => {
                return Err(ProtocolError::Unsupported("stream in WebSocket create"));
            }
            Some(v) => v.as_bool().ok_or(ProtocolError::Invalid("stream"))?,
            None => websocket,
        };
        let output_field = match endpoint {
            Endpoint::AnthropicMessages => {
                if obj.contains_key("mcp_servers") || obj.contains_key("container") {
                    return Err(ProtocolError::Unsupported("hosted tool or container"));
                }
                if websocket {
                    return Err(ProtocolError::Unsupported("Messages WebSocket"));
                }
                if !obj
                    .get("messages")
                    .and_then(Value::as_array)
                    .is_some_and(|v| !v.is_empty())
                {
                    return Err(ProtocolError::Invalid("messages"));
                }
                "max_tokens"
            }
            Endpoint::OpenaiResponses => {
                if obj.get("store").is_some_and(|v| v != false)
                    || obj.contains_key("conversation")
                    || obj.contains_key("prompt")
                    || obj
                        .get("input")
                        .and_then(Value::as_array)
                        .is_some_and(|items| items.iter().any(|i| i["type"] == "item_reference"))
                    || obj.get("background").is_some_and(|v| v != false)
                    || (websocket && obj.contains_key("background"))
                {
                    return Err(ProtocolError::Unsupported("stored or background response"));
                }
                if obj.get("previous_response_id").is_some_and(|v| {
                    !v.is_null() && (!websocket || v.as_str().is_none_or(str::is_empty))
                }) {
                    return Err(ProtocolError::Unsupported("previous_response_id"));
                }
                if !obj
                    .get("input")
                    .is_some_and(|v| v.is_string() || v.is_array())
                {
                    return Err(ProtocolError::Invalid("input"));
                }
                "max_output_tokens"
            }
        };
        if let Some(tools) = obj.get("tools") {
            for tool in tools.as_array().ok_or(ProtocolError::Invalid("tools"))? {
                let tool = tool.as_object().ok_or(ProtocolError::Invalid("tool"))?;
                let custom = match endpoint {
                    Endpoint::OpenaiResponses => {
                        tool.get("type").and_then(Value::as_str) == Some("function")
                    }
                    Endpoint::AnthropicMessages => {
                        !tool.contains_key("type")
                            || tool.get("type").and_then(Value::as_str) == Some("custom")
                    }
                };
                if !custom {
                    return Err(ProtocolError::Unsupported("hosted tool"));
                }
                if !tool
                    .get("name")
                    .and_then(Value::as_str)
                    .is_some_and(|s| !s.is_empty())
                {
                    return Err(ProtocolError::Invalid("tool name"));
                }
            }
        }
        let requested_output = obj
            .get(output_field)
            .map(|v| {
                v.as_u64()
                    .filter(|n| (1..=1_000_000_000).contains(n))
                    .map(|n| n as u32)
                    .ok_or(ProtocolError::Invalid(output_field))
            })
            .transpose()?;
        if endpoint == Endpoint::AnthropicMessages && requested_output.is_none() {
            return Err(ProtocolError::Invalid(output_field));
        }
        Ok(Self {
            model,
            endpoint,
            stream,
            requested_output,
            body,
        })
    }

    pub fn has_tools(&self) -> bool {
        self.body
            .get("tools")
            .and_then(Value::as_array)
            .is_some_and(|tools| !tools.is_empty())
            || self.body.get("tool_choice").is_some_and(|choice| {
                choice != "none" && choice.get("type").is_none_or(|kind| kind != "none")
            })
            || self
                .body
                .get("input")
                .and_then(Value::as_array)
                .is_some_and(|items| {
                    items.iter().any(|i| {
                        matches!(
                            i["type"].as_str(),
                            Some("function_call" | "function_call_output")
                        )
                    })
                })
            || self
                .body
                .get("messages")
                .and_then(Value::as_array)
                .is_some_and(|messages| {
                    messages
                        .iter()
                        .filter_map(|m| m["content"].as_array())
                        .flatten()
                        .any(|b| matches!(b["type"].as_str(), Some("tool_use" | "tool_result")))
                })
    }

    pub fn convert(&self) -> Result<Value, ProtocolError> {
        super::convert::request::convert(self.endpoint, &self.body)
    }

    pub fn previous_response_id(&self) -> Option<&str> {
        self.body
            .get("previous_response_id")
            .and_then(Value::as_str)
    }

    pub fn prepare_native(
        &self,
        upstream_model: &str,
        inputs: u32,
        outputs: u32,
    ) -> Result<(Value, ReservationBounds), ProtocolError> {
        let bounds = ReservationBounds::for_deployment(inputs, outputs, self.requested_output)?;
        let mut body = self.body.clone();
        body["model"] = upstream_model.into();
        if self.endpoint == Endpoint::OpenaiResponses {
            body["store"] = false.into();
            body["max_output_tokens"] = bounds.outputs.into();
        }
        Ok((body, bounds))
    }
}
