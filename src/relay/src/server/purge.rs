//! Delete upstream copies of tenant resources.
//!
//! The control plane schedules a purge once the owning key has been unusable (revoked,
//! expired, or its organization, project or service account inactive) for the grace
//! period, and for resources created upstream whose local binding could not be saved.
//! Any node of the cluster may run a leased purge; a provider 404 counts as deleted, so
//! retries are idempotent. Each outcome is recorded with an audit event.
use super::Node;
use crate::{
    control::{purges::Purge, snapshot::Index, vectors::Observation},
    protocol::{
        Endpoint,
        request::{AnthropicHeaders, Operation},
    },
    request::lifecycle::credential,
    upstream::Upstream,
};
use http::{HeaderMap, Method, StatusCode};
use std::{sync::Arc, time::Duration};
use tokio_util::sync::CancellationToken;
use tracing::{info, warn};
use uuid::Uuid;

const BATCH: i32 = 16;

pub(super) async fn supervise(node: Arc<Node>, stop: CancellationToken) {
    loop {
        let base = node.purge_interval.as_millis() as u64;
        let delay = Duration::from_millis(base + rand::random::<u64>() % (base / 4 + 1));
        tokio::select! {
            _ = stop.cancelled() => break,
            _ = tokio::time::sleep(delay) => {}
        }
        let Some(index) = node.current() else {
            continue;
        };
        // The grace period comes from the snapshot the node runs, like every cluster policy.
        let grace = index.policy.purge_grace_seconds();
        let jobs = match node.db.claim_purges(grace, BATCH).await {
            Ok(jobs) => jobs,
            Err(error) => {
                warn!(reason = error.reason(), "upstream purge claim failed");
                continue;
            }
        };
        for job in jobs {
            let result = purge(&node, &index, &job).await;
            match result {
                Ok(()) => {
                    info!(purge_id = %job.id, resource_kind = job.kind, "upstream copy deleted")
                }
                Err(code) => warn!(purge_id = %job.id, resource_kind = job.kind, reason = code,
                    "upstream copy not deleted"),
            }
            if let Err(error) = node.db.finish_purge(job.id, result.err()).await {
                warn!(purge_id = %job.id, reason = error.reason(), "upstream purge not recorded");
            }
        }
    }
}

async fn purge(node: &Node, index: &Index, job: &Purge) -> Result<(), &'static str> {
    let dep = index
        .deployment(job.deployment_id)
        .filter(|d| d.credential_id == job.credential_id)
        .ok_or("deployment_unavailable")?;
    let endpoint = match job.endpoint.as_str() {
        "openai_responses" => Endpoint::OpenaiResponses,
        "anthropic_messages" => Endpoint::AnthropicMessages,
        _ => return Err("invalid_endpoint"),
    };
    if job.kind == "container" && endpoint == Endpoint::AnthropicMessages {
        // Claude execution containers have no delete operation; the provider expires them.
        return Ok(());
    }
    delete(node, index, dep, &job.kind, endpoint, &job.provider_id).await?;
    if job.kind == "vector_store"
        && let Some(handle) = job.resource_id
    {
        // The deletion observation closes storage metering through the existing path.
        node.db
            .observe_vector(Observation {
                handle,
                key: Some(job.api_key_id),
                revision: index.revision,
                provider: Some(&job.provider_id),
                stamp: chrono::Utc::now(),
                metadata: None,
                state: "deleted",
                uncertain: false,
            })
            .await
            .map_err(|_| "observation_unavailable")?;
    }
    Ok(())
}

/// Delete one upstream resource. A missing resource counts as deleted.
pub(super) async fn delete(
    node: &Node,
    index: &Index,
    dep: &crate::control::snapshot::Deployment,
    kind: &str,
    endpoint: Endpoint,
    provider_id: &str,
) -> Result<(), &'static str> {
    let token = credential(node, index, dep)
        .await
        .map_err(|_| "credential_unavailable")?;
    let mut url = Upstream::endpoint(&dep.base_url, endpoint, Operation::Generate);
    {
        let mut path = url.path_segments_mut().map_err(|_| "invalid_endpoint")?;
        match (kind, endpoint) {
            ("response", Endpoint::OpenaiResponses) => {}
            ("batch", Endpoint::AnthropicMessages) => {
                path.push("batches");
            }
            ("file", _) => {
                path.pop().push("files");
            }
            ("conversation", Endpoint::OpenaiResponses) => {
                path.pop().push("conversations");
            }
            ("vector_store", Endpoint::OpenaiResponses) => {
                path.pop().push("vector_stores");
            }
            ("container", Endpoint::OpenaiResponses) => {
                path.pop().push("containers");
            }
            _ => return Err("invalid_kind"),
        }
        path.push(provider_id);
    }
    let send = |method: Method, url: url::Url| {
        let call = node
            .upstream
            .client
            .request(method, url)
            .timeout(Duration::from_secs(30));
        match endpoint {
            Endpoint::OpenaiResponses => call.bearer_auth(token.as_str()),
            Endpoint::AnthropicMessages => {
                let mut headers = HeaderMap::new();
                if kind == "file" {
                    headers.insert(
                        "anthropic-beta",
                        http::HeaderValue::from_static("files-api-2025-04-14"),
                    );
                }
                AnthropicHeaders::parse(&headers)
                    .unwrap_or_default()
                    .apply(call.header("x-api-key", token.as_str()))
            }
        }
    };
    let status = send(Method::DELETE, url.clone())
        .send()
        .await
        .map_err(|_| "upstream_unreachable")?
        .status();
    match status {
        s if s.is_success() || s == StatusCode::NOT_FOUND => Ok(()),
        StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN => Err("credential_rejected"),
        StatusCode::TOO_MANY_REQUESTS => Err("upstream_throttled"),
        s if s.is_server_error() => Err("upstream_unavailable"),
        _ if kind == "batch" => {
            // A batch in progress must end before it can be deleted.
            url.path_segments_mut()
                .map_err(|_| "invalid_endpoint")?
                .push("cancel");
            let _ = send(Method::POST, url).send().await;
            Err("batch_in_progress")
        }
        _ => Err("upstream_rejected"),
    }
}

/// Delete an upstream resource whose local binding could not be saved. If that fails too,
/// record it so the purge queue deletes it later; as a last resort, log its locator.
#[allow(clippy::too_many_arguments)]
pub(super) async fn orphaned(
    node: &Node,
    index: &Index,
    dep: &crate::control::snapshot::Deployment,
    key: Uuid,
    kind: &'static str,
    endpoint: Endpoint,
    provider_id: &str,
) {
    if delete(node, index, dep, kind, endpoint, provider_id)
        .await
        .is_ok()
    {
        return;
    }
    if let Err(error) = node
        .db
        .record_orphan(
            key,
            index.revision,
            kind,
            dep.id,
            endpoint.as_str(),
            provider_id,
        )
        .await
    {
        warn!(
            resource_kind = kind,
            deployment_id = %dep.id,
            provider_id,
            reason = error.reason(),
            "orphaned upstream resource not recorded"
        );
    }
}
