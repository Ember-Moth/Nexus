use super::{
    ControlError,
    database::{Gateway, StoredResponse},
};
use crate::protocol::Endpoint;
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

impl Gateway {
    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn execution_container(
        &self,
        kid: Uuid,
        rev: i64,
        target: (Uuid, Uuid),
        ep: Endpoint,
        pid: &str,
        meta: Option<&Value>,
        creating: bool,
    ) -> Result<Uuid, ControlError> {
        self.call(
            "container_bind",
            sqlx::query_scalar("SELECT gateway_v1.container_bind($1,$2,$3,$4,$5,$6,$7,$8)")
                .bind(kid)
                .bind(rev)
                .bind(target.0)
                .bind(target.1)
                .bind(ep.as_str())
                .bind(pid)
                .bind(meta.map(Json))
                .bind(creating)
                .fetch_one(&self.pool),
        )
        .await
    }
    pub(crate) async fn container(
        &self,
        id: Uuid,
        kid: Uuid,
        rev: i64,
        ep: Endpoint,
    ) -> Result<StoredResponse, ControlError> {
        let Json(row) = self
            .call(
                "container_resolve",
                sqlx::query_scalar("SELECT gateway_v1.container_resolve($1,$2,$3,$4)")
                    .bind(id)
                    .bind(kid)
                    .bind(rev)
                    .bind(ep.as_str())
                    .fetch_one(&self.pool),
            )
            .await?;
        Ok(row)
    }
    pub(crate) async fn execution_output(
        &self,
        origin: Uuid,
        kid: Uuid,
        dep: Uuid,
        pid: &str,
        file: bool,
    ) -> Result<Uuid, ControlError> {
        self.call(
            "execution_output",
            sqlx::query_scalar("SELECT gateway_v1.execution_output($1,$2,$3,$4,$5)")
                .bind(origin)
                .bind(kid)
                .bind(dep)
                .bind(pid)
                .bind(file)
                .fetch_one(&self.pool),
        )
        .await
    }
    pub(crate) async fn execution_file(
        &self,
        kid: Uuid,
        rev: i64,
        model: Uuid,
        dep: Uuid,
        pid: &str,
        creating: bool,
    ) -> Result<Uuid, ControlError> {
        self.call(
            "execution_file",
            sqlx::query_scalar("SELECT gateway_v1.execution_file($1,$2,$3,$4,$5,$6)")
                .bind(kid)
                .bind(rev)
                .bind(model)
                .bind(dep)
                .bind(pid)
                .bind(creating)
                .fetch_one(&self.pool),
        )
        .await
    }
    pub(crate) async fn refresh_execution_file(
        &self,
        id: Uuid,
        kid: Uuid,
        rev: i64,
        v: &Value,
    ) -> Result<(), ControlError> {
        self.call(
            "execution_file_refresh",
            sqlx::query("SELECT gateway_v1.execution_file_refresh($1,$2,$3,$4)")
                .bind(id)
                .bind(kid)
                .bind(rev)
                .bind(Json(v))
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }
    pub(crate) async fn batch_execution(
        &self,
        b: Uuid,
        custom: &str,
        kid: Uuid,
        rev: i64,
    ) -> Result<Value, ControlError> {
        let Json(item) = self
            .call(
                "batch_execution",
                sqlx::query_scalar("SELECT gateway_v1.batch_execution($1,$2,$3,$4)")
                    .bind(b)
                    .bind(custom)
                    .bind(kid)
                    .bind(rev)
                    .fetch_one(&self.pool),
            )
            .await?;
        Ok(item)
    }
}

impl Gateway {
    /// Reserve `selection`; with `first` its first attempt is registered on the selected
    /// deployment in the same transaction.
    pub(crate) async fn reserve_generation(
        &self,
        id: Uuid,
        rev: i64,
        selection: &super::snapshot::Selection<'_>,
        first: Option<Uuid>,
    ) -> Result<Value, ControlError> {
        let (kid, model, dep, b) = (
            selection.key.id,
            selection.model,
            selection.deployment.id,
            selection.bounds,
        );
        if b.guardrail_units > 0 {
            let Json(reply) = self
                .call(
                    "reserve_guardrail_request",
                    sqlx::query_scalar(
                        "SELECT gateway_v1.reserve_guardrail_request($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                    )
                    .bind(id)
                    .bind(kid)
                    .bind(model)
                    .bind(rev)
                    .bind(i64::from(b.inputs))
                    .bind(i64::from(b.outputs))
                    .bind(dep)
                    .bind(b.guardrail_units as i32)
                    .bind(first)
                    .fetch_one(&self.pool),
                )
                .await?;
            return Ok(reply);
        }
        let Some(t) = b.interpreter else {
            return self
                .reserve_with_source(
                    id,
                    kid,
                    model,
                    rev,
                    b,
                    first.map(|aid| (aid, dep)),
                    selection.private_history.then_some(dep),
                )
                .await;
        };
        let Json(reply) = self
            .call(
                "reserve_interpreter",
                sqlx::query_scalar(
                    "SELECT gateway_v1.reserve_interpreter($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
                )
                .bind(id)
                .bind(kid)
                .bind(model)
                .bind(rev)
                .bind(i64::from(b.inputs))
                .bind(i64::from(b.outputs))
                .bind(b.searches as i32)
                .bind(b.file_searches as i32)
                .bind(dep)
                .bind(t.existing)
                .bind(t.tier.name())
                .bind(t.preloaded)
                .bind(first)
                .fetch_one(&self.pool),
            )
            .await?;
        Ok(reply)
    }
    pub(crate) async fn admit_container(
        &self,
        origin: Uuid,
        kid: Uuid,
        rev: i64,
        target: (Uuid, Uuid),
        t: crate::protocol::execution::ContainerTarget,
        fresh: bool,
    ) -> Result<(), ControlError> {
        let insert = || {
            self.call(
                "container_admit",
                sqlx::query("SELECT gateway_v1.container_admit($1,$2,$3,$4,$5,$6,$7,$8,$9)")
                    .bind(origin)
                    .bind(kid)
                    .bind(rev)
                    .bind(target.0)
                    .bind(target.1)
                    .bind(t.existing)
                    .bind(t.tier.name())
                    .bind(fresh)
                    .bind(t.preloaded)
                    .execute(&self.pool),
            )
        };
        if let Err(e) = insert().await {
            if !e.retryable() {
                return Err(e);
            }
            insert().await?;
        }
        Ok(())
    }
    pub(crate) async fn container_receipt(
        &self,
        origin: Uuid,
        kid: Uuid,
        pid: &str,
        meta: Option<&Value>,
        used: bool,
    ) -> Result<Uuid, ControlError> {
        let save = || {
            self.call(
                "container_receipt",
                sqlx::query_scalar("SELECT gateway_v1.container_receipt($1,$2,$3,$4,$5)")
                    .bind(origin)
                    .bind(kid)
                    .bind(pid)
                    .bind(meta.map(Json))
                    .bind(used)
                    .fetch_one(&self.pool),
            )
        };
        match save().await {
            Err(e) if e.retryable() => save().await,
            result => result,
        }
    }
    pub(crate) async fn unused_container(
        &self,
        origin: Uuid,
        kid: Uuid,
        definite: bool,
    ) -> Result<(), ControlError> {
        self.call(
            "container_no_use",
            sqlx::query("SELECT gateway_v1.container_no_use($1,$2,$3)")
                .bind(origin)
                .bind(kid)
                .bind(definite)
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }
    pub(crate) async fn container_page(
        &self,
        kid: Uuid,
        rev: i64,
        cursor: Option<Uuid>,
        asc: bool,
        n: i32,
    ) -> Result<Vec<Value>, ControlError> {
        let rows: Vec<Json<Value>> = self
            .call(
                "containers",
                sqlx::query_scalar("SELECT * FROM gateway_v1.containers($1,$2,$3,$4,$5)")
                    .bind(kid)
                    .bind(rev)
                    .bind(cursor)
                    .bind(asc)
                    .bind(n)
                    .fetch_all(&self.pool),
            )
            .await?;
        Ok(rows.into_iter().map(|j| j.0).collect())
    }
    pub(crate) async fn container_file_id(
        &self,
        c: Uuid,
        kid: Uuid,
        rev: i64,
        pid: &str,
        create: bool,
    ) -> Result<Uuid, ControlError> {
        self.call(
            "container_file_bind",
            sqlx::query_scalar("SELECT gateway_v1.container_file_bind($1,$2,$3,$4,$5)")
                .bind(c)
                .bind(kid)
                .bind(rev)
                .bind(pid)
                .bind(create)
                .fetch_one(&self.pool),
        )
        .await
    }
    pub(crate) async fn container_file(
        &self,
        c: Uuid,
        kid: Uuid,
        rev: i64,
        id: Uuid,
        remove: bool,
    ) -> Result<String, ControlError> {
        self.call(
            "container_file",
            sqlx::query_scalar("SELECT gateway_v1.container_file($1,$2,$3,$4,$5)")
                .bind(c)
                .bind(kid)
                .bind(rev)
                .bind(id)
                .bind(remove)
                .fetch_one(&self.pool),
        )
        .await
    }
    pub(crate) async fn cleanup_container(
        &self,
        origin: Uuid,
        c: Uuid,
        kid: Uuid,
        rev: i64,
    ) -> Result<StoredResponse, ControlError> {
        let Json(row) = self
            .call(
                "container_cleanup",
                sqlx::query_scalar("SELECT gateway_v1.container_cleanup($1,$2,$3,$4)")
                    .bind(origin)
                    .bind(c)
                    .bind(kid)
                    .bind(rev)
                    .fetch_one(&self.pool),
            )
            .await?;
        Ok(row)
    }
    pub(crate) async fn retired_container(
        &self,
        origin: Uuid,
        kid: Uuid,
        pid: &str,
        confirmed: bool,
    ) -> Result<(), ControlError> {
        self.call(
            "container_retired",
            sqlx::query("SELECT gateway_v1.container_retired($1,$2,$3,$4)")
                .bind(origin)
                .bind(kid)
                .bind(pid)
                .bind(confirmed)
                .execute(&self.pool),
        )
        .await?;
        Ok(())
    }
    pub(crate) async fn stored_interpreter(
        &self,
        handle: Uuid,
        kid: Uuid,
        rev: i64,
    ) -> Result<Option<Uuid>, ControlError> {
        self.call(
            "stored_interpreter",
            sqlx::query_scalar("SELECT gateway_v1.stored_interpreter($1,$2,$3)")
                .bind(handle)
                .bind(kid)
                .bind(rev)
                .fetch_one(&self.pool),
        )
        .await
    }
}
