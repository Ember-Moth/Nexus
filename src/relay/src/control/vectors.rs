//! Key-scoped vector store bindings and independently recorded storage observations.
use super::{ControlError, database::Gateway};
use chrono::{DateTime, Utc};
use serde::Deserialize;
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

#[derive(Deserialize)]
pub(crate) struct Store {
    pub id: Uuid,
    pub model_id: Uuid,
    pub deployment_id: Uuid,
    pub credential_id: Uuid,
    pub credential_version: i32,
    pub upstream_version: i32,
    pub state: String,
    pub provider_id: Option<String>,
    pub stamp: DateTime<Utc>,
    /// Delete the supplier store: its prepaid storage lapsed.
    #[serde(default)]
    pub retire: bool,
}
pub(crate) struct Observation<'a> {
    pub handle: Uuid,
    pub key: Option<Uuid>,
    pub revision: i64,
    pub provider: Option<&'a str>,
    pub stamp: DateTime<Utc>,
    pub metadata: Option<&'a Value>,
    pub state: &'static str,
    pub uncertain: bool,
}
impl Gateway {
    pub(crate) async fn prepare_vector(
        &self,
        id: Uuid,
        key: Uuid,
        rev: i64,
        model: Uuid,
        dep: Uuid,
    ) -> Result<DateTime<Utc>, ControlError> {
        let prepare = || {
            self.call(
                "vector_prepare",
                sqlx::query_scalar("SELECT gateway_v1.vector_prepare($1,$2,$3,$4,$5)")
                    .bind(id)
                    .bind(key)
                    .bind(rev)
                    .bind(model)
                    .bind(dep)
                    .fetch_one(&self.pool),
            )
        };
        let Json(stamp) = match prepare().await {
            Err(e) if e.retryable() => prepare().await?,
            res => res?,
        };
        Ok(stamp)
    }
    pub(crate) async fn vector(
        &self,
        id: Uuid,
        key: Uuid,
        rev: i64,
    ) -> Result<Store, ControlError> {
        let Json(v) = self
            .call(
                "vector_resolve",
                sqlx::query_scalar("SELECT gateway_v1.vector_resolve($1,$2,$3)")
                    .bind(id)
                    .bind(key)
                    .bind(rev)
                    .fetch_one(&self.pool),
            )
            .await?;
        Ok(v)
    }
    pub(crate) async fn cleanup_vector(
        &self,
        id: Uuid,
        key: Uuid,
        rev: i64,
    ) -> Result<Store, ControlError> {
        let Json(v) = self
            .call(
                "vector_cleanup",
                sqlx::query_scalar("SELECT gateway_v1.vector_cleanup($1,$2,$3)")
                    .bind(id)
                    .bind(key)
                    .bind(rev)
                    .fetch_one(&self.pool),
            )
            .await?;
        Ok(v)
    }
    pub(crate) async fn vector_page(
        &self,
        key: Uuid,
        rev: i64,
        cursor: Option<Uuid>,
        asc: bool,
        n: i32,
    ) -> Result<Vec<Value>, ControlError> {
        let rows: Vec<Json<Value>> = self
            .call(
                "vector_page",
                sqlx::query_scalar("SELECT * FROM gateway_v1.vector_page($1,$2,$3,$4,$5)")
                    .bind(key)
                    .bind(rev)
                    .bind(cursor)
                    .bind(asc)
                    .bind(n)
                    .fetch_all(&self.pool),
            )
            .await?;
        Ok(rows.into_iter().map(|v| v.0).collect())
    }
    pub(crate) async fn observe_vector(&self, s: Observation<'_>) -> Result<(), ControlError> {
        let record = || {
            self.call(
                "vector_observe",
                sqlx::query(
                    "SELECT gateway_v1.vector_observe($1,$2,$3,$4,$5::text::timestamptz,$6,$7,$8)",
                )
                .bind(s.handle)
                .bind(s.key)
                .bind(s.revision)
                .bind(s.provider)
                .bind(s.stamp.to_rfc3339())
                .bind(s.metadata.map(Json))
                .bind(s.state)
                .bind(s.uncertain)
                .execute(&self.pool),
            )
        };
        if let Err(e) = record().await {
            if !e.retryable() {
                return Err(e);
            }
            record().await?;
        }
        Ok(())
    }
    pub(crate) async fn vector_due(
        &self,
        id: Uuid,
        kid: Uuid,
        rev: i64,
    ) -> Result<(), ControlError> {
        self.call(
            "vector_due",
            sqlx::query("SELECT gateway_v1.vector_due($1,$2,$3)")
                .bind(id)
                .bind(kid)
                .bind(rev)
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }
    pub(crate) async fn vector_locator(
        &self,
        id: Uuid,
        kid: Uuid,
        pid: &str,
    ) -> Result<(), ControlError> {
        self.call(
            "vector_locator",
            sqlx::query("SELECT gateway_v1.vector_locator($1,$2,$3)")
                .bind(id)
                .bind(kid)
                .bind(pid)
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }
    pub(crate) async fn vector_writable(
        &self,
        id: Uuid,
        kid: Uuid,
        rev: i64,
    ) -> Result<(), ControlError> {
        self.call(
            "vector_writable",
            sqlx::query("SELECT gateway_v1.vector_writable($1,$2,$3)")
                .bind(id)
                .bind(kid)
                .bind(rev)
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }
    pub(crate) async fn vector_missed(
        &self,
        id: Uuid,
        stamp: DateTime<Utc>,
    ) -> Result<(), ControlError> {
        self.call(
            "vector_missed",
            sqlx::query("SELECT gateway_v1.vector_missed($1,$2::text::timestamptz)")
                .bind(id)
                .bind(stamp.to_rfc3339())
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }
    pub(crate) async fn claim_vector(&self) -> Result<Option<Store>, ControlError> {
        let job: Option<Json<Store>> = self
            .call(
                "vector_claim",
                sqlx::query_scalar("SELECT gateway_v1.vector_claim()").fetch_one(&self.pool),
            )
            .await?;
        Ok(job.map(|v| v.0))
    }
    pub(crate) async fn vector_batch(
        &self,
        store: Uuid,
        kid: Uuid,
        rev: i64,
        id: Uuid,
        pid: Option<&str>,
    ) -> Result<String, ControlError> {
        self.call(
            "vector_batch",
            sqlx::query_scalar("SELECT gateway_v1.vector_batch($1,$2,$3,$4,$5,$6)")
                .bind(store)
                .bind(kid)
                .bind(rev)
                .bind(id)
                .bind(pid)
                .bind(pid.is_some())
                .fetch_one(&self.pool),
        )
        .await
    }
    pub(crate) async fn vector_id(
        &self,
        kid: Uuid,
        rev: i64,
        model: Uuid,
        dep: Uuid,
        pid: &str,
    ) -> Result<Option<Uuid>, ControlError> {
        self.call(
            "vector_id",
            sqlx::query_scalar("SELECT gateway_v1.vector_id($1,$2,$3,$4,$5)")
                .bind(kid)
                .bind(rev)
                .bind(model)
                .bind(dep)
                .bind(pid)
                .fetch_one(&self.pool),
        )
        .await
    }
}
