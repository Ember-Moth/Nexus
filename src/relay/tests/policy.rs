//! Cluster policy and resource limits delivered in the configuration snapshot.
use std::time::Duration;

use nexus_relay::control::{database::Snapshot, snapshot::Index};
use serde_json::{Value, json};
use uuid::Uuid;

fn snapshot(extra: Value) -> Snapshot {
    let mut resources = json!({"models":[],"key_verifiers":[],"deployments":[],
        "access_policies":[],"quota_policies":[],"routes":[],"credential_refs":[],
        "cluster_policy": {"max_attempts": 2, "failover_budget_ms": 10000,
            "purge_grace_seconds": 604800, "stream_lifetime_seconds": 1800},
        "resource_limits": {"default": {"resources_per_key": 10000,
            "resources_per_organization": 100000, "vector_stores_per_key": 100,
            "vector_stores_per_organization": 1000}, "organizations": []}});
    for (k, v) in extra.as_object().unwrap() {
        resources[k] = v.clone();
    }
    Snapshot {
        applied_revision: 0,
        node_id: Uuid::new_v4(),
        cluster_id: Uuid::new_v4(),
        revision: 3,
        digest: String::new(),
        required_capabilities: Vec::new(),
        resources,
        key_set: None,
    }
}

#[test]
fn a_snapshot_must_carry_both_policy_sections() {
    assert!(Index::build(&mut snapshot(json!({})), Default::default()).is_ok());
    for section in ["cluster_policy", "resource_limits"] {
        let mut incomplete = snapshot(json!({}));
        incomplete
            .resources
            .as_object_mut()
            .unwrap()
            .remove(section);
        assert!(Index::build(&mut incomplete, Default::default()).is_err());
    }
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
    let index = Index::build(&mut snapshot(extra), Default::default()).unwrap();
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
fn out_of_bounds_policy_is_rejected() {
    let excessive = json!({"cluster_policy": {"max_attempts": 9, "failover_budget_ms": 1000,
        "purge_grace_seconds": 10, "stream_lifetime_seconds": 10}});
    assert!(Index::build(&mut snapshot(excessive), Default::default()).is_err());
    let zero = json!({"resource_limits": {"default": {"resources_per_key": 0,
        "resources_per_organization": 1, "vector_stores_per_key": 1,
        "vector_stores_per_organization": 1}, "organizations": []}});
    assert!(Index::build(&mut snapshot(zero), Default::default()).is_err());
}
