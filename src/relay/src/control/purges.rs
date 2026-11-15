//! Tenant resource counts and the queue of upstream copies to delete.
use super::{ControlError, database::Gateway};
use serde::Deserialize;
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

#[derive(Debug, Deserialize)]
pub(crate) struct Usage {
    pub key: u64,
    pub organization: u64,
}

/// A leased purge. The node deletes the upstream copy, then reports the outcome.
#[derive(Debug, Deserialize)]
pub(crate) struct Purge {
    pub id: Uuid,
    pub kind: String,
    pub resource_id: Option<Uuid>,
    pub api_key_id: Uuid,
    pub deployment_id: Uuid,
    pub credential_id: Uuid,
    pub endpoint: String,
    pub provider_id: String,
}

impl Gateway {
    pub(crate) async fn resource_usage(
        &self,
        kind: &str,
        key: Uuid,
        revision: i64,
        organization_keys: &[Uuid],
    ) -> Result<Usage, ControlError> {
        let Json(usage): Json<Value> = self
            .call(
                "resource_usage",
                sqlx::query_scalar("SELECT gateway_v1.resource_usage($1,$2,$3,$4)")
                    .bind(kind)
                    .bind(key)
                    .bind(revision)
                    .bind(organization_keys)
                    .fetch_one(&self.pool),
            )
            .await?;
        serde_json::from_value(usage).map_err(|_| ControlError::Invalid("resource usage"))
    }

    pub(crate) async fn claim_purges(
        &self,
        grace_seconds: i32,
        limit: i32,
    ) -> Result<Vec<Purge>, ControlError> {
        let Json(jobs): Json<Value> = self
            .call(
                "claim_purges",
                sqlx::query_scalar("SELECT gateway_v1.claim_purges($1,$2)")
                    .bind(grace_seconds)
                    .bind(limit)
                    .fetch_one(&self.pool),
            )
            .await?;
        serde_json::from_value(jobs).map_err(|_| ControlError::Invalid("purge claim"))
    }

    pub(crate) async fn finish_purge(
        &self,
        id: Uuid,
        error: Option<&str>,
    ) -> Result<(), ControlError> {
        self.call(
            "finish_purge",
            sqlx::query("SELECT gateway_v1.finish_purge($1,$2,$3)")
                .bind(id)
                .bind(if error.is_some() { "failed" } else { "done" })
                .bind(error)
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn record_orphan(
        &self,
        key: Uuid,
        revision: i64,
        kind: &str,
        deployment: Uuid,
        endpoint: &str,
        provider_id: &str,
    ) -> Result<(), ControlError> {
        self.call(
            "record_orphan",
            sqlx::query("SELECT gateway_v1.record_orphan($1,$2,$3,$4,$5,$6)")
                .bind(key)
                .bind(revision)
                .bind(kind)
                .bind(deployment)
                .bind(endpoint)
                .bind(provider_id)
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }
}
