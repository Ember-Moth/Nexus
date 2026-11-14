mod completion;

use crate::{
    control::{
        ControlError,
        database::Gateway,
        limits::{Admission, LimitError},
        snapshot::{Index, Rejection},
    },
    protocol::request::Generate,
    server::Node,
};
use completion::{Attempt, confirmed_tokens, finish_lease};
use http::StatusCode;
use std::{sync::Arc, time::Duration};
use tokio::{
    sync::oneshot,
    time::{Instant, sleep_until},
};
use uuid::Uuid;

pub struct Response {
    pub status: StatusCode,
    pub body: Vec<u8>,
    pub receipt: oneshot::Sender<()>,
}
#[derive(Clone, Copy)]
pub struct Failure(pub StatusCode, pub &'static str);
impl Failure {
    pub fn unavailable() -> Self {
        Self(StatusCode::SERVICE_UNAVAILABLE, "dependency_unavailable")
    }
    pub fn upstream() -> Self {
        Self(StatusCode::BAD_GATEWAY, "upstream_error")
    }
}
impl From<Rejection> for Failure {
    fn from(r: Rejection) -> Self {
        match r {
            Rejection::Key => Self(StatusCode::UNAUTHORIZED, "invalid_api_key"),
            Rejection::Forbidden => Self(StatusCode::FORBIDDEN, "model_forbidden"),
            Rejection::Unsupported => Self(StatusCode::BAD_REQUEST, "unsupported_request"),
            Rejection::Unavailable => Self(StatusCode::BAD_REQUEST, "unsupported_deployment"),
        }
    }
}
impl From<ControlError> for Failure {
    fn from(e: ControlError) -> Self {
        match e.database_message() {
            Some("key_inactive") => Self(StatusCode::UNAUTHORIZED, "invalid_api_key"),
            Some(
                "model_forbidden" | "snapshot_forbidden" | "key_outside_cluster" | "account_frozen",
            ) => Self(StatusCode::FORBIDDEN, "access_denied"),
            Some("insufficient_funds" | "budget_exceeded") => {
                Self(StatusCode::PAYMENT_REQUIRED, "insufficient_budget")
            }
            _ => Self::unavailable(),
        }
    }
}

type Channel = oneshot::Sender<Result<Response, Failure>>;

async fn cancel(db: &Gateway, rid: Uuid) -> bool {
    for _ in 0..3 {
        match db.cancel_unstarted(rid, "relay_cancelled").await {
            Ok(_) => return true,
            Err(e) if e.retryable() => tokio::time::sleep(Duration::from_millis(100)).await,
            Err(_) => return false,
        }
    }
    false
}

pub async fn run(
    node: Arc<Node>,
    index: Arc<Index>,
    secret: String,
    request: Generate,
    mut sender: Channel,
) {
    let selected = match index.select(&secret, &request) {
        Ok(s) => s,
        Err(e) => {
            let _ = sender.send(Err(e.into()));
            return;
        }
    };
    let endpoint = request.endpoint;
    let rid = Uuid::new_v4();
    let bounds = selected.bounds;
    let db = &node.db;
    let deployment = selected.deployment;
    let reference = &index.credentials[&deployment.credential_id];
    let credential = match db
        .credential(index.revision, reference)
        .await
        .and_then(|e| e.open(index.cluster, reference, &node.private))
    {
        Ok(s) => s,
        Err(e) => {
            let _ = sender.send(Err(e.into()));
            return;
        }
    };
    if sender.is_closed() {
        return;
    }
    let mut lease = match node
        .limits
        .acquire(Admission {
            key: selected.key,
            request: rid,
            node: index.node,
            model: selected.model,
            revision: index.revision,
            bounds,
            quotas: &index.quotas,
            ttl_ms: node.lease_ms,
        })
        .await
    {
        Ok(l) => l,
        Err(e) => {
            let _ = sender.send(Err(match e {
                LimitError::Limited => Failure(StatusCode::TOO_MANY_REQUESTS, "rate_limited"),
                _ => Failure::unavailable(),
            }));
            return;
        }
    };

    let reserve = || db.reserve(rid, selected.key.id, selected.model, index.revision, bounds);
    let first = reserve().await;
    let retried = first.as_ref().is_err_and(|e| e.retryable());
    let reserved = if retried { reserve().await } else { first };
    if let Err(e) = reserved {
        let _ = sender.send(Err(e.into()));
        // No upstream side effect was possible. Reconcile any committed reservation.
        if retried && !cancel(db, rid).await {
            eprintln!("could not confirm reservation cleanup for request {rid}");
        }
        finish_lease(&node.limits, &lease, Some(0), rid).await;
        return;
    }
    if sender.is_closed() || node.limits.renew(&mut lease).await.is_err() {
        let _ = sender.send(Err(Failure::unavailable()));
        if !cancel(db, rid).await {
            eprintln!("could not confirm reservation cleanup for request {rid}");
        }
        finish_lease(&node.limits, &lease, Some(0), rid).await;
        return;
    }

    let a = Attempt {
        request: rid,
        attempt: Uuid::new_v4(),
        revision: index.revision,
        endpoint,
        started: Instant::now(),
    };
    let start = || db.start_attempt(rid, a.attempt, 1, deployment.id);
    let first = start().await;
    let retried = first.as_ref().is_err_and(|e| e.retryable());
    let started = if retried { start().await } else { first };
    if let Err(e) = started {
        let _ = sender.send(Err(e.into()));
        // Cancellation locks the request. If a commit won the race, look up its actual attempt.
        if !cancel(db, rid).await
            && retried
            && let Ok(Some(status)) = db.status(rid).await
            && status["attempts"]
                .as_array()
                .is_some_and(|v| v.iter().any(|r| r["id"] == a.attempt.to_string()))
        {
            a.unknown(db).await;
        }
        finish_lease(&node.limits, &lease, Some(0), rid).await;
        return;
    }

    // A confirmed attempt is owned by this task even if the handler or socket disappears.
    let result = {
        let upstream =
            node.upstream
                .generate(&deployment.base_url, endpoint, &credential, &selected.body);
        tokio::pin!(upstream);
        loop {
            if Instant::now() >= lease.safe_until || sender.is_closed() {
                break None;
            }
            tokio::select! {
                biased;
                _=sender.closed()=>break None,
                _=sleep_until(lease.renewal_at())=>{if node.limits.renew(&mut lease).await.is_err(){break None;}},
                result=&mut upstream=>break result.ok(),
            }
        }
    };
    let Some(reply) = result else {
        let _ = sender.send(Err(Failure::upstream()));
        finish_lease(&node.limits, &lease, None, rid).await;
        a.unknown(db).await;
        return;
    };
    let actual = confirmed_tokens(endpoint, &reply.usage);
    // The upstream operation is finished. Remaining work owns no concurrency slot.
    let (receipt, received) = oneshot::channel();
    let sent = sender
        .send(Ok(Response {
            status: reply.status,
            body: reply.body,
            receipt,
        }))
        .is_ok();
    finish_lease(&node.limits, &lease, actual, rid).await;
    let delivered = sent && received.await.is_ok();
    let delivery = match (reply.outcome, delivered) {
        ("succeeded", true) => "succeeded",
        ("failed", _) => "upstream_failed",
        _ => "unknown",
    };
    a.complete(
        db,
        reply.outcome,
        delivery,
        reply.response_id.as_deref(),
        reply.usage,
    )
    .await;
}
