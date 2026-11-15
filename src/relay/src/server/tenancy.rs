//! Node-local fairness between organizations, and Redis admission for resource APIs.
//!
//! The node-wide capacity semaphore alone lets one organization's slow uploads or long
//! streams exhaust a node for every tenant. Each organization therefore holds at most a
//! configured number of request and stream slots, resource API calls pass the same Redis
//! RPM and concurrency policies as generations, and creations respect resource counts.
use super::Node;
use crate::{
    control::{
        limits::{Admission, Lease, LimitError, Limits},
        snapshot::{Index, Key, Metric, Quota},
    },
    protocol::tokens::ReservationBounds,
    request::Failure,
};
use http::StatusCode;
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
};
use tokio::sync::OwnedSemaphorePermit;
use tokio_util::task::TaskTracker;
use uuid::Uuid;

pub(crate) struct TenantSlots {
    counts: Mutex<HashMap<Uuid, usize>>,
    limit: usize,
}

impl TenantSlots {
    pub fn new(limit: usize) -> Arc<Self> {
        Arc::new(Self {
            counts: Mutex::new(HashMap::new()),
            limit,
        })
    }
    pub fn enter(self: &Arc<Self>, organization: Uuid) -> Option<TenantPermit> {
        let mut counts = self.counts.lock().unwrap();
        let count = counts.entry(organization).or_default();
        if *count >= self.limit {
            return None;
        }
        *count += 1;
        Some(TenantPermit {
            pool: self.clone(),
            organization,
        })
    }
}

pub(crate) struct TenantPermit {
    pool: Arc<TenantSlots>,
    organization: Uuid,
}

impl Drop for TenantPermit {
    fn drop(&mut self) {
        let mut counts = self.pool.counts.lock().unwrap();
        if let Some(n) = counts.get_mut(&self.organization) {
            *n -= 1;
            if *n == 0 {
                counts.remove(&self.organization);
            }
        }
    }
}

pub(super) fn tenant_limited() -> Failure {
    Failure(StatusCode::TOO_MANY_REQUESTS, "tenant_concurrency_limit")
}

/// A node slot, an organization slot and a Redis lease for one resource API call.
pub(super) struct Admitted {
    _slot: OwnedSemaphorePermit,
    _tenant: TenantPermit,
    lease: Option<Lease>,
    limits: Limits,
    tasks: TaskTracker,
}

impl Drop for Admitted {
    fn drop(&mut self) {
        if let Some(lease) = self.lease.take() {
            let limits = self.limits.clone();
            self.tasks.spawn(async move {
                let _ = limits.finish(&lease, Some(0)).await;
            });
        }
    }
}

/// Admit a resource API call. It counts one request toward the organization's RPM policies.
/// Concurrency is bounded by the node's organization slots: the Redis concurrency quota
/// governs generations, which metered resource calls such as batches lease separately.
pub(super) async fn admit(node: &Node, index: &Index, key: &Key) -> Result<Admitted, Failure> {
    let tenant = node
        .tenant_requests
        .enter(key.organization_id)
        .ok_or_else(tenant_limited)?;
    let slot = node
        .capacity
        .clone()
        .try_acquire_owned()
        .map_err(|_| Failure::unavailable())?;
    let rpm: Vec<Quota> = index
        .quotas
        .iter()
        .filter(|q| matches!(q.metric, Metric::Rpm))
        .cloned()
        .collect();
    let lease = node
        .limits
        .acquire(Admission {
            key,
            request: Uuid::new_v4(),
            node: index.node,
            model: Uuid::nil(),
            revision: index.revision,
            bounds: ReservationBounds {
                guardrail_units: 0,
                searches: 0,
                file_searches: 0,
                interpreter: None,
                execution_mode: crate::protocol::execution::ExecutionMode::None,
                execution_seconds: 0,
                inputs: 0,
                outputs: 0,
            },
            quotas: &rpm,
            ttl_ms: node.lease_ms,
        })
        .await
        .map_err(|e| match e {
            LimitError::Limited => Failure(StatusCode::TOO_MANY_REQUESTS, "rate_limited"),
            _ => Failure::unavailable(),
        })?;
    Ok(Admitted {
        _slot: slot,
        _tenant: tenant,
        lease: Some(lease),
        limits: node.limits.clone(),
        tasks: node.tasks.clone(),
    })
}

/// Refuse a creation that would exceed the key's or the organization's resource count.
///
/// The limits come from the snapshot: the platform defaults, or the organization's own
/// override. The database counts no further than them.
pub(super) async fn room(
    node: &Node,
    index: &Index,
    key: &Key,
    kind: &'static str,
) -> Result<(), Failure> {
    let (per_key, per_org) = index.resource_limits(key.organization_id).of(kind);
    let keys = index.organization_keys(key.organization_id);
    let usage = node
        .db
        .resource_usage(kind, key.id, index.revision, &keys, (per_key, per_org))
        .await?;
    if usage.key >= per_key || usage.organization >= per_org {
        return Err(Failure(
            StatusCode::TOO_MANY_REQUESTS,
            "resource_limit_exceeded",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_organization_holds_at_most_its_slots() {
        let slots = TenantSlots::new(2);
        let (a, b) = (Uuid::new_v4(), Uuid::new_v4());
        let first = slots.enter(a).unwrap();
        let _second = slots.enter(a).unwrap();
        assert!(slots.enter(a).is_none());
        assert!(slots.enter(b).is_some());
        drop(first);
        assert!(slots.enter(a).is_some());
    }
}
