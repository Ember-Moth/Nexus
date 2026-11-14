use crypto_box::SecretKey;
use serde::Deserialize;
use uuid::Uuid;
use zeroize::Zeroizing;

use super::{ControlError, pg_base64};

#[derive(Deserialize)]
pub struct CredentialRef {
    pub credential_id: Uuid,
    pub credential_version: i32,
    pub key_version: i32,
    pub algorithm: String,
}

#[derive(Deserialize)]
pub struct Envelope {
    algorithm: String,
    key_version: i32,
    ciphertext: String,
}

#[derive(Deserialize)]
struct Payload {
    cluster_id: Uuid,
    credential_id: Uuid,
    credential_version: i32,
    key_version: i32,
    secret: String,
}

impl Envelope {
    pub fn open(
        &self,
        cluster: Uuid,
        reference: &CredentialRef,
        key: &SecretKey,
    ) -> Result<Zeroizing<String>, ControlError> {
        if self.algorithm != "libsodium-sealed-box"
            || reference.algorithm != self.algorithm
            || self.key_version != reference.key_version
        {
            return Err(ControlError::Invalid("envelope"));
        }
        let ciphertext = pg_base64(&self.ciphertext)?;
        let plain = Zeroizing::new(
            key.unseal(&ciphertext)
                .map_err(|_| ControlError::Invalid("ciphertext"))?,
        );
        let data: Payload = serde_json::from_slice(&plain)
            .map_err(|_| ControlError::Invalid("credential payload"))?;
        let secret = Zeroizing::new(data.secret);
        if data.cluster_id != cluster
            || data.credential_id != reference.credential_id
            || data.credential_version != reference.credential_version
            || data.key_version != reference.key_version
            || secret.is_empty()
            || secret.len() > 4096
            || !secret.bytes().all(|b| b.is_ascii_graphic())
        {
            return Err(ControlError::Invalid("credential binding"));
        }
        Ok(secret)
    }
}
