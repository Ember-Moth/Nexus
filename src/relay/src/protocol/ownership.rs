//! Provider item identifiers and reasoning ciphertext are bound to the issuing API key.
//!
//! Deployments share one upstream account, which resolves item IDs and decrypts
//! `encrypted_content` for every tenant alike. Responses output therefore leaves the node
//! with each item ID and ciphertext tagged by a keyed MAC over the owning API key. Input
//! must carry a tag that verifies for the calling key; the tag is removed before the
//! upstream sees the item. Unsealed provider references are refused before reservation,
//! and unsealed IDs that are optional on full-content items are dropped.
use super::ProtocolError;
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use hkdf::Hkdf;
use serde_json::Value;
use sha2::Sha256;
use uuid::Uuid;
use zeroize::Zeroizing;

const ID: &str = "nxo_";
pub(crate) const BLOB: &str = "nxo1.";
pub(crate) const TAG: usize = 22;

pub struct OwnershipKey(Zeroizing<[u8; 32]>);

impl OwnershipKey {
    pub fn new(private: &crypto_box::SecretKey, cluster: Uuid) -> Self {
        let secret = Zeroizing::new(private.to_bytes());
        let kdf = Hkdf::<Sha256>::new(Some(cluster.as_bytes()), secret.as_ref());
        let mut key = Zeroizing::new([0u8; 32]);
        kdf.expand(b"nexus/relay/item-ownership/v1", &mut key[..])
            .expect("32-byte HKDF key");
        Self(key)
    }

    /// HKDF-Expand over one block is HMAC-SHA256(key, info || 0x01), a keyed PRF.
    fn tag(&self, kind: &[u8], key: Uuid, value: &str) -> String {
        let prk = Hkdf::<Sha256>::from_prk(&self.0[..]).expect("32-byte PRK");
        let mut out = [0u8; 16];
        prk.expand_multi_info(&[kind, key.as_bytes(), value.as_bytes()], &mut out)
            .expect("16-byte tag");
        URL_SAFE_NO_PAD.encode(out)
    }

    pub fn seal_id(&self, key: Uuid, id: &str) -> String {
        format!("{ID}{}_{id}", self.tag(b"id\0", key, id))
    }
    pub fn seal_blob(&self, key: Uuid, blob: &str) -> String {
        format!("{BLOB}{}.{blob}", self.tag(b"blob\0", key, blob))
    }

    fn open<'a>(
        &self,
        prefix: &str,
        sep: char,
        kind: &[u8],
        key: Uuid,
        s: &'a str,
    ) -> Option<&'a str> {
        let rest = s.strip_prefix(prefix)?;
        let (tag, value) = rest.split_at_checked(TAG)?;
        let value = value.strip_prefix(sep)?;
        (!value.is_empty() && constant_eq(tag.as_bytes(), self.tag(kind, key, value).as_bytes()))
            .then_some(value)
    }
    pub fn open_id<'a>(&self, key: Uuid, s: &'a str) -> Option<&'a str> {
        self.open(ID, '_', b"id\0", key, s)
    }
    pub fn open_blob<'a>(&self, key: Uuid, s: &'a str) -> Option<&'a str> {
        self.open(BLOB, '.', b"blob\0", key, s)
    }
}

fn constant_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// Seal every item ID, `item_id` reference and reasoning ciphertext in an outgoing
/// Responses value: a response, a stream event, an item or a list of items.
pub fn seal(value: &mut Value, owner: &OwnershipKey, key: Uuid) {
    match value {
        Value::Object(map) => {
            let item = map.get("type").is_some_and(Value::is_string);
            for (k, v) in map.iter_mut() {
                match (k.as_str(), &*v) {
                    ("id", Value::String(id)) if item && !id.starts_with(ID) => {
                        *v = owner.seal_id(key, id).into();
                    }
                    ("item_id" | "approval_request_id", Value::String(id))
                        if !id.starts_with(ID) =>
                    {
                        *v = owner.seal_id(key, id).into();
                    }
                    ("encrypted_content", Value::String(blob)) if !blob.starts_with(BLOB) => {
                        *v = owner.seal_blob(key, blob).into();
                    }
                    _ => seal(v, owner, key),
                }
            }
        }
        Value::Array(items) => items.iter_mut().for_each(|v| seal(v, owner, key)),
        _ => {}
    }
}

fn foreign() -> ProtocolError {
    ProtocolError::Invalid("item ownership")
}

/// Verify and remove ownership tags from Responses input items.
pub fn open(items: &mut Value, owner: &OwnershipKey, key: Uuid) -> Result<(), ProtocolError> {
    for item in items.as_array_mut().into_iter().flatten() {
        let Some(map) = item.as_object_mut() else {
            continue;
        };
        let mut unsealed_id = false;
        if let Some(Value::String(id)) = map.get_mut("id") {
            if id.starts_with(ID) {
                *id = owner.open_id(key, id).ok_or_else(foreign)?.to_owned();
            } else {
                unsealed_id = true;
            }
        }
        if let Some(Value::String(id)) = map.get_mut("approval_request_id") {
            *id = owner.open_id(key, id).ok_or_else(foreign)?.to_owned();
        }
        if let Some(Value::String(blob)) = map.get_mut("encrypted_content") {
            if blob.starts_with(BLOB) {
                *blob = owner.open_blob(key, blob).ok_or_else(foreign)?.to_owned();
            } else if !blob.starts_with(super::history::PREFIX) {
                // Provider ciphertext without a tag may belong to another tenant.
                return Err(foreign());
            }
        }
        if unsealed_id && !super::history::reserved(item) {
            let map = item.as_object_mut().expect("object item");
            match map.get("type").and_then(Value::as_str) {
                // These resolve the ID against the provider account's stored state.
                None | Some("item_reference" | "reasoning" | "compaction") => {
                    return Err(foreign());
                }
                // The ID of a full-content item is optional and only names the item.
                Some(_) => {
                    map.remove("id");
                }
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn owner() -> OwnershipKey {
        OwnershipKey::new(&crypto_box::SecretKey::from([7u8; 32]), Uuid::nil())
    }

    #[test]
    fn output_items_are_sealed_for_one_key_and_open_only_for_it() {
        let o = owner();
        let (mine, other) = (Uuid::new_v4(), Uuid::new_v4());
        let mut response = json!({"id":"resp_1","object":"response","output":[
            {"type":"reasoning","id":"rs_1","summary":[],"encrypted_content":"gAAAA"},
            {"type":"message","id":"msg_1","role":"assistant","content":[
                {"type":"output_text","text":"hi","annotations":[]}]},
            {"type":"mcp_approval_request","id":"mcpr_1","name":"t"}]});
        seal(&mut response, &o, mine);
        assert_eq!(response["id"], "resp_1");
        let id = response["output"][0]["id"].as_str().unwrap().to_owned();
        assert!(id.starts_with("nxo_") && id.ends_with("_rs_1"));
        let again = {
            let mut v = json!({"type":"response.output_text.delta","item_id":"rs_1"});
            seal(&mut v, &o, mine);
            v
        };
        assert_eq!(again["item_id"], id);
        let mut sealed = response.clone();
        seal(&mut sealed, &o, mine);
        assert_eq!(sealed, response);
        let mut input = response["output"].clone();
        input
            .as_array_mut()
            .unwrap()
            .push(json!({"type":"mcp_approval_response",
            "approval_request_id":response["output"][2]["id"],"approve":true}));
        let mut theirs = input.clone();
        open(&mut input, &o, mine).unwrap();
        assert_eq!(input[0]["id"], "rs_1");
        assert_eq!(input[0]["encrypted_content"], "gAAAA");
        assert_eq!(input[3]["approval_request_id"], "mcpr_1");
        assert!(open(&mut theirs, &o, other).is_err());
    }

    #[test]
    fn unsealed_provider_references_are_refused_or_dropped() {
        let o = owner();
        let key = Uuid::new_v4();
        for item in [
            json!({"type":"reasoning","id":"rs_x","summary":[]}),
            json!({"type":"reasoning","id":"rs_x","summary":[],"encrypted_content":"gAAAA"}),
            json!({"type":"compaction","encrypted_content":"gAAAA"}),
            json!({"type":"item_reference","id":"msg_x"}),
            json!({"id":"msg_x"}),
            json!({"type":"mcp_approval_response","approval_request_id":"mcpr_x","approve":true}),
        ] {
            assert!(open(&mut json!([item]), &o, key).is_err());
        }
        let mut items = json!([
            {"type":"message","id":"msg_x","role":"assistant","content":"hi"},
            {"type":"function_call","id":"fc_x","call_id":"c","name":"f","arguments":"{}"},
            {"type":"message","role":"user","content":"next"}]);
        open(&mut items, &o, key).unwrap();
        assert!(
            items
                .as_array()
                .unwrap()
                .iter()
                .all(|i| i.get("id").is_none())
        );
        let mut forged = json!([{"type":"reasoning","summary":[],
            "id":format!("nxo_{}_rs_x", "A".repeat(22))}]);
        assert!(open(&mut forged, &o, key).is_err());
    }
}
