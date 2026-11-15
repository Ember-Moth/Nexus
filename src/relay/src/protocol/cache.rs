//! Request-local cache hints and stable routing for an authorized deployment.
use super::{Endpoint, convert};
use serde_json::Value;
use sha2::{Digest, Sha256};
use uuid::Uuid;

pub fn valid_key(s: &str) -> bool {
    !s.is_empty() && s.len() <= 256 && !s.chars().any(char::is_control)
}

/// Scope the client workflow, shared template or first user turn to the authenticated tenant.
pub fn affinity(
    org: Uuid,
    model: Uuid,
    endpoint: Endpoint,
    body: &Value,
    header: Option<&str>,
    derive_from_prompt: bool,
) -> Option<[u8; 32]> {
    if let Some(key) = header.or_else(|| {
        if endpoint != Endpoint::OpenaiResponses {
            return None;
        }
        body.get("prompt_cache_key")
            .and_then(Value::as_str)
            .filter(|s| valid_key(s))
    }) {
        return Some(key_digest(org, model, key));
    }
    if !derive_from_prompt {
        return None;
    }
    let mut hash = Sha256::new();
    hash.update(b"nexus:prompt-affinity:v1");
    hash.update(org.as_bytes());
    hash.update(model.as_bytes());
    // Share cache placement across unrelated queries using the same tenant/template.
    // A short or generic system preamble alone is less useful than the first user turn.
    let system = match endpoint {
        Endpoint::AnthropicMessages => body.get("system"),
        Endpoint::OpenaiResponses => body.get("instructions").or_else(|| {
            body["input"]
                .as_array()?
                .first()
                .filter(|m| matches!(m["role"].as_str(), Some("developer" | "system")))?
                .get("content")
        }),
    };
    let tools = body
        .get("tools")
        .filter(|v| v.as_array().is_some_and(|a| !a.is_empty()));
    let mut template = Vec::new();
    for section in [system, tools] {
        serde_json::to_writer(&mut template, &section).expect("serialize JSON");
    }
    if template.len() >= 1024 {
        hash.update(b"template");
        hash.update(&template);
        return Some(hash.finalize().into());
    }
    let history = match endpoint {
        Endpoint::AnthropicMessages => &body["messages"],
        Endpoint::OpenaiResponses => &body["input"],
    };
    let first = if history.is_string() {
        history
    } else {
        history
            .as_array()?
            .iter()
            .find(|v| v["role"] == "user")?
            .get("content")?
    };
    hash.update(b"first-turn");
    serde_json::to_writer(&mut hash, first).expect("hash JSON");
    Some(hash.finalize().into())
}

/// Weighted rendezvous: minimize -ln(U)/weight. Independent of iteration order and node RNG.
pub fn score(key: &[u8; 32], deployment: Uuid, weight: u32) -> f64 {
    let mut h = Sha256::new();
    h.update(key);
    h.update(deployment.as_bytes());
    let bytes = h.finalize();
    let n = u64::from_be_bytes(bytes[..8].try_into().unwrap()) >> 12;
    let u = (n as f64 + 0.5) / (1_u64 << 52) as f64;
    -u.ln() / f64::from(weight)
}

fn key_digest(org: Uuid, model: Uuid, value: &str) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update(b"nexus:provider-cache-key:v1");
    h.update(org.as_bytes());
    h.update(model.as_bytes());
    h.update(value.as_bytes());
    h.finalize().into()
}

pub fn provider_key(seed: [u8; 32]) -> String {
    // A distinct domain for the upstream-visible value and the routing rendezvous.
    format!("{:x}", Sha256::digest(seed))
}

pub fn scoped_key(org: Uuid, model: Uuid, value: &str) -> String {
    provider_key(key_digest(org, model, value))
}

pub fn to_responses(body: &Value) -> convert::Result<Value> {
    convert::request::convert_cached(Endpoint::AnthropicMessages, body)
}
pub fn to_messages(body: &Value) -> convert::Result<Value> {
    convert::request::convert_cached(Endpoint::OpenaiResponses, body)
}

pub fn has_messages_breakpoint(body: &Value) -> bool {
    body.get("cache_control").is_some()
        || ["tools", "system"].iter().any(|k| {
            body[k]
                .as_array()
                .is_some_and(|a| a.iter().any(|v| v.get("cache_control").is_some()))
        })
        || body["messages"].as_array().is_some_and(|a| {
            a.iter().any(|m| {
                m["content"]
                    .as_array()
                    .is_some_and(|p| p.iter().any(|v| v.get("cache_control").is_some()))
            })
        })
}
