//! Cluster policy and resource limits delivered in the configuration snapshot.
use std::time::Duration;

use nexus_relay::control::{
    database::Snapshot,
    policy::{CAPABILITY, ClusterPolicy, ResourceLimits},
    snapshot::{CAPABILITIES, Index},
};
use serde_json::{Value, json};
use uuid::Uuid;

fn snapshot(extra: Value, capabilities: &[&str]) -> Snapshot {
    let mut resources = json!({"models":[],"key_verifiers":[],"deployments":[],
        "access_policies":[],"quota_policies":[],"routes":[],"credential_refs":[]});
    for (k, v) in extra.as_object().unwrap() {
        resources[k] = v.clone();
    }
    Snapshot {
        applied_revision: 0,
        node_id: Uuid::new_v4(),
        cluster_id: Uuid::new_v4(),
        revision: 3,
        digest: String::new(),
        required_capabilities: capabilities.iter().map(|c| c.to_string()).collect(),
        resources,
        key_set: None,
    }
}

#[test]
fn nodes_announce_the_cluster_policy_capability() {
    assert!(CAPABILITIES.contains(&CAPABILITY));
}

#[test]
fn a_snapshot_without_policy_keeps_the_previous_node_defaults() {
    let index = Index::build(&mut snapshot(json!({}), &[]), Default::default()).unwrap();
    assert_eq!(index.policy, ClusterPolicy::default());
    assert_eq!(index.policy.max_attempts(), 2);
    assert_eq!(
        index.policy.failover_budget(),
        Duration::from_millis(10_000)
    );
    assert_eq!(index.policy.stream_lifetime(), Duration::from_secs(1800));
    assert_eq!(index.policy.purge_grace_seconds(), 7 * 24 * 3600);
    assert_eq!(
        index.resource_limits(Uuid::new_v4()),
        ResourceLimits::default()
    );
}

#[test]
fn published_policy_and_organization_limits_take_effect() {
    let (tuned, other) = (Uuid::new_v4(), Uuid::new_v4());
    let extra = json!({
        "cluster_policy": {"max_attempts": 1, "failover_budget_ms": 250,
            "purge_grace_seconds": 0, "stream_lifetime_seconds": 60},
        "resource_limits": {
            "default": {"resources_per_key": 20, "resources_per_organization": 200,
                "vector_stores_per_key": 2, "vector_stores_per_organization": 20},
            "organizations": [{"organization_id": tuned, "resources_per_key": 1,
                "resources_per_organization": 2, "vector_stores_per_key": 1,
                "vector_stores_per_organization": 1}],
        },
    });
    let index = Index::build(&mut snapshot(extra, &[CAPABILITY]), Default::default()).unwrap();
    assert_eq!(
        index.policy.max_attempts(),
        1,
        "one attempt disables failover"
    );
    assert_eq!(index.policy.failover_budget(), Duration::from_millis(250));
    assert_eq!(index.policy.stream_lifetime(), Duration::from_secs(60));
    assert_eq!(index.policy.purge_grace_seconds(), 0);
    assert_eq!(index.resource_limits(tuned).of("file"), (1, 2));
    assert_eq!(index.resource_limits(tuned).of("vector_store"), (1, 1));
    assert_eq!(index.resource_limits(other).of("conversation"), (20, 200));
    assert_eq!(index.resource_limits(other).of("vector_store"), (2, 20));
}

#[test]
fn policy_without_its_capability_or_out_of_bounds_is_rejected() {
    let policy = json!({"cluster_policy": {"max_attempts": 3, "failover_budget_ms": 1000,
        "purge_grace_seconds": 10, "stream_lifetime_seconds": 10}});
    assert!(Index::build(&mut snapshot(policy.clone(), &[]), Default::default()).is_err());
    assert!(Index::build(&mut snapshot(policy, &[CAPABILITY]), Default::default()).is_ok());
    let excessive = json!({"cluster_policy": {"max_attempts": 9, "failover_budget_ms": 1000,
        "purge_grace_seconds": 10, "stream_lifetime_seconds": 10}});
    assert!(Index::build(&mut snapshot(excessive, &[CAPABILITY]), Default::default()).is_err());
    let limits = json!({"resource_limits": {"default": {"resources_per_key": 1,
        "resources_per_organization": 1, "vector_stores_per_key": 1,
        "vector_stores_per_organization": 1}, "organizations": []}});
    assert!(Index::build(&mut snapshot(limits, &[]), Default::default()).is_err());
}
