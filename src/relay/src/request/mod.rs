mod completion;
pub(crate) mod lifecycle;
mod sse;

use crate::{
    control::{
        ControlError,
        database::Gateway,
        snapshot::{Index, Rejection, Selection},
    },
    protocol::{
        convert::{self, Exchange},
        request::Generate,
        usage::UsageEvidence,
    },
    server::Node,
    upstream::Reply,
};
use http::StatusCode;
use std::{sync::Arc, time::Duration};
use tokio::{
    sync::{mpsc, oneshot},
    time::{Instant, timeout},
};
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

/// How an upstream answered the opening of one attempt.
enum Opened {
    /// The existing delivery path handles this response, including non-retryable errors.
    Answer(reqwest::Response),
    /// The upstream provably did not execute the generation. A declined status keeps its
    /// buffered reply as cost evidence and as the client's error if no attempt follows.
    Declined(Option<Reply>),
    /// No usable answer; the generation may have executed or the client has left.
    Lost,
}

async fn open(
    node: &Node,
    run: &mut lifecycle::Run,
    selected: &Selection<'_>,
    exchange: Exchange<'_>,
    credential: &str,
    stream: bool,
    sender: &mut Channel,
) -> Opened {
    let (dep, body) = (selected.deployment, &selected.body);
    let ep = exchange.upstream;
    let bedrock = dep.is_bedrock();
    let call = async {
        if bedrock {
            crate::bedrock::transport::open(&node.upstream.client, dep, credential, body, stream)
                .await
        } else {
            node.upstream
                .open(&dep.base_url, ep, credential, body, stream)
                .await
        }
    };
    let opened = run
        .reservation
        .guarded(async {
            tokio::select! {
                _ = sender.closed() => None,
                result = call => Some(result),
            }
        })
        .await;
    // Failover is disabled at one attempt; every outcome then keeps its single-attempt report.
    let failover = node.max_attempts > 1;
    let response = match opened {
        Ok(Some(Ok(response))) => response,
        Ok(Some(Err(error))) if failover && error.unsent() => return Opened::Declined(None),
        _ => return Opened::Lost,
    };
    if !failover || !crate::upstream::declined(response.status()) {
        return Opened::Answer(response);
    }
    let rid = run.reservation.id.to_string();
    let limit = node.upstream.response_limit();
    let reply = run
        .reservation
        .guarded(timeout(node.stream_idle, async {
            if bedrock {
                crate::bedrock::transport::buffered(response, exchange.public_model, &rid, limit)
                    .await
            } else {
                node.upstream.buffered(response, ep).await
            }
        }))
        .await;
    Opened::Declined(match reply {
        Ok(Ok(Ok(mut reply))) => {
            reply.outcome = "failed";
            Some(reply)
        }
        _ => None,
    })
}

pub async fn run(
    node: Arc<Node>,
    index: Arc<Index>,
    secret: String,
    request: Generate,
    mut sender: Channel,
) {
    let mut selected = match index.select(&secret, &request) {
        Ok(s) => s,
        Err(e) => {
            let _ = sender.send(Err(e.into()));
            return;
        }
    };
    let (reservation, mut credential) = match lifecycle::Reservation::reserve(
        node.clone(),
        &index,
        &selected,
        selected.upstream.representation(),
    )
    .await
    {
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
    let frozen = selected.bounds;
    let deadline = Instant::now() + node.failover_budget;
    let mut failed = Vec::new();
    let limit = node.upstream.response_limit();
    loop {
        let dep = selected.deployment;
        let exchange = Exchange {
            client: request.endpoint,
            upstream: selected.upstream.representation(),
            public_model: &request.model,
        };
        let opened = open(
            &node,
            &mut run,
            &selected,
            exchange,
            &credential,
            request.stream,
            &mut sender,
        )
        .await;
        let answer = match opened {
            Opened::Answer(answer) => answer,
            Opened::Lost => {
                let _ = sender.send(Err(Failure::upstream()));
                run.ended(None, false).await;
                return;
            }
            Opened::Declined(reply) => {
                failed.push(dep.id);
                // Nothing has been written to the client. Another candidate from the same
                // snapshot, within the frozen bounds, gets a newly committed attempt.
                let next = (run.attempts() < node.max_attempts
                    && Instant::now() < deadline
                    && !sender.is_closed())
                .then(|| index.fallback(&secret, &request, &failed, frozen))
                .flatten();
                if let Some(next) = next
                    && let Ok(key) = lifecycle::credential(&node, &index, next.deployment).await
                {
                    let target = lifecycle::Target {
                        deployment: next.deployment.id,
                        endpoint: next.upstream.representation(),
                        usage_format: next.upstream.usage_format(),
                    };
                    let evidence = reply.as_ref().map_or(
                        UsageEvidence {
                            usage_format: run.usage_format(),
                            raw_usage: None,
                        },
                        |r| r.usage.clone(),
                    );
                    let response_id = reply.as_ref().and_then(|r| r.response_id.as_deref());
                    match run.fail_over(target, evidence, response_id).await {
                        Ok(following) => {
                            run = following;
                            selected = next;
                            credential = key;
                            continue;
                        }
                        Err(current) => run = current,
                    }
                }
                // The last declined attempt is the client's answer; nothing was executed.
                match reply {
                    Some(reply) => deliver(run, sender, reply, exchange, limit).await,
                    None => {
                        let _ = sender.send(Err(Failure::upstream()));
                        let format = run.usage_format();
                        run.finish(
                            "failed",
                            "upstream_failed",
                            None,
                            UsageEvidence {
                                usage_format: format,
                                raw_usage: None,
                            },
                        )
                        .await;
                    }
                }
                return;
            }
        };
        if request.stream {
            sse::forward(node, run, dep, exchange, answer, sender).await;
            return;
        }
        let rid = run.reservation.id.to_string();
        let reading = async {
            if dep.is_bedrock() {
                crate::bedrock::transport::buffered(answer, &request.model, &rid, limit).await
            } else {
                node.upstream.buffered(answer, exchange.upstream).await
            }
        };
        let result = run
            .reservation
            .guarded(async {
                tokio::select! {_=sender.closed()=>None,r=reading=>r.ok()}
            })
            .await;
        if let Ok(Some(reply)) = result {
            deliver(run, sender, reply, exchange, limit).await;
        } else {
            let _ = sender.send(Err(Failure::upstream()));
            run.ended(None, false).await;
        }
        return;
    }
}

async fn deliver(
    run: lifecycle::Run,
    sender: Channel,
    mut reply: Reply,
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
