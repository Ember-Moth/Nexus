mod completion;
pub(crate) mod lifecycle;
mod sse;

use crate::{
    control::{
        ControlError,
        database::Gateway,
        snapshot::{Index, Rejection, Selection},
    },
    metrics::{
        self, FailoverReason, FailoverResult, Outcome, RequestTally, Transport, UpstreamTimer,
    },
    protocol::{
        Endpoint,
        convert::{self, Exchange},
        request::Generate,
        usage::UsageEvidence,
    },
    server::Node,
    upstream::{Reply, UpstreamError},
};
use http::StatusCode;
use std::{sync::Arc, time::Duration};
use tokio::{
    sync::{mpsc, oneshot},
    time::{Instant, timeout},
};
use tracing::{info, warn};
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

/// Identifiers for logs about one upstream exchange.
#[derive(Clone, Copy)]
struct Trace {
    request: Uuid,
    deployment: Uuid,
    endpoint: Endpoint,
    transport: Transport,
}

impl Trace {
    /// Log why no upstream response arrived. A client that left is logged when the request finishes.
    fn not_answered<T>(self, result: &Result<Option<Result<T, UpstreamError>>, Failure>) {
        match result {
            Ok(Some(Err(error))) => warn!(
                request_id = %self.request,
                deployment_id = %self.deployment,
                endpoint = self.endpoint.as_str(),
                transport = self.transport.as_str(),
                error = %error,
                "upstream request failed"
            ),
            Err(_) => self.lease_lost(),
            Ok(_) => {}
        }
    }
    fn lease_lost(self) {
        warn!(
            request_id = %self.request,
            deployment_id = %self.deployment,
            endpoint = self.endpoint.as_str(),
            transport = self.transport.as_str(),
            reason = "lease_lost",
            "upstream request interrupted"
        );
    }
    fn rejected(self, reason: &'static str) {
        warn!(
            request_id = %self.request,
            deployment_id = %self.deployment,
            endpoint = self.endpoint.as_str(),
            transport = self.transport.as_str(),
            reason,
            "upstream response rejected"
        );
    }
    /// Count and log what followed an attempt that provably did not execute.
    fn failover(
        self,
        attempt: usize,
        next: Option<Uuid>,
        reason: FailoverReason,
        result: FailoverResult,
    ) {
        metrics::failover(reason, result);
        warn!(
            request_id = %self.request,
            attempt = attempt as u64,
            deployment_id = %self.deployment,
            next_deployment_id = next.map(tracing::field::display),
            endpoint = self.endpoint.as_str(),
            transport = self.transport.as_str(),
            reason = reason.as_str(),
            outcome = result.as_str(),
            "upstream attempt declined"
        );
    }
    fn interrupted(self, client_gone: bool) {
        if client_gone {
            info!(
                request_id = %self.request,
                endpoint = self.endpoint.as_str(),
                transport = self.transport.as_str(),
                "client disconnected before the terminal event"
            );
        } else {
            warn!(
                request_id = %self.request,
                deployment_id = %self.deployment,
                endpoint = self.endpoint.as_str(),
                transport = self.transport.as_str(),
                "upstream stream interrupted"
            );
        }
    }
}

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
    Buffered(Reply),
    /// The upstream provably did not execute the generation. A declined status keeps its
    /// buffered reply as cost evidence and as the client's error if no attempt follows.
    Declined(Option<Reply>, FailoverReason),
    /// No usable answer; the generation may have executed or the client has left.
    Lost,
}

// The timer marks the upstream's first byte; it is the only argument added for observability.
#[allow(clippy::too_many_arguments)]
async fn open(
    node: &Node,
    run: &mut lifecycle::Run,
    selected: &Selection<'_>,
    exchange: Exchange<'_>,
    credential: &str,
    stream: bool,
    sender: &mut Channel,
    timer: &mut UpstreamTimer,
) -> Opened {
    let (dep, body) = (selected.deployment, &selected.body);
    let trace = Trace {
        request: run.reservation.id,
        deployment: dep.id,
        endpoint: exchange.client,
        transport: if stream {
            Transport::Sse
        } else {
            Transport::Http
        },
    };
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
            tokio::select! {biased;
                _ = sender.closed() => None,
                result = call => Some(result),
            }
        })
        .await;
    // Classify ambiguous replies even when there is no retry budget.
    let failover = node.max_attempts > 1;
    let response = match opened {
        Ok(Some(Ok(response))) => response,
        Ok(Some(Err(error))) if failover && error.unsent() => {
            let reason = match error {
                UpstreamError::Signing => FailoverReason::Signing,
                _ => FailoverReason::Connect,
            };
            return Opened::Declined(None, reason);
        }
        opened => {
            trace.not_answered(&opened);
            return Opened::Lost;
        }
    };
    timer.first_byte();
    if response.status() != StatusCode::BAD_GATEWAY && !crate::upstream::declined(response.status())
    {
        return Opened::Answer(response);
    }
    let reason = FailoverReason::declined(response.status().as_u16());
    // Bedrock's buffered error shape is derived from its HTTP status. Verify the
    // service error header before projecting the response into a client protocol.
    let bedrock_declined = bedrock
        && matches!(
            (
                response.status().as_u16(),
                response
                    .headers()
                    .get("x-amzn-errortype")
                    .and_then(|header| header.to_str().ok())
                    .and_then(|value| value.split(':').next())
            ),
            (429, Some("ThrottlingException")) | (503, Some("ServiceUnavailableException"))
        );
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
    match reply {
        Ok(Ok(Ok(mut reply)))
            if (bedrock_declined
                || (!bedrock
                    && crate::upstream::confirms_rejection(ep, reply.status, &reply.body)))
                && reply
                    .usage
                    .raw_usage
                    .as_ref()
                    .is_none_or(|counts| counts.values().all(|n| *n == 0)) =>
        {
            reply.outcome = "failed";
            if failover {
                Opened::Declined(Some(reply), reason)
            } else {
                Opened::Buffered(reply)
            }
        }
        Ok(Ok(Ok(mut reply))) => {
            reply.outcome = "unknown";
            Opened::Buffered(reply)
        }
        _ => Opened::Lost,
    }
}

pub async fn run(
    node: Arc<Node>,
    index: Arc<Index>,
    secret: String,
    request: Generate,
    mut sender: Channel,
    mut tally: RequestTally,
) {
    let mut selected = match index.select(&secret, &request) {
        Ok(s) => s,
        Err(e) => {
            let _ = sender.send(Err(e.into()));
            return;
        }
    };
    tally.deployment = Some(selected.deployment.id);
    let (mut reservation, mut credential) = match lifecycle::Reservation::reserve(
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
    reservation.tally = tally;
    if sender.is_closed() {
        reservation.tally.record(Outcome::ClientCancelled);
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
    let deadline = run.retry_deadline(node.failover_budget);
    let mut failed = Vec::new();
    let limit = node.upstream.response_limit();
    let transport = if request.stream {
        Transport::Sse
    } else {
        Transport::Http
    };
    loop {
        let dep = selected.deployment;
        let exchange = Exchange {
            client: request.endpoint,
            upstream: selected.upstream.representation(),
            public_model: &request.model,
        };
        let trace = Trace {
            request: run.reservation.id,
            deployment: dep.id,
            endpoint: request.endpoint,
            transport,
        };
        let mut timer = UpstreamTimer::start(request.endpoint, transport);
        let opened = open(
            &node,
            &mut run,
            &selected,
            exchange,
            &credential,
            request.stream,
            &mut sender,
            &mut timer,
        )
        .await;
        let answer = match opened {
            Opened::Answer(answer) => answer,
            Opened::Buffered(reply) => {
                deliver(run, sender, reply, exchange, limit, trace).await;
                return;
            }
            Opened::Lost => {
                let _ = sender.send(Err(Failure::upstream()));
                run.ended(None, false).await;
                return;
            }
            Opened::Declined(reply, reason) => {
                failed.push(dep.id);
                let attempt = run.attempts();
                // Nothing has been written to the client. Another candidate from the same
                // snapshot, within the frozen bounds, gets a newly committed attempt.
                // The gate checks the same conditions in the same order and names the one
                // that stopped failover.
                let gate = if run.attempts() >= node.max_attempts {
                    Some(FailoverResult::AttemptLimit)
                } else if Instant::now() >= deadline {
                    Some(FailoverResult::BudgetExhausted)
                } else if sender.is_closed() {
                    Some(FailoverResult::ClientGone)
                } else {
                    None
                };
                let next = gate
                    .is_none()
                    .then(|| index.fallback(&secret, &request, &failed, frozen))
                    .flatten();
                let candidate = next.as_ref().map(|n| n.deployment.id);
                let mut result = gate.unwrap_or(FailoverResult::NoCandidate);
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
                    match run
                        .fail_over(target, evidence, response_id, deadline, &sender)
                        .await
                    {
                        Ok(()) => {
                            if let Err(stop) = run.ready_to_retry(deadline, &sender).await {
                                trace.failover(attempt, candidate, reason, stop);
                                let respond = async {
                                    if let Some(reply) = reply {
                                        let _ = write_reply(sender, reply, exchange, limit).await;
                                    } else {
                                        let _ = sender.send(Err(Failure::upstream()));
                                    }
                                };
                                let outcome = if matches!(stop, FailoverResult::ClientGone) {
                                    "cancelled"
                                } else {
                                    "failed"
                                };
                                // Every earlier attempt was declined and the backup was never sent.
                                tokio::join!(
                                    respond,
                                    run.finish_unsent(outcome, "upstream_failed")
                                );
                                return;
                            }
                            trace.failover(attempt, candidate, reason, FailoverResult::Switched);
                            selected = next;
                            credential = key;
                            continue;
                        }
                        Err(stop) => result = stop,
                    }
                } else if candidate.is_some() {
                    result = FailoverResult::CredentialUnavailable;
                }
                trace.failover(attempt, candidate, reason, result);
                // The last declined attempt is the client's answer; nothing was executed.
                match reply {
                    Some(reply) => deliver(run, sender, reply, exchange, limit, trace).await,
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
            sse::forward(node, run, dep, exchange, answer, sender, timer).await;
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
                tokio::select! {_=sender.closed()=>None,r=reading=>Some(r)}
            })
            .await;
        drop(timer);
        if let Ok(Some(Ok(reply))) = result {
            deliver(run, sender, reply, exchange, limit, trace).await;
        } else {
            trace.not_answered(&result);
            let _ = sender.send(Err(Failure::upstream()));
            run.ended(None, false).await;
        }
        return;
    }
}

async fn deliver(
    run: lifecycle::Run,
    sender: Channel,
    reply: Reply,
    exchange: Exchange<'_>,
    limit: usize,
    trace: Trace,
) {
    let delivered = write_reply(sender, reply, exchange, limit).await;
    let (reply, written) = match delivered {
        Ok(pair) => pair,
        Err(reply) => {
            trace.rejected("conversion_failed");
            let delivery = if reply.outcome == "unknown" {
                "unknown"
            } else {
                "upstream_failed"
            };
            run.finish(
                reply.outcome,
                delivery,
                reply.response_id.as_deref(),
                reply.usage,
            )
            .await;
            return;
        }
    };
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

async fn write_reply(
    sender: Channel,
    mut reply: Reply,
    exchange: Exchange<'_>,
    limit: usize,
) -> Result<(Reply, bool), Reply> {
    if exchange.converts() {
        let converted = serde_json::from_slice(&reply.body).ok().and_then(|body| {
            convert::response::convert(exchange, &body, &reply.usage, reply.status.as_u16()).ok()
        });
        let converted = converted
            .map(|(body, status)| (body.to_string().into_bytes(), status))
            .filter(|(wire, _)| wire.len() <= limit);
        let Some((body, status)) = converted else {
            let _ = sender.send(Err(Failure::upstream()));
            return Err(reply);
        };
        reply.body = body;
        reply.status = StatusCode::from_u16(status).expect("HTTP status");
    }
    let (receipt, received) = oneshot::channel();
    let sent = sender
        .send(Ok(Response::Buffered {
            status: reply.status,
            body: std::mem::take(&mut reply.body),
            receipt,
        }))
        .is_ok();
    let written = sent && received.await.is_ok();
    Ok((reply, written))
}
