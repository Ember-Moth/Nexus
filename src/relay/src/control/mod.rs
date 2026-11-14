//! The v1 gateway principal can execute only its cluster-scoped SQL contracts.
pub mod credential;
pub mod database;

use base64::{Engine, engine::general_purpose::STANDARD};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ControlError {
    #[error("invalid control-plane {0}")]
    Invalid(&'static str),
    #[error("gateway database operation failed")]
    Database(#[from] sqlx::Error),
}

// PostgreSQL encode(bytea, 'base64') inserts line feeds at 76 columns.
fn pg_base64(input: &str) -> Result<Vec<u8>, ControlError> {
    let encoded: Vec<u8> = input
        .bytes()
        .filter(|b| *b != b'\n' && *b != b'\r')
        .collect();
    STANDARD
        .decode(encoded)
        .map_err(|_| ControlError::Invalid("base64"))
}
