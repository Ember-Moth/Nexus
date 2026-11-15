//! Rewrite only provider response identifiers; never inspect or persist response content.
use super::database::Gateway;
use serde_json::Value;
use uuid::Uuid;

pub struct StoredStream {
    db: Gateway,
    request: Uuid,
    attempt: Uuid,
    handle: Uuid,
    provider: Option<String>,
    background: bool,
    registration_started: bool,
}
impl StoredStream {
    pub fn new(db: Gateway, request: Uuid, attempt: Uuid) -> Self {
        Self {
            db,
            request,
            attempt,
            handle: Uuid::new_v4(),
            provider: None,
            background: false,
            registration_started: false,
        }
    }
    pub fn public_id(&self) -> String {
        format!("resp_nx_{}", self.handle)
    }
    pub(crate) fn background(mut self, enabled: bool) -> Self {
        self.background = enabled;
        self
    }

    pub(crate) fn awaiting_registration(&self) -> bool {
        self.background && self.provider.is_none()
    }
    pub(crate) fn submitted_job(&self) -> Option<Uuid> {
        (self.background && self.registration_started).then_some(self.handle)
    }
    pub async fn rewrite(&mut self, event: &mut Value) -> Result<(), ()> {
        if let Some(id) = event["response"]["id"].as_str() {
            if self.provider.as_deref().is_some_and(|p| p != id) {
                return Err(());
            }
            if self.provider.is_none() {
                if self.background {
                    // Once the register call starts its commit outcome can be unknown.
                    // The single durable job owns final reporting in that case.
                    self.registration_started = true;
                    if let Err(e) = self
                        .db
                        .register_background(self.handle, self.request, self.attempt, id)
                        .await
                    {
                        if !e.retryable() {
                            self.registration_started = false;
                        }
                        return Err(());
                    }
                } else {
                    self.db
                        .save_response(self.handle, self.request, self.attempt, id)
                        .await
                        .map_err(|_| ())?;
                }
                self.provider = Some(id.into());
            }
            event["response"]["id"] = self.public_id().into();
        }
        if let Some(id) = event.get("response_id").and_then(Value::as_str) {
            if Some(id) != self.provider.as_deref() {
                return Err(());
            }
            event["response_id"] = self.public_id().into();
        }
        Ok(())
    }
}

/// Resolve the parent through the live key contract. For item references, require the item
/// to appear in the authenticated parent's output before spending any generation quota.
pub(crate) async fn bind_parent(
    node: &crate::server::Node,
    index: &super::snapshot::Index,
    raw: &str,
    request: &mut crate::protocol::request::Generate,
    handle: Uuid,
) -> Result<(), crate::request::Failure> {
    use crate::{
        protocol::{
            Endpoint,
            request::{Generate, Operation},
        },
        request::{Failure, lifecycle::credential},
        upstream::{Upstream, read_body},
    };
    let key = index.authenticate(raw)?;
    let record = node
        .db
        .resolve_response(handle, key.id, index.revision)
        .await?;
    let refs = Generate::references(request.body())
        .map_err(|_| Failure(http::StatusCode::BAD_REQUEST, "invalid_item_reference"))?;
    if !refs.is_empty() {
        let dep = index
            .deployment(record.deployment)
            .ok_or_else(Failure::unavailable)?;
        let secret = credential(node, index, dep).await?;
        let mut url = Upstream::endpoint(
            &dep.base_url,
            Endpoint::OpenaiResponses,
            Operation::Generate,
        );
        url.path_segments_mut()
            .map_err(|_| Failure::unavailable())?
            .push(&record.provider_id);
        let reply = node
            .upstream
            .client
            .get(url)
            .bearer_auth(secret.as_str())
            .timeout(std::time::Duration::from_secs(20))
            .send()
            .await
            .map_err(|_| Failure::upstream())?;
        if !reply.status().is_success() {
            return Err(Failure::upstream());
        }
        let bytes = read_body(reply, node.upstream.response_limit())
            .await
            .map_err(|_| Failure::upstream())?;
        let body: Value = serde_json::from_slice(&bytes).map_err(|_| Failure::upstream())?;
        if body["id"] != record.provider_id || body["object"] != "response" {
            return Err(Failure::upstream());
        }
        let output = body["output"].as_array().ok_or_else(Failure::upstream)?;
        let items: std::collections::HashMap<_, _> = output
            .iter()
            .filter_map(|o| Some((o["id"].as_str()?, o["type"].as_str()?)))
            .collect();
        if refs.iter().any(|(id, kind)| {
            items
                .get(id)
                .is_none_or(|actual| kind.is_some_and(|expected| expected != *actual))
        }) {
            return Err(Failure(http::StatusCode::NOT_FOUND, "item_not_found"));
        }
    }
    request.bind_previous(record).map_err(|_| {
        Failure(
            http::StatusCode::BAD_REQUEST,
            "resource_deployment_mismatch",
        )
    })
}
