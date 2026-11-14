//! Responses WebSocket wire events. Session authorization and admission run for every Create.
use super::{Endpoint, ProtocolError, request::Generate};
use base64::{Engine, engine::general_purpose::STANDARD};
use http::{HeaderMap, Method, header};
use serde_json::Value;

pub fn validate_upgrade(method: &Method, headers: &HeaderMap) -> Result<(), ProtocolError> {
    fn contains(headers: &HeaderMap, key: http::HeaderName, token: &str) -> bool {
        headers
            .get_all(key)
            .iter()
            .filter_map(|v| v.to_str().ok())
            .flat_map(|s| s.split(','))
            .any(|v| v.trim().eq_ignore_ascii_case(token))
    }
    let mut keys = headers.get_all(header::SEC_WEBSOCKET_KEY).iter();
    let key = keys.next().and_then(|v| v.to_str().ok());
    let mut decoded = [0u8; 18];
    if keys.next().is_some()
        || key.and_then(|k| STANDARD.decode_slice(k, &mut decoded).ok()) != Some(16)
        || headers
            .get_all(header::SEC_WEBSOCKET_VERSION)
            .iter()
            .count()
            != 1
        || method != Method::GET
        || !contains(headers, header::CONNECTION, "upgrade")
        || !contains(headers, header::UPGRADE, "websocket")
        || headers
            .get(header::SEC_WEBSOCKET_VERSION)
            .and_then(|v| v.to_str().ok())
            != Some("13")
    {
        return Err(ProtocolError::Invalid("WebSocket upgrade"));
    }
    Ok(())
}

pub enum ClientEvent {
    Create {
        event_id: Option<String>,
        stream_id: Option<String>,
        request: Generate,
    },
    Cancel {
        event_id: Option<String>,
        stream_id: Option<String>,
        response_id: Option<String>,
    },
}

pub fn parse_event(bytes: &[u8], limit: usize) -> Result<ClientEvent, ProtocolError> {
    if bytes.len() > limit {
        return Err(ProtocolError::EventTooLarge);
    }
    let mut value: Value =
        serde_json::from_slice(bytes).map_err(|_| ProtocolError::Invalid("WebSocket JSON"))?;
    let obj = value
        .as_object_mut()
        .ok_or(ProtocolError::Invalid("WebSocket event"))?;
    fn id(
        obj: &mut serde_json::Map<String, Value>,
        key: &'static str,
    ) -> Result<Option<String>, ProtocolError> {
        obj.remove(key)
            .map(|v| {
                v.as_str()
                    .filter(|v| !v.is_empty() && v.len() <= 256)
                    .map(str::to_owned)
                    .ok_or(ProtocolError::Invalid(key))
            })
            .transpose()
    }
    let event_id = id(obj, "event_id")?;
    let stream_id = id(obj, "stream_id")?;
    if stream_id.as_ref().is_some_and(|v| {
        !v.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b))
    }) {
        return Err(ProtocolError::Invalid("stream_id"));
    }
    match obj
        .remove("type")
        .and_then(|v| v.as_str().map(str::to_owned))
        .as_deref()
    {
        Some("response.create") => {
            if obj.remove("generate").is_some_and(|v| v != true) {
                return Err(ProtocolError::Unsupported("warmup"));
            }
            Ok(ClientEvent::Create {
                event_id,
                stream_id,
                request: Generate::parse(Endpoint::OpenaiResponses, value, true)?,
            })
        }
        Some("response.cancel") => {
            let response_id = id(obj, "response_id")?;
            if !obj.is_empty() {
                return Err(ProtocolError::Invalid("cancel fields"));
            }
            Ok(ClientEvent::Cancel {
                event_id,
                stream_id,
                response_id,
            })
        }
        _ => Err(ProtocolError::Unsupported("WebSocket event type")),
    }
}
