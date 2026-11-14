//! Direct Messages / Responses conversion. Only the deployment's upstream observer supplies billing evidence.
pub mod request;
pub mod response;
pub mod stream;

use super::{Endpoint, ProtocolError};
use serde_json::{Map, Value};

pub type Result<T> = std::result::Result<T, ProtocolError>;

#[derive(Clone, Copy)]
pub struct Exchange<'a> {
    pub client: Endpoint,
    pub upstream: Endpoint,
    pub public_model: &'a str,
}

impl Exchange<'_> {
    pub fn converts(self) -> bool {
        self.client != self.upstream
    }
}

pub(crate) fn fields<'a>(v: &'a Value, allowed: &[&str]) -> Result<&'a Map<String, Value>> {
    let obj = v
        .as_object()
        .ok_or(ProtocolError::Invalid("protocol object"))?;
    if obj.keys().any(|k| !allowed.contains(&k.as_str())) {
        return Err(ProtocolError::Unsupported("unmapped protocol field"));
    }
    Ok(obj)
}
pub(crate) fn text<'a>(v: &'a Value, key: &'static str) -> Result<&'a str> {
    v.get(key)
        .and_then(Value::as_str)
        .ok_or(ProtocolError::Invalid(key))
}
pub(crate) fn id<'a>(v: &'a Value, key: &'static str) -> Result<&'a str> {
    let s = text(v, key)?;
    if s.is_empty() || s.len() > 256 {
        return Err(ProtocolError::Invalid(key));
    }
    Ok(s)
}
pub(crate) fn array<'a>(v: &'a Value, what: &'static str) -> Result<&'a Vec<Value>> {
    v.as_array().ok_or(ProtocolError::Invalid(what))
}
fn arguments(v: &Value) -> Result<Value> {
    let args: Value = serde_json::from_str(text(v, "arguments")?)
        .map_err(|_| ProtocolError::Invalid("function arguments"))?;
    if !args.is_object() {
        return Err(ProtocolError::Invalid("function arguments"));
    }
    Ok(args)
}
