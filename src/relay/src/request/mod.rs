mod completion;
pub(crate) mod lifecycle;
mod sse;

use crate::{
    control::{
        ControlError,
        database::Gateway,
        snapshot::{Index, Rejection},
    },
    protocol::{
        convert::{self, Exchange},
        request::Generate,
    },
    server::Node,
};
use http::StatusCode;
use std::{sync::Arc, time::Duration};
use tokio::sync::{mpsc, oneshot};
use uuid::Uuid;

pub enum Response {
    Buffered {
        status: StatusCode,
        body: Vec<u8>,
        receipt: oneshot::Sender<()>,
    },
    Stream {
        body: mpsc::Receiver<Result<axum::body::Bytes, std::io::Error>>,
        receipt: oneshot::Sender<()>,
    },
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
    let endpoint = selected.upstream.representation();
    let exchange = Exchange {
        client: request.endpoint,
        upstream: endpoint,
        public_model: &request.model,
    };
    let (reservation, credential) =
        match lifecycle::Reservation::reserve(node.clone(), &index, &selected, endpoint).await {
            Ok(v) => v,
            Err(e) => {
                let _ = sender.send(Err(e));
                return;
            }
        };
    if sender.is_closed() {
        reservation.abort().await;
        return;
    }
    let mut run = match reservation.start().await {
        Ok(v) => v,
        Err(e) => {
            let _ = sender.send(Err(e));
            return;
        }
    };
    let dep = selected.deployment;
    let bedrock = dep.is_bedrock();
    if request.stream {
        sse::forward(
            node,
            run,
            dep,
            exchange,
            &credential,
            &selected.body,
            sender,
        )
        .await;
        return;
    }
    let rid = run.reservation.id.to_string();
    let generation = async {
        if bedrock {
            let res = crate::bedrock::transport::open(
                &node.upstream.client,
                dep,
                &credential,
                &selected.body,
                false,
            )
            .await?;
            crate::bedrock::transport::buffered(
                res,
                &request.model,
                &rid,
                node.upstream.response_limit(),
            )
            .await
        } else {
            node.upstream
                .generate(&dep.base_url, endpoint, &credential, &selected.body)
                .await
        }
    };
    let result = run
        .reservation
        .guarded(async {
            tokio::select! {_=sender.closed()=>None,r=generation=>r.ok()}
        })
        .await;
    if let Ok(Some(reply)) = result {
        deliver(run, sender, reply, exchange, node.upstream.response_limit()).await;
    } else {
        let _ = sender.send(Err(Failure::upstream()));
        run.ended(None, false).await;
    }
}

async fn deliver(
    run: lifecycle::Run,
    sender: Channel,
    mut reply: crate::upstream::Reply,
    exchange: Exchange<'_>,
    limit: usize,
) {
    if exchange.converts() {
        let converted = serde_json::from_slice(&reply.body).ok().and_then(|body| {
            convert::response::convert(exchange, &body, &reply.usage, reply.status.as_u16()).ok()
        });
        let converted = converted
            .map(|(body, status)| (body.to_string().into_bytes(), status))
            .filter(|(wire, _)| wire.len() <= limit);
        let Some((body, status)) = converted else {
            let _ = sender.send(Err(Failure::upstream()));
            // The upstream may have completed and incurred cost even though the client
            // received no equivalent response. Retain its raw evidence and charge no customer.
            run.finish(
                reply.outcome,
                "upstream_failed",
                reply.response_id.as_deref(),
                reply.usage,
            )
            .await;
            return;
        };
        reply.body = body;
        reply.status = StatusCode::from_u16(status).expect("HTTP status");
    }
    let (receipt, received) = oneshot::channel();
    let sent = sender
        .send(Ok(Response::Buffered {
            status: reply.status,
            body: reply.body,
            receipt,
        }))
        .is_ok();
    let written = sent && received.await.is_ok();
    let delivery = match (reply.outcome, written) {
        ("succeeded", _) if !reply.status.is_success() => "upstream_failed",
        ("succeeded", true) => "succeeded",
        ("failed", _) => "upstream_failed",
        _ => "unknown",
    };
    run.finish(
        reply.outcome,
        delivery,
        reply.response_id.as_deref(),
        reply.usage,
    )
    .await;
}
