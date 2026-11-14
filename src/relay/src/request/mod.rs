mod completion;
pub(crate) mod lifecycle;
mod sse;

use crate::{
    control::{
        ControlError,
        database::Gateway,
        snapshot::{Index, Rejection},
    },
    protocol::request::Generate,
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
    let endpoint = request.endpoint;
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
    let base = &selected.deployment.base_url;
    if request.stream {
        sse::forward(
            node,
            run,
            base,
            endpoint,
            &credential,
            &selected.body,
            sender,
        )
        .await;
        return;
    }
    let result=run.reservation.guarded(async{
        tokio::select!{_=sender.closed()=>None,r=node.upstream.generate(base,endpoint,&credential,&selected.body)=>r.ok()}
    }).await;
    if let Ok(Some(reply)) = result {
        deliver(run, sender, reply).await;
    } else {
        let _ = sender.send(Err(Failure::upstream()));
        run.ended(None, false).await;
    }
}

async fn deliver(run: lifecycle::Run, sender: Channel, reply: crate::upstream::Reply) {
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
