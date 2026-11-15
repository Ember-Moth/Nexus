//! The v1 gateway principal can execute only its cluster-scoped SQL contracts.
pub(crate) mod batches;
pub mod credential;
pub mod database;
pub(crate) mod execution;
pub mod limits;
pub(crate) mod purges;
pub(crate) mod resources;
pub mod snapshot;
pub(crate) mod stored;
pub(crate) mod vectors;

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

impl ControlError {
    pub fn database_message(&self) -> Option<&str> {
        if let Self::Database(sqlx::Error::Database(db)) = self {
            (db.code().as_deref() == Some("P0001")).then(|| db.message())
        } else {
            None
        }
    }
    /// A short reason for logs: a gateway contract error code (SQLSTATE P0001, raised
    /// by `gateway_v1` with a fixed code) or a fixed description. Never driver text.
    pub fn reason(&self) -> &str {
        match self {
            Self::Invalid(what) => what,
            Self::Database(sqlx::Error::Database(db)) if db.code().as_deref() == Some("P0001") => {
                db.message()
            }
            Self::Database(_) if self.retryable() => "database_unavailable",
            Self::Database(_) => "database_error",
        }
    }
    pub fn kind(&self) -> crate::metrics::ErrorKind {
        match self {
            Self::Database(sqlx::Error::Database(db)) if db.code().as_deref() == Some("P0001") => {
                crate::metrics::ErrorKind::Rejected
            }
            _ => crate::metrics::ErrorKind::Unavailable,
        }
    }
    pub fn retryable(&self) -> bool {
        match self {
            Self::Database(sqlx::Error::Database(db)) => db.code().is_some_and(|s| {
                s.starts_with("08")
                    || matches!(s.as_ref(), "40001" | "40P01" | "57P01" | "57P02" | "57P03")
            }),
            Self::Database(_) => true,
            _ => false,
        }
    }
}
