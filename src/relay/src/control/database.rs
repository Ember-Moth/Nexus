use std::io::{Error, ErrorKind};
use std::{future::Future, time::Duration};

use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use sqlx::{PgPool, postgres::PgPoolOptions, types::Json};
use uuid::Uuid;

use super::{
    ControlError,
    credential::{CredentialRef, Envelope},
    pg_base64,
};
use crate::{
    metrics::{self, Dependency},
    protocol::tokens::ReservationBounds,
};

#[derive(Clone)]
pub struct Gateway {
    pool: PgPool,
}

#[derive(Deserialize)]
struct SnapshotReply {
    applied_revision: String,
    node_id: Uuid,
    sha256: String,
    bytes: String,
}

pub struct Snapshot {
    pub applied_revision: i64,
    pub node_id: Uuid,
    pub cluster_id: Uuid,
    pub revision: i64,
    pub digest: String,
    pub resources: Value,
    pub required_capabilities: Vec<String>,
}

impl Gateway {
    pub async fn connect(
        url: &str,
        max_connections: u32,
        timeout: Duration,
    ) -> Result<Self, ControlError> {
        if max_connections == 0 || timeout.is_zero() {
            return Err(ControlError::Invalid("pool limits"));
        }
        let pool = PgPoolOptions::new()
            .max_connections(max_connections)
            .after_connect(|conn, _| {
                Box::pin(async move {
                    sqlx::query("SET statement_timeout = '8s'")
                        .execute(conn)
                        .await?;
                    Ok(())
                })
            })
            .acquire_timeout(timeout)
            .connect(url)
            .await?;
        Ok(Self { pool })
    }

    // statement_timeout covers execution at PostgreSQL. Bound the entire round trip
    // as well: the connection can go silent after it has committed a write.
    async fn call<T>(
        &self,
        operation: &'static str,
        query: impl Future<Output = Result<T, sqlx::Error>>,
    ) -> Result<T, ControlError> {
        let result = match tokio::time::timeout(Duration::from_secs(15), query).await {
            Ok(result) => result.map_err(ControlError::from),
            Err(_) => Err(ControlError::from(sqlx::Error::Io(Error::new(
                ErrorKind::TimedOut,
                "gateway response timed out",
            )))),
        };
        if let Err(error) = &result {
            metrics::control_error(Dependency::Database, operation, error.kind());
        }
        result
    }

    pub async fn snapshot(
        &self,
        cluster: Uuid,
        revision: Option<i64>,
    ) -> Result<Snapshot, ControlError> {
        let Json(reply): Json<SnapshotReply> = self
            .call(
                "get_config_snapshot",
                sqlx::query_scalar("SELECT gateway_v1.get_config_snapshot($1)")
                    .bind(revision)
                    .fetch_one(&self.pool),
            )
            .await?;
        let bytes = pg_base64(&reply.bytes)?;
        if format!("{:x}", Sha256::digest(&bytes)) != reply.sha256 {
            return Err(ControlError::Invalid("snapshot digest"));
        }
        #[derive(Deserialize)]
        struct Manifest {
            schema_version: i32,
            cluster_id: Uuid,
            revision: String,
            resources: Value,
            required_capabilities: Vec<String>,
        }
        let manifest: Manifest =
            serde_json::from_slice(&bytes).map_err(|_| ControlError::Invalid("snapshot"))?;
        let rev: i64 = manifest
            .revision
            .parse()
            .map_err(|_| ControlError::Invalid("revision"))?;
        if manifest.schema_version != 1
            || manifest.cluster_id != cluster
            || rev < 1
            || revision.is_some_and(|r| r != rev)
            || !manifest.resources.is_object()
        {
            return Err(ControlError::Invalid("snapshot binding"));
        }
        Ok(Snapshot {
            applied_revision: reply
                .applied_revision
                .parse()
                .map_err(|_| ControlError::Invalid("applied revision"))?,
            node_id: reply.node_id,
            cluster_id: cluster,
            revision: rev,
            digest: reply.sha256,
            resources: manifest.resources,
            required_capabilities: manifest.required_capabilities,
        })
    }

    /// Call only after the consumer has validated and installed all required resources.
    pub async fn acknowledge(
        &self,
        snapshot: &Snapshot,
        supported: &[&str],
        rejection: Option<&str>,
    ) -> Result<(), ControlError> {
        let unsupported = snapshot
            .required_capabilities
            .iter()
            .any(|c| !supported.contains(&c.as_str()));
        let rejection = rejection.or(unsupported.then_some("unsupported_capability"));
        let _: Json<Value> = self
            .call(
                "ack_config",
                sqlx::query_scalar("SELECT gateway_v1.ack_config($1,$2,$3,$4)")
                    .bind(snapshot.revision)
                    .bind(&snapshot.digest)
                    .bind(rejection.is_none())
                    .bind(rejection.unwrap_or(""))
                    .fetch_one(&self.pool),
            )
            .await?;
        if unsupported {
            return Err(ControlError::Invalid("required capability"));
        }
        Ok(())
    }

    pub async fn credential(
        &self,
        revision: i64,
        reference: &CredentialRef,
    ) -> Result<Envelope, ControlError> {
        let Json(envelope) = self
            .call(
                "get_credential_envelope",
                sqlx::query_scalar("SELECT gateway_v1.get_credential_envelope($1,$2,$3)")
                    .bind(revision)
                    .bind(reference.credential_id)
                    .bind(reference.credential_version)
                    .fetch_one(&self.pool),
            )
            .await?;
        Ok(envelope)
    }

    pub async fn reserve(
        &self,
        request: Uuid,
        key: Uuid,
        model: Uuid,
        revision: i64,
        bounds: ReservationBounds,
    ) -> Result<Value, ControlError> {
        let Json(reply) = self
            .call(
                "reserve_request",
                sqlx::query_scalar("SELECT gateway_v1.reserve_request($1,$2,$3,$4,$5,$6)")
                    .bind(request)
                    .bind(key)
                    .bind(model)
                    .bind(revision)
                    .bind(i64::from(bounds.inputs))
                    .bind(i64::from(bounds.outputs))
                    .fetch_one(&self.pool),
            )
            .await?;
        Ok(reply)
    }

    pub async fn start_attempt(
        &self,
        request: Uuid,
        attempt: Uuid,
        sequence: i32,
        deployment: Uuid,
    ) -> Result<Value, ControlError> {
        let Json(reply) = self
            .call(
                "start_attempt",
                sqlx::query_scalar("SELECT gateway_v1.start_attempt($1,$2,$3,$4)")
                    .bind(request)
                    .bind(attempt)
                    .bind(sequence)
                    .bind(deployment)
                    .fetch_one(&self.pool),
            )
            .await?;
        Ok(reply)
    }

    pub async fn status(&self, request: Uuid) -> Result<Option<Value>, ControlError> {
        let reply: Option<Json<Value>> = self
            .call(
                "get_request_status",
                sqlx::query_scalar("SELECT gateway_v1.get_request_status($1)")
                    .bind(request)
                    .fetch_one(&self.pool),
            )
            .await?;
        Ok(reply.map(|j| j.0))
    }

    pub async fn cancel_unstarted(
        &self,
        request: Uuid,
        reason: &str,
    ) -> Result<Value, ControlError> {
        let Json(reply) = self
            .call(
                "cancel_unstarted_request",
                sqlx::query_scalar("SELECT gateway_v1.cancel_unstarted_request($1,$2)")
                    .bind(request)
                    .bind(reason)
                    .fetch_one(&self.pool),
            )
            .await?;
        Ok(reply)
    }

    /// Replay exactly this serialized envelope after an unknown commit outcome.
    pub async fn append_event(&self, event: &str) -> Result<Value, ControlError> {
        let Json(reply) = self
            .call(
                "append_usage_event",
                sqlx::query_scalar("SELECT gateway_v1.append_usage_event($1)")
                    .bind(event)
                    .fetch_one(&self.pool),
            )
            .await?;
        Ok(reply)
    }
}
