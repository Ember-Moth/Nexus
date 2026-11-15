//! Gateway contracts for asynchronous native Message Batches.
use super::{ControlError, database::Gateway};
use crate::protocol::tokens::ReservationBounds;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

#[derive(Serialize)]
pub(crate) struct Item {
    pub custom_id: String,
    pub model: Uuid,
    pub deployment: Uuid,
    pub rid: Uuid,
    pub aid: Uuid,
    #[serde(flatten)]
    pub bounds: ReservationBounds,
}

#[derive(Deserialize)]
pub(crate) struct Batch {
    pub id: Uuid,
    pub api_key_id: Uuid,
    pub deployment_id: Uuid,
    pub provider_id: Option<String>,
    pub credential_id: Uuid,
    pub credential_version: i32,
    pub upstream_version: i32,
    pub headers: std::collections::BTreeMap<String, String>,
    pub done: bool,
    pub created_at: chrono::DateTime<chrono::Utc>,
}

impl Gateway {
    pub(crate) async fn batch_prepare(
        &self,
        id: Uuid,
        key: Uuid,
        rev: i64,
        dep: Uuid,
        items: &[Item],
        headers: Value,
    ) -> Result<(), ControlError> {
        self.call(
            "batch_prepare",
            sqlx::query("SELECT gateway_v1.batch_prepare($1,$2,$3,$4,$5,$6)")
                .bind(id)
                .bind(key)
                .bind(rev)
                .bind(dep)
                .bind(Json(items))
                .bind(Json(headers))
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }
    pub(crate) async fn batch_resolve(
        &self,
        id: Uuid,
        key: Uuid,
        rev: i64,
    ) -> Result<Batch, ControlError> {
        let Json(batch) = self
            .call(
                "batch_resolve",
                sqlx::query_scalar("SELECT gateway_v1.batch_resolve($1,$2,$3)")
                    .bind(id)
                    .bind(key)
                    .bind(rev)
                    .fetch_one(&self.pool),
            )
            .await?;
        Ok(batch)
    }
    pub(crate) async fn batch_page(
        &self,
        key: Uuid,
        rev: i64,
        cursor: Option<Uuid>,
        newer: bool,
        n: i32,
    ) -> Result<Vec<Value>, ControlError> {
        let rows: Vec<Json<Value>> = self
            .call(
                "batch_page",
                sqlx::query_scalar("SELECT * FROM gateway_v1.batch_page($1,$2,$3,$4,$5)")
                    .bind(key)
                    .bind(rev)
                    .bind(cursor)
                    .bind(newer)
                    .bind(n)
                    .fetch_all(&self.pool),
            )
            .await?;
        Ok(rows.into_iter().map(|j| j.0).collect())
    }
    pub(crate) async fn batch_record(
        &self,
        id: Uuid,
        key: Uuid,
        rev: i64,
        pid: &str,
        meta: &Value,
        remove: bool,
    ) -> Result<(), ControlError> {
        self.call(
            "batch_record",
            sqlx::query("SELECT gateway_v1.batch_record($1,$2,$3,$4,$5,$6)")
                .bind(id)
                .bind(key)
                .bind(rev)
                .bind(pid)
                .bind(Json(meta))
                .bind(remove)
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }
    pub(crate) async fn batch_claim(&self) -> Result<Option<Batch>, ControlError> {
        let row: Option<Json<Batch>> = self
            .call(
                "batch_claim",
                sqlx::query_scalar("SELECT gateway_v1.batch_claim()").fetch_one(&self.pool),
            )
            .await?;
        Ok(row.map(|j| j.0))
    }
    pub(crate) async fn batch_finish(
        &self,
        id: Uuid,
        pid: Option<&str>,
        meta: Option<&Value>,
        results: Value,
        fallback: &str,
    ) -> Result<(), ControlError> {
        self.call(
            "batch_finish",
            sqlx::query("SELECT gateway_v1.batch_finish($1,$2,$3,$4,$5)")
                .bind(id)
                .bind(pid)
                .bind(meta.map(Json))
                .bind(Json(results))
                .bind(fallback)
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }
}
