//! Client-carried, authenticated context for a converted generation.
//! The cluster stores no conversation plaintext. Authorizing the source deployment
//! and checking that the visible projection still matches are required on every use.
pub(crate) mod citations;

use super::{
    Endpoint, ProtocolError,
    convert::{Result, array, fields, text},
};
use crate::control::snapshot::Deployment;
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use crypto_secretbox::{
    Nonce, XSalsa20Poly1305,
    aead::{Aead, AeadCore, KeyInit, OsRng},
};
use hkdf::Hkdf;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, sync::Arc};
use uuid::Uuid;
use zeroize::Zeroizing;

pub(crate) const PREFIX: &str = "nxctx1.";
fn invalid() -> ProtocolError {
    ProtocolError::Invalid("private history")
}

pub(crate) struct HistoryKey(XSalsa20Poly1305);
impl HistoryKey {
    pub fn new(private: &crypto_box::SecretKey, cluster: Uuid) -> Self {
        let secret = Zeroizing::new(private.to_bytes());
        let kdf = Hkdf::<Sha256>::new(Some(cluster.as_bytes()), secret.as_ref());
        let mut key = Zeroizing::new([0u8; 32]);
        kdf.expand(b"nexus/relay/converted-history/v1", &mut key[..])
            .expect("32-byte HKDF key");
        Self(XSalsa20Poly1305::new_from_slice(&key[..]).expect("32-byte history key"))
    }

    fn open(&self, token: &str) -> Result<Packet> {
        let bytes = URL_SAFE_NO_PAD
            .decode(token.strip_prefix(PREFIX).ok_or_else(invalid)?)
            .map_err(|_| invalid())?;
        let (nonce, cipher) = bytes.split_at_checked(24).ok_or_else(invalid)?;
        let plain = Zeroizing::new(
            self.0
                .decrypt(Nonce::from_slice(nonce), cipher)
                .map_err(|_| invalid())?,
        );
        serde_json::from_slice(&plain).map_err(|_| invalid())
    }
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
struct Binding {
    key: Uuid,
    model: Uuid,
    deployment: Uuid,
    fingerprint: [u8; 32],
    client: Endpoint,
}
impl Binding {
    fn new(key: Uuid, model: Uuid, d: &Deployment, client: Endpoint) -> Self {
        let identity = json!([
            d.credential_id,
            d.credential_version,
            d.upstream_version,
            d.upstream_model,
            d.base_url.as_str(),
            d.region,
            d.protocol_family(),
            d.is_bedrock(),
            d.invoke()
        ]);
        Self {
            key,
            model,
            deployment: d.id,
            client,
            fingerprint: Sha256::digest(identity.to_string().as_bytes()).into(),
        }
    }
}
#[derive(Serialize, Deserialize)]
struct Packet {
    #[serde(default)]
    hide_summary: bool,
    binding: Binding,
    id: String,
    original: Value,
}

#[derive(Clone)]
pub(crate) struct Writer {
    pub hide_summary: bool,
    key: Arc<HistoryKey>,
    binding: Binding,
}
pub(crate) fn item_id(rid: &str, index: usize, compact: bool) -> String {
    let tag = if compact { "cmp" } else { "rs" };
    let hash = Sha256::digest(json!([rid, index]).to_string().as_bytes());
    format!("{tag}_nx_{:.32}", format!("{hash:x}"))
}
impl Writer {
    pub fn new(
        key: Arc<HistoryKey>,
        enterprise: Uuid,
        model: Uuid,
        d: &Deployment,
        client: Endpoint,
    ) -> Self {
        Self {
            key,
            binding: Binding::new(enterprise, model, d, client),
            hide_summary: false,
        }
    }
    pub fn project(&self, original: &Value, rid: &str, index: usize) -> Result<Value> {
        let mut group = self.project_group(original, rid, index)?;
        if group.len() != 1 {
            return Err(invalid());
        }
        Ok(group.pop().unwrap())
    }
    pub fn project_group(&self, original: &Value, rid: &str, index: usize) -> Result<Vec<Value>> {
        let id = if original["type"] == "text" {
            super::convert::response::item_id(rid, index, false)
        } else {
            item_id(rid, index, original["type"] == "compaction")
        };
        let packet = Packet {
            hide_summary: self.hide_summary,
            binding: self.binding.clone(),
            id,
            original: original.clone(),
        };
        let plain = Zeroizing::new(serde_json::to_vec(&packet).map_err(|_| invalid())?);
        let nonce = XSalsa20Poly1305::generate_nonce(&mut OsRng);
        let cipher = self
            .key
            .0
            .encrypt(&nonce, &plain[..])
            .map_err(|_| invalid())?;
        let mut bytes = Vec::with_capacity(nonce.len() + cipher.len());
        bytes.extend_from_slice(nonce.as_slice());
        bytes.extend(cipher);
        packet.group(format!("{PREFIX}{}", URL_SAFE_NO_PAD.encode(bytes)))
    }
}

impl Packet {
    fn group(&self, token: String) -> Result<Vec<Value>> {
        match (self.binding.client, self.original["type"].as_str()) {
            (Endpoint::OpenaiResponses, Some("text")) => Ok(vec![citations::to_responses(
                &self.original,
                &self.id,
                &token,
            )?]),
            (Endpoint::AnthropicMessages, Some("message")) => {
                citations::to_messages(&self.original, &token)
            }
            _ => Ok(vec![self.visible(token)?]),
        }
    }
    fn visible(&self, token: String) -> Result<Value> {
        let v = &self.original;
        match (self.binding.client, v["type"].as_str()) {
            (Endpoint::OpenaiResponses, Some("thinking" | "redacted_thinking")) => {
                let summary = if v["type"] == "thinking" {
                    fields(v, &["type", "thinking", "signature"])?;
                    if let Some(s) = v.get("signature")
                        && !s.is_null()
                        && !s.is_string()
                    {
                        return Err(invalid());
                    }
                    let shown = text(v, "thinking")?;
                    if self.hide_summary {
                        vec![]
                    } else {
                        vec![json!({"type":"summary_text","text":shown})]
                    }
                } else {
                    fields(v, &["type", "data"])?;
                    if text(v, "data")?.is_empty() {
                        return Err(invalid());
                    }
                    vec![]
                };
                Ok(
                    json!({"type":"reasoning","id":self.id,"summary":summary,"status":"completed","encrypted_content":token}),
                )
            }
            (Endpoint::OpenaiResponses, Some("compaction")) => {
                fields(
                    v,
                    &[
                        "type",
                        "content",
                        "signature",
                        "encrypted_content",
                        "tool_changes",
                    ],
                )?;
                if text(v, "content")?.is_empty()
                    || v.get("tool_changes")
                        .is_some_and(|t| !t.is_null() && !t.is_array())
                    || ["signature", "encrypted_content"]
                        .iter()
                        .any(|k| v.get(*k).is_some_and(|v| !v.is_null() && !v.is_string()))
                {
                    return Err(invalid());
                }
                Ok(json!({"type":"compaction","id":self.id,"encrypted_content":token}))
            }
            (Endpoint::AnthropicMessages, Some("compaction")) => {
                fields(v, &["type", "id", "encrypted_content", "created_by"])?;
                super::convert::id(v, "id")?;
                if text(v, "encrypted_content")?.is_empty()
                    || v.get("created_by")
                        .is_some_and(|x| !x.is_null() && !x.is_string())
                {
                    return Err(invalid());
                }
                // The provider exposes no plaintext summary. This block carries the
                // original item opaquely, including through SDKs that discard extra fields.
                Ok(json!({"type":"redacted_thinking","data":token,"nexus_item_type":"compaction"}))
            }
            (Endpoint::AnthropicMessages, Some("reasoning")) => {
                fields(
                    v,
                    &[
                        "type",
                        "id",
                        "summary",
                        "content",
                        "encrypted_content",
                        "status",
                        "role",
                        "quality",
                        "size",
                    ],
                )?;
                if ["role", "quality", "size"]
                    .iter()
                    .any(|k| v.get(*k).is_some_and(|s| !s.is_null() && s != ""))
                {
                    return Err(invalid());
                }
                super::convert::id(v, "id")?;
                if v.get("encrypted_content")
                    .is_some_and(|c| !c.is_null() && !c.is_string())
                    || v.get("status")
                        .is_some_and(|s| !s.is_null() && s != "completed" && s != "incomplete")
                {
                    return Err(invalid());
                }
                if let Some(c) = v.get("content").filter(|c| !c.is_null()) {
                    for part in array(c, "reasoning content")? {
                        fields(part, &["type", "text"])?;
                        if part["type"] != "reasoning_text" || !part["text"].is_string() {
                            return Err(invalid());
                        }
                    }
                }
                // Only published summary parts enter the displayed thinking field.
                // Provider ciphertext and any provider-specific reasoning content remain opaque.
                let summary = array(&v["summary"], "reasoning summary")?
                    .iter()
                    .map(|p| {
                        fields(p, &["type", "text"])?;
                        if p["type"] != "summary_text" {
                            return Err(invalid());
                        }
                        text(p, "text")
                    })
                    .collect::<Result<Vec<_>>>()?
                    .join("\n\n");
                if self.hide_summary || summary.is_empty() {
                    Ok(json!({"type":"redacted_thinking","data":token}))
                } else {
                    Ok(json!({"type":"thinking","thinking":summary,"signature":token}))
                }
            }
            _ => Err(ProtocolError::Unsupported("private context projection")),
        }
    }
}

pub(crate) fn reserved(item: &Value) -> bool {
    anchor(item).is_some()
        || item.get("nexus_history").is_some_and(|v| !v.is_null())
        || item.get("nexus_citations").is_some_and(|v| !v.is_null())
        || item["id"]
            .as_str()
            .is_some_and(|s| s.starts_with("rs_nx_") || s.starts_with("cmp_nx_"))
        || item["citations"].as_array().is_some_and(|c| {
            c.iter().any(|v| {
                v["encrypted_index"]
                    .as_str()
                    .is_some_and(|s| s.starts_with("nxref1."))
            })
        })
}
/// A capsule issued to a Responses client also carries its ownership tag. Other protocols
/// do not remove that tag, so recognize the capsule inside it and refuse it as history.
fn capsule(s: &str) -> &str {
    s.strip_prefix(super::ownership::BLOB)
        .and_then(|rest| rest.get(super::ownership::TAG + 1..))
        .filter(|inner| inner.starts_with(PREFIX))
        .unwrap_or(s)
}
fn anchor(item: &Value) -> Option<&str> {
    if let Some(s) = item["nexus_history"].as_str() {
        return Some(s);
    }
    let field = match item["type"].as_str()? {
        "thinking" => "signature",
        "redacted_thinking" => "data",
        "reasoning" | "compaction" => "encrypted_content",
        "text" => {
            return item["citations"]
                .as_array()?
                .iter()
                .filter_map(|v| v["encrypted_index"].as_str())
                .find(|s| s.starts_with(PREFIX));
        }
        _ => return None,
    };
    item[field]
        .as_str()
        .map(capsule)
        .filter(|s| s.starts_with(PREFIX))
}

fn matches_projection(expected: &Value, actual: &Value) -> bool {
    match (expected, actual) {
        (Value::Object(e), Value::Object(a)) => {
            e.iter().all(|(k, v)| match a.get(k) {
                Some(x) if k == "status" && x.is_null() => true,
                Some(x) => matches_projection(v, x),
                None => matches!(
                    k.as_str(),
                    "status"
                        | "nexus_item_type"
                        | "nexus_citations"
                        | "nexus_annotations"
                        | "nexus_start_index"
                        | "nexus_end_index"
                ),
            }) && a.iter().all(|(k, v)| e.contains_key(k) || v.is_null())
        }
        (Value::Array(e), Value::Array(a)) => {
            e.len() == a.len() && e.iter().zip(a).all(|(e, a)| matches_projection(e, a))
        }
        _ => expected == actual,
    }
}

#[derive(Default)]
pub(crate) struct Restored {
    items: BTreeMap<(usize, usize), (Value, usize)>,
    pub binding: Option<(Uuid, Uuid)>,
}
impl std::fmt::Debug for Restored {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Restored")
            .field("items", &self.items.len())
            .finish()
    }
}
impl Restored {
    pub fn get(&self, turn: usize, part: usize) -> Option<&(Value, usize)> {
        self.items.get(&(turn, part))
    }
    pub fn originals(&mut self) -> impl Iterator<Item = &mut Value> {
        self.items.values_mut().map(|(v, _)| v)
    }
    pub fn changes_tools(&self) -> bool {
        self.items.values().any(|(v, _)| {
            v["tool_changes"]
                .as_array()
                .is_some_and(|c| c.iter().any(|x| x["type"] == "tool_addition"))
        })
    }
    pub fn validate_tools(&self, body: &Value) -> Result<()> {
        // A sealed receipt establishes provenance, not authority for new hosted
        // operations. The converted deployment supports client functions here.
        let mut names: std::collections::HashSet<&str> = body["tools"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|t| t["type"] == "function")
            .filter_map(|t| t["name"].as_str())
            .collect();
        for (original, _) in self.items.values() {
            let Some(changes) = original["tool_changes"].as_array() else {
                continue;
            };
            for edit in changes {
                fields(edit, &["type", "tool"])?;
                let t = &edit["tool"];
                match (edit["type"].as_str(), t["type"].as_str()) {
                    (Some("tool_removal"), Some(_)) => {
                        if let Some(name) = t["name"].as_str() {
                            names.remove(name);
                        }
                    }
                    (Some("tool_addition"), Some("tool_reference"))
                        if names.contains(text(t, "name")?) => {}
                    (Some("tool_addition"), Some("tool_definition")) => {
                        fields(t, &["type", "definition"])?;
                        let d = &t["definition"];
                        fields(
                            d,
                            &["type", "name", "description", "input_schema", "strict"],
                        )?;
                        if d.get("type").is_some_and(|v| !v.is_null() && v != "custom")
                            || d["input_schema"]["type"] != "object"
                            || d.get("strict")
                                .is_some_and(|v| !v.is_null() && !v.is_boolean())
                        {
                            return Err(invalid());
                        }
                        names.insert(super::convert::id(d, "name")?);
                    }
                    _ => {
                        return Err(ProtocolError::Unsupported(
                            "compacted tool requires a native declaration",
                        ));
                    }
                }
            }
        }
        Ok(())
    }
    pub fn has_file_annotations(&self) -> bool {
        self.items
            .values()
            .any(|(v, _)| citations::resource_annotations(v))
    }
    pub fn has_compaction(&self) -> bool {
        self.items
            .values()
            .any(|(item, _)| item["type"] == "compaction")
    }
    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    pub fn read(
        &mut self,
        parts: &[Value],
        turn: usize,
        codec: &HistoryKey,
        index: &crate::control::snapshot::Index,
        enterprise: Uuid,
        client: Endpoint,
    ) -> Result<()> {
        let mut covered = vec![false; parts.len()];
        for (i, item) in parts.iter().enumerate() {
            let Some(token) = anchor(item) else {
                continue;
            };
            let packet = codec.open(token)?;
            let b = &packet.binding;
            let d = index.deployment(b.deployment).ok_or_else(invalid)?;
            if b != &Binding::new(enterprise, b.model, d, client) {
                return Err(invalid());
            }
            let next = (b.model, b.deployment);
            if self.binding.is_some_and(|old| old != next) {
                return Err(invalid());
            }
            let group = packet.group(token.to_owned())?;
            let loc = group
                .iter()
                .position(|v| anchor(v) == Some(token))
                .ok_or_else(invalid)?;
            let start = i.checked_sub(loc).ok_or_else(invalid)?;
            let end = start
                .checked_add(group.len())
                .filter(|n| *n <= parts.len())
                .ok_or_else(invalid)?;
            if covered[start..end].contains(&true)
                || !group
                    .iter()
                    .zip(&parts[start..end])
                    .all(|(e, a)| matches_projection(e, a))
            {
                return Err(invalid());
            }
            covered[start..end].fill(true);
            self.binding = Some(next);
            self.items
                .insert((turn, start), (packet.original, group.len()));
        }
        if parts
            .iter()
            .zip(covered)
            .any(|(p, seen)| reserved(p) && !seen)
        {
            return Err(invalid());
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn each_issue_uses_a_fresh_nonce_and_only_the_original_cluster_key_opens_it() {
        let private = crypto_box::SecretKey::from([35u8; 32]);
        let cluster = Uuid::new_v4();
        let writer = Writer {
            hide_summary: false,
            key: Arc::new(HistoryKey::new(&private, cluster)),
            binding: Binding {
                key: Uuid::new_v4(),
                model: Uuid::new_v4(),
                deployment: Uuid::new_v4(),
                fingerprint: [1; 32],
                client: Endpoint::OpenaiResponses,
            },
        };
        let original =
            json!({"type":"thinking","thinking":"summary","signature":"opaque native evidence"});
        let one = writer.project(&original, "r", 0).unwrap();
        let two = writer.project(&original, "r", 0).unwrap();
        assert_eq!(one["id"], two["id"]);
        let token = one["encrypted_content"].as_str().unwrap();
        assert_ne!(token, two["encrypted_content"].as_str().unwrap());
        assert_eq!(
            HistoryKey::new(&private, cluster)
                .open(token)
                .unwrap()
                .original,
            original
        );
        assert!(
            HistoryKey::new(&private, Uuid::new_v4())
                .open(token)
                .is_err()
        );
        assert!(
            HistoryKey::new(&crypto_box::SecretKey::from([38; 32]), cluster)
                .open(token)
                .is_err()
        );
    }
    #[test]
    fn restoring_tool_changes_checks_current_declarations() {
        fn restored(tool: Value) -> Restored {
            let mut r = Restored::default();
            r.items.insert((0,0),(json!({"type":"compaction","tool_changes":[{"type":"tool_addition","tool":tool}]}),1));
            r
        }
        let named = restored(json!({"type":"tool_reference","name":"external"}));
        assert!(named.validate_tools(&json!({})).is_err());
        assert!(
            named
                .validate_tools(&json!({"tools":[{"type":"function","name":"external"}]}))
                .is_ok()
        );
        let hosted = restored(
            json!({"type":"tool_definition","definition":{"type":"web_search_20250305","name":"web_search","input_schema":{"type":"object"}}}),
        );
        assert!(hosted.validate_tools(&json!({})).is_err());
    }
}
