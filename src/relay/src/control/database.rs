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
    pub(super) pool: PgPool,
}

#[derive(Clone)]
pub(crate) struct FilePage {
    pub cursor: Option<Uuid>,
    pub newer: bool,
    pub limit: i32,
    pub ascending: bool,
    pub purpose: Option<String>,
}

#[derive(Deserialize)]
pub struct StoredResponse {
    #[serde(default)]
    pub metadata: Value,
    pub model: Uuid,
    pub deployment: Uuid,
    pub provider_id: String,
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
    pub(super) async fn call<T>(
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

    pub async fn register_background(
        &self,
        id: Uuid,
        rid: Uuid,
        aid: Uuid,
        pid: &str,
    ) -> Result<(), ControlError> {
        let attempt_event = Uuid::new_v4();
        let close_event = Uuid::new_v4();
        let register = || {
            self.call(
                "background_register",
                sqlx::query("SELECT gateway_v1.background_register($1,$2,$3,$4,$5,$6)")
                    .bind(id)
                    .bind(rid)
                    .bind(aid)
                    .bind(pid)
                    .bind(attempt_event)
                    .bind(close_event)
                    .execute(&self.pool),
            )
        };
        if let Err(error) = register().await {
            if !error.retryable() {
                return Err(error);
            }
            if register().await.is_err() {
                return Err(error);
            }
        }
        Ok(())
    }

    pub(crate) async fn background_claim(
        &self,
    ) -> Result<Option<crate::server::background::Job>, ControlError> {
        let row: Option<Json<crate::server::background::Job>> = self
            .call(
                "background_claim",
                sqlx::query_scalar("SELECT gateway_v1.background_claim()").fetch_one(&self.pool),
            )
            .await?;
        Ok(row.map(|v| v.0))
    }

    pub async fn background_finish(
        &self,
        id: Uuid,
        outcome: &str,
        delivery: &str,
        usage: Value,
    ) -> Result<(), ControlError> {
        self.call(
            "background_finish",
            sqlx::query("SELECT gateway_v1.background_finish($1,$2,$3,$4)")
                .bind(id)
                .bind(outcome)
                .bind(delivery)
                .bind(Json(usage))
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }

    pub async fn can_delete(&self, id: Uuid, kid: Uuid, rev: i64) -> Result<(), ControlError> {
        self.call(
            "response_can_delete",
            sqlx::query("SELECT gateway_v1.response_can_delete($1,$2,$3)")
                .bind(id)
                .bind(kid)
                .bind(rev)
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }

    pub async fn files(
        &self,
        kid: Uuid,
        rev: i64,
        cursor: Option<Uuid>,
        newer: bool,
        limit: i32,
    ) -> Result<Vec<Value>, ControlError> {
        self.native_files(
            kid,
            rev,
            crate::protocol::Endpoint::AnthropicMessages,
            FilePage {
                cursor,
                newer,
                limit,
                ascending: false,
                purpose: None,
            },
        )
        .await
    }
    pub(crate) async fn native_files(
        &self,
        kid: Uuid,
        rev: i64,
        ep: crate::protocol::Endpoint,
        page: FilePage,
    ) -> Result<Vec<Value>, ControlError> {
        let rows: Vec<Json<Value>> = self
            .call(
                "files",
                sqlx::query_scalar(
                    "SELECT * FROM gateway_v1.native_files($1,$2,$3,$4,$5,$6,$7,$8)",
                )
                .bind(kid)
                .bind(rev)
                .bind(page.cursor)
                .bind(page.newer)
                .bind(page.limit)
                .bind(ep.as_str())
                .bind(page.ascending)
                .bind(page.purpose)
                .fetch_all(&self.pool),
            )
            .await?;
        Ok(rows.into_iter().map(|j| j.0).collect())
    }
    pub async fn save_file(
        &self,
        id: Uuid,
        kid: Uuid,
        rev: i64,
        model: Uuid,
        dep: Uuid,
        meta: &Value,
    ) -> Result<(), ControlError> {
        self.save_native_file(
            id,
            kid,
            rev,
            (model, dep),
            meta,
            crate::protocol::Endpoint::AnthropicMessages,
        )
        .await
    }
    pub(crate) async fn save_native_file(
        &self,
        id: Uuid,
        kid: Uuid,
        rev: i64,
        target: (Uuid, Uuid),
        meta: &Value,
        ep: crate::protocol::Endpoint,
    ) -> Result<(), ControlError> {
        self.call(
            "file_save",
            sqlx::query("SELECT gateway_v1.native_file_save($1,$2,$3,$4,$5,$6,$7)")
                .bind(id)
                .bind(kid)
                .bind(rev)
                .bind(target.0)
                .bind(target.1)
                .bind(Json(meta))
                .bind(ep.as_str())
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }
    pub async fn file(
        &self,
        id: Uuid,
        kid: Uuid,
        rev: i64,
    ) -> Result<StoredResponse, ControlError> {
        self.native_file(id, kid, rev, crate::protocol::Endpoint::AnthropicMessages)
            .await
    }
    pub(crate) async fn native_file(
        &self,
        id: Uuid,
        kid: Uuid,
        rev: i64,
        ep: crate::protocol::Endpoint,
    ) -> Result<StoredResponse, ControlError> {
        let Json(value) = self
            .call(
                "file_resolve",
                sqlx::query_scalar("SELECT gateway_v1.native_file_resolve($1,$2,$3,$4)")
                    .bind(id)
                    .bind(kid)
                    .bind(rev)
                    .bind(ep.as_str())
                    .fetch_one(&self.pool),
            )
            .await?;
        Ok(value)
    }
    pub async fn remove_file(&self, id: Uuid, kid: Uuid, rev: i64) -> Result<(), ControlError> {
        self.remove_native_file(id, kid, rev, crate::protocol::Endpoint::AnthropicMessages)
            .await
    }
    pub(crate) async fn remove_native_file(
        &self,
        id: Uuid,
        kid: Uuid,
        rev: i64,
        ep: crate::protocol::Endpoint,
    ) -> Result<(), ControlError> {
        self.call(
            "file_remove",
            sqlx::query("SELECT gateway_v1.native_file_remove($1,$2,$3,$4)")
                .bind(id)
                .bind(kid)
                .bind(rev)
                .bind(ep.as_str())
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }

    pub(crate) async fn openai_file_id(
        &self,
        kid: Uuid,
        rev: i64,
        model: Uuid,
        dep: Uuid,
        pid: &str,
    ) -> Result<Option<Uuid>, ControlError> {
        self.call(
            "openai_file_id",
            sqlx::query_scalar("SELECT gateway_v1.openai_file_id($1,$2,$3,$4,$5)")
                .bind(kid)
                .bind(rev)
                .bind(model)
                .bind(dep)
                .bind(pid)
                .fetch_one(&self.pool),
        )
        .await
    }

    pub(crate) async fn conversation(
        &self,
        id: Uuid,
        kid: Uuid,
        rev: i64,
    ) -> Result<StoredResponse, ControlError> {
        let Json(value) = self
            .call(
                "conversation_resolve",
                sqlx::query_scalar("SELECT gateway_v1.conversation_resolve($1,$2,$3)")
                    .bind(id)
                    .bind(kid)
                    .bind(rev)
                    .fetch_one(&self.pool),
            )
            .await?;
        Ok(value)
    }
    pub(crate) async fn save_conversation(
        &self,
        id: Uuid,
        kid: Uuid,
        rev: i64,
        model: Uuid,
        dep: Uuid,
        pid: &str,
    ) -> Result<(), ControlError> {
        self.call(
            "conversation_save",
            sqlx::query("SELECT gateway_v1.conversation_save($1,$2,$3,$4,$5,$6)")
                .bind(id)
                .bind(kid)
                .bind(rev)
                .bind(model)
                .bind(dep)
                .bind(pid)
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }
    pub(crate) async fn conversation_id(
        &self,
        kid: Uuid,
        rev: i64,
        model: Uuid,
        dep: Uuid,
        pid: &str,
    ) -> Result<Option<Uuid>, ControlError> {
        self.call(
            "conversation_id",
            sqlx::query_scalar("SELECT gateway_v1.conversation_id($1,$2,$3,$4,$5)")
                .bind(kid)
                .bind(rev)
                .bind(model)
                .bind(dep)
                .bind(pid)
                .fetch_one(&self.pool),
        )
        .await
    }
    pub(crate) async fn remove_conversation(
        &self,
        id: Uuid,
        kid: Uuid,
        rev: i64,
    ) -> Result<(), ControlError> {
        self.call(
            "conversation_remove",
            sqlx::query("SELECT gateway_v1.conversation_remove($1,$2,$3)")
                .bind(id)
                .bind(kid)
                .bind(rev)
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }

    pub async fn save_response(
        &self,
        id: Uuid,
        rid: Uuid,
        aid: Uuid,
        provider: &str,
    ) -> Result<(), ControlError> {
        self.call(
            "save_response",
            sqlx::query("SELECT gateway_v1.save_response($1,$2,$3,$4)")
                .bind(id)
                .bind(rid)
                .bind(aid)
                .bind(provider)
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }
    pub async fn resolve_response(
        &self,
        id: Uuid,
        kid: Uuid,
        rev: i64,
    ) -> Result<StoredResponse, ControlError> {
        let Json(data) = self
            .call(
                "resolve_response",
                sqlx::query_scalar("SELECT gateway_v1.resolve_response($1,$2,$3)")
                    .bind(id)
                    .bind(kid)
                    .bind(rev)
                    .fetch_one(&self.pool),
            )
            .await?;
        Ok(data)
    }
    pub async fn forget_response(&self, id: Uuid, kid: Uuid, rev: i64) -> Result<(), ControlError> {
        self.call(
            "forget_response",
            sqlx::query("SELECT gateway_v1.forget_response($1,$2,$3)")
                .bind(id)
                .bind(kid)
                .bind(rev)
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }

    pub async fn preflight(
        &self,
        key: Uuid,
        revision: i64,
        model: Option<Uuid>,
        dep: Option<Uuid>,
    ) -> Result<Value, ControlError> {
        let Json(result) = self
            .call(
                "preflight",
                sqlx::query_scalar("SELECT gateway_v1.preflight($1,$2,$3,$4)")
                    .bind(key)
                    .bind(revision)
                    .bind(model)
                    .bind(dep)
                    .fetch_one(&self.pool),
            )
            .await?;
        Ok(result)
    }

    pub async fn reserve(
        &self,
        request: Uuid,
        key: Uuid,
        model: Uuid,
        revision: i64,
        bounds: ReservationBounds,
    ) -> Result<Value, ControlError> {
        self.reserve_with_source(request, key, model, revision, bounds, None)
            .await
    }
    pub(super) async fn reserve_with_source(
        &self,
        request: Uuid,
        key: Uuid,
        model: Uuid,
        revision: i64,
        bounds: ReservationBounds,
        source: Option<Uuid>,
    ) -> Result<Value, ControlError> {
        let execution = bounds.execution_mode != crate::protocol::execution::ExecutionMode::None;
        let (sql, next_parameter) = if execution {
            (
                "SELECT gateway_v1.reserve_execution_request($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                10,
            )
        } else if bounds.file_searches > 0 {
            (
                "SELECT gateway_v1.reserve_file_request($1,$2,$3,$4,$5,$6,$7,$8)",
                9,
            )
        } else if bounds.searches > 0 {
            (
                "SELECT gateway_v1.reserve_search_request($1,$2,$3,$4,$5,$6,$7)",
                8,
            )
        } else {
            ("SELECT gateway_v1.reserve_request($1,$2,$3,$4,$5,$6)", 7)
        };
        // Both security-definer calls share one transaction. An eligible
        // alternate route cannot authorize a hold for stale private history.
        let pinned;
        let sql = if source.is_some() {
            pinned = format!("{sql} FROM gateway_v1.preflight($2,$4,$3,${next_parameter})");
            &pinned
        } else {
            sql
        };
        let mut query = sqlx::query_scalar(sql)
            .bind(request)
            .bind(key)
            .bind(model)
            .bind(revision)
            .bind(i64::from(bounds.inputs))
            .bind(i64::from(bounds.outputs));
        if execution || bounds.searches > 0 || bounds.file_searches > 0 {
            query = query.bind(bounds.searches as i32);
        }
        if execution {
            query = query
                .bind(bounds.execution_mode.as_str())
                .bind(bounds.execution_seconds as i32);
        } else if bounds.file_searches > 0 {
            query = query.bind(bounds.file_searches as i32);
        }
        if let Some(dep) = source {
            query = query.bind(dep);
        }
        let Json(reply) = self
            .call("reserve_request", query.fetch_one(&self.pool))
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
