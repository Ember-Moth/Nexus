//! The cluster's API keys, delivered beside the snapshot instead of inside it.
//!
//! A release names a key sequence and the digest of the key set at that sequence. A node keeps
//! the keys of the sequence it last applied and reads only the net changes since then
//! (`gateway_v1.config_keys`), so an unchanged key set costs nothing to poll and a node holds
//! one compact entry per key.
use std::collections::HashMap;

use chrono::{DateTime, Utc};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use uuid::Uuid;

use super::{ControlError, snapshot::Key};

/// SHA-256 of a presented secret: the lookup key of the table.
pub type SecretDigest = [u8; 32];

/// The `key_set` member of a manifest.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
pub struct KeySet {
    pub sequence: String,
    pub count: usize,
    pub sha256: String,
}

impl KeySet {
    pub fn sequence(&self) -> Result<i64, ControlError> {
        self.sequence
            .parse()
            .ok()
            .filter(|s| *s >= 0)
            .ok_or(ControlError::Invalid("key sequence"))
    }
}

/// One row of `gateway_v1.config_keys`: a key that entered (present) or left the set.
#[derive(Clone, Debug)]
pub struct KeyChange {
    pub sequence: i64,
    pub key: Uuid,
    pub present: bool,
    pub digest: Option<String>,
    pub organization_id: Option<Uuid>,
    pub project_id: Option<Uuid>,
    pub service_account_id: Option<Uuid>,
    pub expires_at: Option<DateTime<Utc>>,
}

#[derive(Default)]
pub struct KeyTable {
    sequence: i64,
    keys: HashMap<SecretDigest, Key>,
    ids: HashMap<Uuid, SecretDigest>,
}

fn parse_digest(hex: &str) -> Result<SecretDigest, ControlError> {
    let bytes = hex.as_bytes();
    if bytes.len() != 64 || !bytes.iter().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')) {
        return Err(ControlError::Invalid("key digest"));
    }
    let mut out = [0u8; 32];
    for (i, pair) in bytes.chunks(2).enumerate() {
        let n = |b: u8| if b <= b'9' { b - b'0' } else { b - b'a' + 10 };
        out[i] = n(pair[0]) << 4 | n(pair[1]);
    }
    Ok(out)
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

impl KeyTable {
    pub fn sequence(&self) -> i64 {
        self.sequence
    }
    pub fn len(&self) -> usize {
        self.keys.len()
    }
    pub fn is_empty(&self) -> bool {
        self.keys.is_empty()
    }
    /// The key whose secret hashes to `digest`, expired or not.
    pub fn get(&self, digest: &SecretDigest) -> Option<&Key> {
        self.keys.get(digest)
    }
    pub fn values(&self) -> impl Iterator<Item = &Key> {
        self.keys.values()
    }

    fn insert(&mut self, digest: SecretDigest, key: Key) -> Result<(), ControlError> {
        if self.ids.contains_key(&key.id) || self.keys.contains_key(&digest) {
            return Err(ControlError::Invalid("duplicate key"));
        }
        self.ids.insert(key.id, digest);
        self.keys.insert(digest, key);
        Ok(())
    }

    /// Apply the changes up to `sequence`, in the order `config_keys` returned them.
    pub fn apply(&mut self, changes: &[KeyChange], sequence: i64) -> Result<(), ControlError> {
        if sequence < self.sequence {
            return Err(ControlError::Invalid("key sequence regression"));
        }
        for change in changes {
            if change.sequence <= self.sequence || change.sequence > sequence {
                return Err(ControlError::Invalid("key change outside range"));
            }
            if change.present {
                let digest = parse_digest(change.digest.as_deref().unwrap_or_default())?;
                let (Some(organization_id), Some(project_id), Some(service_account_id)) = (
                    change.organization_id,
                    change.project_id,
                    change.service_account_id,
                ) else {
                    return Err(ControlError::Invalid("key change"));
                };
                self.insert(
                    digest,
                    Key {
                        id: change.key,
                        organization_id,
                        project_id,
                        service_account_id,
                        expires_at: change.expires_at,
                    },
                )?;
            } else {
                let digest = self
                    .ids
                    .remove(&change.key)
                    .ok_or(ControlError::Invalid("unknown key removed"))?;
                self.keys.remove(&digest);
            }
        }
        self.sequence = sequence;
        Ok(())
    }

    /// The key set digest a release names: SHA-256 over one line per key in id order,
    /// `id, digest, organization, project, service account, expiry` joined by tabs, the
    /// expiry as Unix microseconds or `-`.
    pub fn digest(&self) -> String {
        let mut rows: Vec<_> = self.ids.iter().collect();
        rows.sort_unstable_by_key(|(id, _)| **id);
        let mut hash = Sha256::new();
        for (id, digest) in rows {
            let k = &self.keys[digest];
            let expires = k
                .expires_at
                .map(|t| t.timestamp_micros().to_string())
                .unwrap_or_else(|| "-".into());
            hash.update(
                format!(
                    "{id}\t{}\t{}\t{}\t{}\t{expires}\n",
                    hex(digest),
                    k.organization_id,
                    k.project_id,
                    k.service_account_id
                )
                .as_bytes(),
            );
        }
        format!("{:x}", hash.finalize())
    }

    /// Check the table against the release that names it.
    pub fn verify(&self, set: &KeySet) -> Result<(), ControlError> {
        if set.sequence()? != self.sequence
            || set.count != self.len()
            || set.sha256 != self.digest()
        {
            return Err(ControlError::Invalid("key set"));
        }
        Ok(())
    }

    /// A table from manifest-style key objects (`id`, `digest`, `organization_id`,
    /// `project_id`, optional `service_account_id` and `expires_at`); used by tests and tools.
    pub fn from_verifiers(sequence: i64, keys: serde_json::Value) -> Result<Self, ControlError> {
        #[derive(Deserialize)]
        struct Verifier {
            id: Uuid,
            digest: String,
            organization_id: Uuid,
            project_id: Uuid,
            #[serde(default)]
            service_account_id: Uuid,
            #[serde(default)]
            expires_at: Option<DateTime<Utc>>,
        }
        let keys: Vec<Verifier> =
            serde_json::from_value(keys).map_err(|_| ControlError::Invalid("key schema"))?;
        let mut table = Self {
            sequence,
            ..Self::default()
        };
        for k in keys {
            table.insert(
                parse_digest(&k.digest)?,
                Key {
                    id: k.id,
                    organization_id: k.organization_id,
                    project_id: k.project_id,
                    service_account_id: k.service_account_id,
                    expires_at: k.expires_at,
                },
            )?;
        }
        Ok(table)
    }
}

impl Clone for KeyTable {
    fn clone(&self) -> Self {
        Self {
            sequence: self.sequence,
            keys: self.keys.clone(),
            ids: self.ids.clone(),
        }
    }
}

/// SHA-256 of a presented secret.
pub fn secret_digest(secret: &str) -> SecretDigest {
    Sha256::digest(secret.as_bytes()).into()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn change(sequence: i64, key: Uuid, digest: Option<&str>) -> KeyChange {
        let present = digest.is_some();
        KeyChange {
            sequence,
            key,
            present,
            digest: digest.map(str::to_owned),
            organization_id: present.then(Uuid::nil),
            project_id: present.then(Uuid::nil),
            service_account_id: present.then(Uuid::nil),
            expires_at: None,
        }
    }

    #[test]
    fn changes_apply_in_order_and_the_digest_matches_a_fresh_table() {
        let (a, b) = (Uuid::new_v4(), Uuid::new_v4());
        let da = format!("{:x}", Sha256::digest(b"a"));
        let db = format!("{:x}", Sha256::digest(b"b"));
        let mut table = KeyTable::default();
        table
            .apply(&[change(1, a, Some(&da)), change(1, b, Some(&db))], 1)
            .unwrap();
        assert_eq!(table.get(&secret_digest("a")).unwrap().id, a);
        // b leaves and comes back in one release: removal first, then the new row.
        table
            .apply(
                &[
                    change(2, a, None),
                    change(3, b, None),
                    change(3, b, Some(&db)),
                ],
                3,
            )
            .unwrap();
        assert!(table.get(&secret_digest("a")).is_none());
        assert_eq!(table.len(), 1);
        let fresh = KeyTable::from_verifiers(
            3,
            serde_json::json!([{"id":b,"digest":db,"organization_id":Uuid::nil(),
                "project_id":Uuid::nil(),"service_account_id":Uuid::nil()}]),
        )
        .unwrap();
        assert_eq!(table.digest(), fresh.digest());
        let set = KeySet {
            sequence: "3".into(),
            count: 1,
            sha256: fresh.digest(),
        };
        assert!(table.verify(&set).is_ok());
        assert!(
            table
                .verify(&KeySet {
                    count: 2,
                    ..set.clone()
                })
                .is_err()
        );
    }

    #[test]
    fn out_of_range_or_inconsistent_changes_are_refused() {
        let a = Uuid::new_v4();
        let da = format!("{:x}", Sha256::digest(b"a"));
        let mut table = KeyTable::default();
        assert!(table.apply(&[change(2, a, Some(&da))], 1).is_err());
        assert!(table.apply(&[change(1, a, None)], 1).is_err());
        assert!(table.apply(&[change(1, a, Some("XYZ"))], 1).is_err());
        let mut table = KeyTable::default();
        table.apply(&[change(1, a, Some(&da))], 1).unwrap();
        assert!(table.apply(&[change(1, a, None)], 1).is_err());
        assert!(table.apply(&[], 0).is_err());
    }

    #[test]
    fn the_digest_matches_the_publisher() {
        // key_set_digest() in nexus.modules.publication.application.service.
        let table = KeyTable::from_verifiers(
            1,
            serde_json::json!([{"id":"00000000-0000-0000-0000-000000000002",
                "digest":"aa".repeat(32),"organization_id":"00000000-0000-0000-0000-00000000000a",
                "project_id":"00000000-0000-0000-0000-00000000000b",
                "service_account_id":"00000000-0000-0000-0000-00000000000c",
                "expires_at":"2030-01-02T03:04:05.000006Z"}]),
        )
        .unwrap();
        let line = format!(
            "00000000-0000-0000-0000-000000000002\t{}\t00000000-0000-0000-0000-00000000000a\t\
             00000000-0000-0000-0000-00000000000b\t00000000-0000-0000-0000-00000000000c\t\
             1893553445000006\n",
            "aa".repeat(32)
        );
        assert_eq!(table.digest(), format!("{:x}", Sha256::digest(line)));
    }
}
