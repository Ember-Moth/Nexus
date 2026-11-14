use super::{
    Failure, cancel,
    completion::{Attempt, confirmed_tokens, finish_lease},
};
use crate::{
    control::{
        limits::{Admission, Lease, LimitError},
        snapshot::{Index, Selection},
    },
    protocol::{Endpoint, stream::Completion, usage::UsageEvidence},
    server::Node,
};
use http::StatusCode;
use std::{future::Future, sync::Arc};
use tokio::time::{Instant, sleep_until};
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
}

impl Reservation {
    pub async fn reserve(
        node: Arc<Node>,
        index: &Index,
        selected: &Selection<'_>,
        endpoint: Endpoint,
    ) -> Result<(Self, Zeroizing<String>), Failure> {
        let dep = selected.deployment;
        let reference = &index.credentials[&dep.credential_id];
        let secret = node.db.credential(index.revision, reference).await?.open(
            index.cluster,
            reference,
            &node.private,
        )?;
        let id = Uuid::new_v4();
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
        let reserve = || {
            node.db
                .reserve(id, selected.key.id, selected.model, index.revision, bounds)
        };
        let first = reserve().await;
        let retry = first.as_ref().is_err_and(|e| e.retryable());
        if let Err(e) = if retry { reserve().await } else { first } {
            if retry && !cancel(&node.db, id).await {
                eprintln!("unconfirmed reservation cleanup {id}");
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
                usage_format: selected.upstream.usage_format(),
                lease,
            },
            secret,
        ))
    }
    pub async fn abort(self) {
        if !cancel(&self.node.db, self.id).await {
            eprintln!("unconfirmed reservation cleanup {}", self.id);
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
            reservation: self,
            attempt,
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
    pub reservation: Reservation,
    attempt: Attempt,
}
impl Run {
    pub async fn not_sent(self) {
        let r = self.reservation;
        finish_lease(&r.node.limits, &r.lease, Some(0), r.id).await;
        self.attempt
            .complete(
                &r.node.db,
                "cancelled",
                "client_cancelled",
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
        let r = self.reservation;
        // A client may send its next turn immediately after the terminal frame.
        // Release this turn's quota before accepting it. The node permit still bounds pending reports.
        finish_lease(
            &r.node.limits,
            &r.lease,
            confirmed_tokens(r.endpoint, &done.usage),
            r.id,
        )
        .await;
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
        let r = self.reservation;
        finish_lease(
            &r.node.limits,
            &r.lease,
            confirmed_tokens(r.endpoint, &usage),
            r.id,
        )
        .await;
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
        ("failed", _) => "upstream_failed",
        ("cancelled", true) => "client_cancelled",
        _ => "unknown",
    }
}
