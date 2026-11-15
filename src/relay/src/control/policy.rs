//! Cluster-wide policy delivered in the configuration snapshot.
//!
//! The control plane stores these values (cluster policy defaults and overrides in
//! publication, resource count defaults and per-organization overrides in catalog) and puts
//! them into a snapshot only once they differ from the built-in values below, together with
//! the required capability `relay.cluster-policy.v1`. A snapshot without them runs with the
//! built-in values. Keep them equal to the control plane's `RELAY_POLICY_DEFAULTS` and
//! `RELAY_RESOURCE_DEFAULTS`.
use std::{collections::HashMap, ops::RangeInclusive, time::Duration};

use serde::Deserialize;
use uuid::Uuid;

use super::ControlError;

pub const CAPABILITY: &str = "relay.cluster-policy.v1";
/// Bounds for `ClusterPolicy::max_attempts`.
pub const MAX_ATTEMPTS: RangeInclusive<u32> = 1..=4;
const FAILOVER_BUDGET_MS: RangeInclusive<u64> = 100..=60_000;
const MAX_COUNT: u64 = 2_000_000_000;

/// How every node of the cluster runs requests and purges.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ClusterPolicy {
    /// Attempts per HTTP generation; 1 disables failover.
    pub max_attempts: u32,
    /// No further attempt starts once this much time has passed since the first one.
    pub failover_budget_ms: u64,
    /// How long a key stays unusable before its upstream copies are deleted.
    pub purge_grace_seconds: u32,
    /// Longest time a resumed response stream may stay open.
    pub stream_lifetime_seconds: u64,
}

impl Default for ClusterPolicy {
    fn default() -> Self {
        Self {
            max_attempts: 2,
            failover_budget_ms: 10_000,
            purge_grace_seconds: 7 * 24 * 3600,
            stream_lifetime_seconds: 1800,
        }
    }
}

impl ClusterPolicy {
    pub fn max_attempts(&self) -> usize {
        self.max_attempts as usize
    }
    pub fn failover_budget(&self) -> Duration {
        Duration::from_millis(self.failover_budget_ms)
    }
    pub fn stream_lifetime(&self) -> Duration {
        Duration::from_secs(self.stream_lifetime_seconds)
    }
    pub fn purge_grace_seconds(&self) -> i32 {
        self.purge_grace_seconds as i32
    }
    fn valid(&self) -> bool {
        MAX_ATTEMPTS.contains(&self.max_attempts)
            && FAILOVER_BUDGET_MS.contains(&self.failover_budget_ms)
            && self.purge_grace_seconds <= i32::MAX as u32
            && (1..=i32::MAX as u64).contains(&self.stream_lifetime_seconds)
    }
}

/// Live resources a key, and all keys of its organization together, may hold in the
/// cluster. `resources_*` apply to each of files, conversations, containers and batches.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
pub struct ResourceLimits {
    pub resources_per_key: u64,
    pub resources_per_organization: u64,
    pub vector_stores_per_key: u64,
    pub vector_stores_per_organization: u64,
}

impl Default for ResourceLimits {
    fn default() -> Self {
        Self {
            resources_per_key: 10_000,
            resources_per_organization: 100_000,
            vector_stores_per_key: 100,
            vector_stores_per_organization: 1000,
        }
    }
}

impl ResourceLimits {
    /// (per key, per organization) for one resource kind.
    pub fn of(&self, kind: &str) -> (u64, u64) {
        if kind == "vector_store" {
            (
                self.vector_stores_per_key,
                self.vector_stores_per_organization,
            )
        } else {
            (self.resources_per_key, self.resources_per_organization)
        }
    }
    fn valid(&self) -> bool {
        [
            self.resources_per_key,
            self.resources_per_organization,
            self.vector_stores_per_key,
            self.vector_stores_per_organization,
        ]
        .iter()
        .all(|v| (1..=MAX_COUNT).contains(v))
    }
}

// serde cannot deny unknown fields next to a flattened struct.
#[derive(Deserialize)]
pub(super) struct OrganizationLimits {
    organization_id: Uuid,
    #[serde(flatten)]
    limits: ResourceLimits,
}

/// Snapshot section: platform defaults and the effective limits of overridden organizations.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ResourceSection {
    default: ResourceLimits,
    organizations: Vec<OrganizationLimits>,
}

/// The resource limits of every organization in a snapshot.
#[derive(Default)]
pub struct ResourceTable {
    default: ResourceLimits,
    organizations: HashMap<Uuid, ResourceLimits>,
}

impl ResourceTable {
    pub fn get(&self, organization: Uuid) -> ResourceLimits {
        self.organizations
            .get(&organization)
            .copied()
            .unwrap_or(self.default)
    }
}

/// Validate the optional policy sections of a snapshot against its required capabilities.
pub(super) fn install(
    policy: Option<ClusterPolicy>,
    resources: Option<ResourceSection>,
    required: &[String],
) -> Result<(ClusterPolicy, ResourceTable), ControlError> {
    if (policy.is_some() || resources.is_some()) && !required.iter().any(|c| c == CAPABILITY) {
        return Err(ControlError::Invalid("cluster policy capability"));
    }
    let policy = policy.unwrap_or_default();
    if !policy.valid() {
        return Err(ControlError::Invalid("cluster policy"));
    }
    let mut table = ResourceTable::default();
    if let Some(section) = resources {
        if !section.default.valid() {
            return Err(ControlError::Invalid("resource limits"));
        }
        table.default = section.default;
        for org in section.organizations {
            if !org.limits.valid()
                || table
                    .organizations
                    .insert(org.organization_id, org.limits)
                    .is_some()
            {
                return Err(ControlError::Invalid("resource limits"));
            }
        }
    }
    Ok((policy, table))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn required() -> Vec<String> {
        vec![CAPABILITY.to_string()]
    }

    #[test]
    fn snapshots_without_policy_run_with_the_built_in_defaults() {
        let (policy, table) = install(None, None, &[]).unwrap();
        assert_eq!(policy, ClusterPolicy::default());
        assert_eq!(policy.max_attempts(), 2);
        assert_eq!(policy.failover_budget(), Duration::from_secs(10));
        assert_eq!(table.get(Uuid::new_v4()), ResourceLimits::default());
    }

    #[test]
    fn organizations_override_the_platform_defaults() {
        let org = Uuid::new_v4();
        let section: ResourceSection = serde_json::from_value(json!({
            "default": {"resources_per_key": 5, "resources_per_organization": 50,
                "vector_stores_per_key": 1, "vector_stores_per_organization": 2},
            "organizations": [{"organization_id": org, "resources_per_key": 1,
                "resources_per_organization": 3, "vector_stores_per_key": 1,
                "vector_stores_per_organization": 2}],
        }))
        .unwrap();
        let (_, table) = install(None, Some(section), &required()).unwrap();
        assert_eq!(table.get(org).of("file"), (1, 3));
        assert_eq!(table.get(Uuid::new_v4()).of("file"), (5, 50));
        assert_eq!(table.get(Uuid::new_v4()).of("vector_store"), (1, 2));
    }

    #[test]
    fn policy_needs_its_capability_and_valid_values() {
        let policy = ClusterPolicy {
            max_attempts: 1,
            ..ClusterPolicy::default()
        };
        assert!(install(Some(policy), None, &[]).is_err());
        assert_eq!(install(Some(policy), None, &required()).unwrap().0, policy);
        for invalid in [
            json!({"max_attempts": 5, "failover_budget_ms": 10000,
                "purge_grace_seconds": 0, "stream_lifetime_seconds": 1}),
            json!({"max_attempts": 2, "failover_budget_ms": 99,
                "purge_grace_seconds": 0, "stream_lifetime_seconds": 1}),
            json!({"max_attempts": 2, "failover_budget_ms": 100,
                "purge_grace_seconds": 0, "stream_lifetime_seconds": 0}),
        ] {
            let policy: ClusterPolicy = serde_json::from_value(invalid).unwrap();
            assert!(install(Some(policy), None, &required()).is_err());
        }
        assert!(
            serde_json::from_value::<ClusterPolicy>(json!({"max_attempts": 2})).is_err(),
            "a partial policy is not a valid section"
        );
    }

    #[test]
    fn duplicate_or_zero_resource_limits_are_rejected() {
        let org = Uuid::new_v4();
        let limits = json!({"resources_per_key": 1, "resources_per_organization": 1,
            "vector_stores_per_key": 1, "vector_stores_per_organization": 1});
        let mut entry = limits.clone();
        entry["organization_id"] = json!(org);
        let duplicate: ResourceSection = serde_json::from_value(
            json!({"default": limits, "organizations": [entry.clone(), entry]}),
        )
        .unwrap();
        assert!(install(None, Some(duplicate), &required()).is_err());
        let mut zero = limits.clone();
        zero["vector_stores_per_key"] = json!(0);
        let section: ResourceSection =
            serde_json::from_value(json!({"default": zero, "organizations": []})).unwrap();
        assert!(install(None, Some(section), &required()).is_err());
    }
}
