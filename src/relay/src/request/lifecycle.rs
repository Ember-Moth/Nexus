use super::{
    Failure, cancel,
    completion::{Attempt, confirmed_tokens, finish_lease},
};
use crate::{
    control::{
        limits::{Admission, Lease, LimitError},
        snapshot::{Deployment, Index, Selection},
    },
    metrics::{FailoverResult, RequestTally},
    protocol::{Endpoint, stream::Completion, usage::UsageEvidence},
    server::Node,
};
use http::StatusCode;
use std::{future::Future, sync::Arc};
use tokio::time::{Instant, sleep_until};
use tracing::warn;
use uuid::Uuid;
use zeroize::Zeroizing;

pub struct Reservation {
    node: Arc<Node>,
    pub id: Uuid,
    revision: i64,
    endpoint: Endpoint,
    deployment: Uuid,
    usage_format: &'static str,
    lease: Lease,
    pub tally: RequestTally,
}

/// Open the cluster envelope for a deployment's upstream credential.
pub async fn credential(
    node: &Node,
    index: &Index,
    dep: &Deployment,
) -> Result<Zeroizing<String>, Failure> {
    let reference = &index.credentials[&dep.credential_id];
    Ok(node.db.credential(index.revision, reference).await?.open(
        index.cluster,
        reference,
        &node.private,
    )?)
}

/// The deployment a later attempt of the same request is bound to.
pub struct Target {
    pub deployment: Uuid,
    pub endpoint: Endpoint,
    pub usage_format: &'static str,
}

impl Reservation {
    pub async fn reserve(
        id: Uuid,
        node: Arc<Node>,
        index: &Index,
        selected: &Selection<'_>,
        endpoint: Endpoint,
    ) -> Result<(Self, Zeroizing<String>), Failure> {
        let dep = selected.deployment;
        let secret = credential(&node, index, dep).await?;
        let bounds = selected.bounds;
        let lease = node
            .limits
            .acquire(Admission {
                key: selected.key,
                request: id,
                node: index.node,
                model: selected.model,
                revision: index.revision,
                bounds,
                quotas: &index.quotas,
                ttl_ms: node.lease_ms,
            })
            .await
            .map_err(|e| match e {
                LimitError::Limited => Failure(StatusCode::TOO_MANY_REQUESTS, "rate_limited"),
                _ => Failure::unavailable(),
            })?;
        let reserve = || node.db.reserve_generation(id, index.revision, selected);
        let first = reserve().await;
        let retry = first.as_ref().is_err_and(|e| e.retryable());
        if let Err(e) = if retry { reserve().await } else { first } {
            if retry && !cancel(&node.db, id).await {
                warn!(request_id = %id, "reservation cancellation unconfirmed");
            }
            finish_lease(&node.limits, &lease, Some(0), id).await;
            return Err(e.into());
        }
        Ok((
            Self {
                node,
                id,
                revision: index.revision,
                endpoint,
                deployment: dep.id,
                usage_format: selected.usage_format(),
                lease,
                tally: RequestTally::detached(),
            },
            secret,
        ))
    }
    pub async fn abort(self) {
        if !cancel(&self.node.db, self.id).await {
            warn!(request_id = %self.id, "reservation cancellation unconfirmed");
        }
        finish_lease(&self.node.limits, &self.lease, Some(0), self.id).await;
    }
    pub async fn start(mut self) -> Result<Run, Failure> {
        if self.node.limits.renew(&mut self.lease).await.is_err() {
            self.abort().await;
            return Err(Failure::unavailable());
        }
        let attempt = Attempt {
            request: self.id,
            attempt: Uuid::new_v4(),
            revision: self.revision,
            usage_format: self.usage_format,
            started: Instant::now(),
            others: Vec::new(),
            closable: true,
            tally: std::mem::take(&mut self.tally),
        };
        let start = || {
            self.node
                .db
                .start_attempt(self.id, attempt.attempt, 1, self.deployment)
        };
        let first = start().await;
        let retry = first.as_ref().is_err_and(|e| e.retryable());
        if let Err(e) = if retry { start().await } else { first } {
            if !cancel(&self.node.db, self.id).await
                && retry
                && let Ok(Some(status)) = self.node.db.status(self.id).await
                && status["attempts"]
                    .as_array()
                    .is_some_and(|v| v.iter().any(|r| r["id"] == attempt.attempt.to_string()))
            {
                attempt.unknown(&self.node.db).await;
            }
            finish_lease(&self.node.limits, &self.lease, Some(0), self.id).await;
            return Err(e.into());
        }
        Ok(Run {
            history: None,
            approximation: Default::default(),
            reservation: self,
            attempt,
            spent: Some(0),
        })
    }

    pub async fn guarded<F: Future>(&mut self, work: F) -> Result<F::Output, Failure> {
        tokio::pin!(work);
        loop {
            if Instant::now() >= self.lease.safe_until {
                return Err(Failure::unavailable());
            }
            tokio::select! {biased;
                _=sleep_until(self.lease.renewal_at())=>self.node.limits.renew(&mut self.lease).await.map_err(|_|Failure::unavailable())?,
                result=&mut work=>return Ok(result),
            }
        }
    }
}

pub struct Run {
    pub(super) approximation: crate::protocol::approximation::Delivery,
    pub(super) history: Option<crate::protocol::history::Writer>,
    pub reservation: Reservation,
    attempt: Attempt,
    /// Confirmed physical tokens of earlier attempts, or None once any was unconfirmed.
    spent: Option<u64>,
}
impl Run {
    pub fn stored_stream(&self) -> crate::control::stored::StoredStream {
        crate::control::stored::StoredStream::new(
            self.reservation.node.db.clone(),
            self.reservation.id,
            self.attempt.attempt,
        )
    }
    pub fn attempt_id(&self) -> Uuid {
        self.attempt.attempt
    }

    pub fn attempts(&self) -> usize {
        self.attempt.others.len() + 1
    }
    /// Last admission check before polling a backup POST. Re-check the deadline
    /// after Redis answers as either the control calls or the renewal can take time.
    pub async fn ready_to_retry(
        &mut self,
        deadline: Instant,
        sender: &super::Channel,
    ) -> Result<(), FailoverResult> {
        if sender.is_closed() {
            return Err(FailoverResult::ClientGone);
        }
        if Instant::now() >= deadline {
            return Err(FailoverResult::BudgetExhausted);
        }
        let lease = &mut self.reservation.lease;
        match tokio::time::timeout_at(deadline, self.reservation.node.limits.renew(lease)).await {
            Err(_) => return Err(FailoverResult::BudgetExhausted),
            Ok(Err(_)) => return Err(FailoverResult::StartFailed),
            Ok(Ok(())) => {}
        }
        if sender.is_closed() {
            return Err(FailoverResult::ClientGone);
        }
        let now = Instant::now();
        if now >= deadline {
            return Err(FailoverResult::BudgetExhausted);
        }
        if now >= lease.safe_until {
            return Err(FailoverResult::StartFailed);
        }
        Ok(())
    }

    pub fn retry_deadline(&self, budget: std::time::Duration) -> Instant {
        self.attempt.started + budget
    }

    pub fn usage_format(&self) -> &'static str {
        self.reservation.usage_format
    }
    /// Commit the next attempt of this request, bound to `next`, after the current attempt
    /// provably did not execute. The rejected attempt then reports its own `attempt.finished`
    /// as `declined`; only the close lists every attempt. On `Err` no new attempt can be sent
    /// and the caller finishes this run. An attempt committed with an unknown reply was never
    /// sent: it is reported as declined and listed by that close; when even its existence is
    /// unknown the close is withheld.
    pub async fn fail_over(
        &mut self,
        next: Target,
        evidence: UsageEvidence,
        response_id: Option<&str>,
        deadline: Instant,
        sender: &super::Channel,
    ) -> Result<(), FailoverResult> {
        self.ready_to_retry(deadline, sender).await?;
        let node = self.reservation.node.clone();
        let rid = self.reservation.id;
        let mut others = self.attempt.others.clone();
        others.push(self.attempt.attempt);
        let mut attempt = Attempt {
            request: rid,
            attempt: Uuid::new_v4(),
            revision: self.reservation.revision,
            usage_format: next.usage_format,
            started: Instant::now(),
            closable: true,
            others,
            tally: RequestTally::detached(),
        };
        let sequence = i32::try_from(attempt.others.len() + 1).expect("bounded attempts");
        let start = || {
            node.db
                .start_attempt(rid, attempt.attempt, sequence, next.deployment)
        };
        let first = start().await;
        let retry = first.as_ref().is_err_and(|e| e.retryable());
        let started = if retry { start().await } else { first };
        if started.is_err() {
            // A definitive rejection started nothing. After an unknown commit the attempt
            // may exist without ever having been sent; the close must list it if it does.
            if retry {
                match node.db.status(rid).await {
                    Ok(Some(status)) => {
                        if status["attempts"].as_array().is_some_and(|v| {
                            v.iter().any(|r| r["id"] == attempt.attempt.to_string())
                        }) {
                            attempt
                                .declined(
                                    &node.db,
                                    None,
                                    UsageEvidence {
                                        usage_format: next.usage_format,
                                        raw_usage: None,
                                    },
                                )
                                .await;
                            self.attempt.others.push(attempt.attempt);
                        }
                    }
                    _ => self.attempt.closable = false,
                }
            }
            return Err(FailoverResult::StartFailed);
        }
        let spent = self
            .spent
            .zip(confirmed_tokens(self.reservation.endpoint, &evidence))
            .map(|(a, b)| a + b);
        self.attempt.declined(&node.db, response_id, evidence).await;
        // The request's tally follows its open attempt and is recorded once, at the close.
        attempt.tally = std::mem::take(&mut self.attempt.tally);
        attempt.tally.deployment = Some(next.deployment);
        let reservation = &mut self.reservation;
        reservation.deployment = next.deployment;
        reservation.endpoint = next.endpoint;
        reservation.usage_format = next.usage_format;
        self.attempt = attempt;
        self.spent = spent;
        Ok(())
    }
    fn tokens(&self, usage: &UsageEvidence) -> Option<u64> {
        self.spent
            .zip(confirmed_tokens(self.reservation.endpoint, usage))
            .map(|(a, b)| a + b)
    }
    pub async fn background_submitted(self, receipt: Option<&UsageEvidence>) {
        // Only a terminal receipt can release the unused part of the reserved TPM.
        let tokens = receipt.and_then(|usage| self.tokens(usage));
        let r = self.reservation;
        finish_lease(&r.node.limits, &r.lease, tokens, r.id).await;
    }
    pub async fn not_sent(self) {
        self.finish_unsent("cancelled", "client_cancelled").await;
    }

    pub async fn finish_unsent(self, outcome: &str, delivery: &str) {
        let r = self.reservation;
        finish_lease(&r.node.limits, &r.lease, self.spent, r.id).await;
        self.attempt
            .complete(
                &r.node.db,
                outcome,
                delivery,
                false,
                None,
                UsageEvidence {
                    usage_format: r.usage_format,
                    raw_usage: None,
                },
            )
            .await;
    }

    pub async fn finish_background(
        self,
        done: Completion,
        written: bool,
        permit: tokio::sync::OwnedSemaphorePermit,
    ) {
        let tokens = self.tokens(&done.usage);
        let r = self.reservation;
        // A client may send its next turn immediately after the terminal frame.
        // Release this turn's quota before accepting it. The node permit still bounds pending reports.
        finish_lease(&r.node.limits, &r.lease, tokens, r.id).await;
        let tracker = r.node.tasks.clone();
        tracker.spawn(async move {
            let _permit = permit;
            let outcome = done.outcome(r.endpoint);
            self.attempt
                .complete(
                    &r.node.db,
                    outcome,
                    delivery(outcome, written),
                    written && matches!(outcome, "succeeded" | "cancelled"),
                    done.response_id.as_deref(),
                    done.usage,
                )
                .await;
        });
    }
    pub async fn finish(
        self,
        outcome: &str,
        delivery: &str,
        response_id: Option<&str>,
        usage: UsageEvidence,
    ) {
        let tokens = self.tokens(&usage);
        let r = self.reservation;
        finish_lease(&r.node.limits, &r.lease, tokens, r.id).await;
        self.attempt
            .complete(
                &r.node.db,
                outcome,
                delivery,
                matches!(delivery, "succeeded" | "client_cancelled"),
                response_id,
                usage,
            )
            .await;
    }
    pub async fn ended(self, done: Option<Completion>, written: bool) {
        let ep = self.reservation.endpoint;
        let format = self.reservation.usage_format;
        if let Some(done) = done {
            let outcome = done.outcome(ep);
            let delivery = delivery(outcome, written);
            self.finish(outcome, delivery, done.response_id.as_deref(), done.usage)
                .await;
        } else {
            self.finish(
                "unknown",
                "unknown",
                None,
                UsageEvidence {
                    usage_format: format,
                    raw_usage: None,
                },
            )
            .await;
        }
    }
}

fn delivery(outcome: &str, written: bool) -> &'static str {
    match (outcome, written) {
        ("succeeded", true) => "succeeded",
        ("failed" | "declined", _) => "upstream_failed",
        ("cancelled", true) => "client_cancelled",
        _ => "unknown",
    }
}

#[cfg(test)]
mod tests {
    use super::delivery;
    use crate::upstream::DECLINED;

    #[test]
    fn declined_and_failed_attempts_deliver_upstream_failed() {
        for written in [true, false] {
            assert_eq!(delivery(DECLINED, written), "upstream_failed");
            assert_eq!(delivery("failed", written), "upstream_failed");
            assert_eq!(delivery("unknown", written), "unknown");
        }
        assert_eq!(delivery("succeeded", true), "succeeded");
        assert_eq!(delivery("cancelled", true), "client_cancelled");
        assert_eq!(delivery("succeeded", false), "unknown");
    }
}
