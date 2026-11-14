use std::collections::{HashMap, HashSet};

use chrono::{DateTime, Utc};
use rand::Rng;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use super::{ControlError, credential::CredentialRef, database::Snapshot};
use crate::protocol::{Endpoint, request::Generate, tokens::ReservationBounds};

pub const CAPABILITIES: &[&str] = &[
    "reservation.v1",
    "usage-events.v1",
    "relay.native.v1",
    "relay.bedrock-converse.v1",
    "relay.sse.v1",
    "relay.responses-ws.v1",
    "relay.messages-to-responses.v1",
    "relay.responses-to-messages.v1",
];

#[derive(Deserialize)]
pub struct Key {
    pub id: Uuid,
    pub organization_id: Uuid,
    pub project_id: Uuid,
    digest: String,
    expires_at: Option<DateTime<Utc>>,
}

#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Scope {
    Organization,
    Project,
    Key,
}

impl Key {
    pub fn scope(&self, scope: Scope) -> Uuid {
        match scope {
            Scope::Organization => self.organization_id,
            Scope::Project => self.project_id,
            Scope::Key => self.id,
        }
    }
}

#[derive(Deserialize)]
struct Access {
    organization_id: Uuid,
    layer: Layer,
    scope_type: Scope,
    scope_id: Uuid,
    allowed_models: HashSet<Uuid>,
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Layer {
    Platform,
    Customer,
}

#[derive(Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Metric {
    Rpm,
    Tpm,
    Concurrency,
}

impl Metric {
    pub fn name(self) -> &'static str {
        match self {
            Self::Rpm => "rpm",
            Self::Tpm => "tpm",
            Self::Concurrency => "concurrency",
        }
    }
}

#[derive(Deserialize, Serialize)]
pub struct Quota {
    pub id: Uuid,
    pub organization_id: Uuid,
    pub layer: Layer,
    pub scope_type: Scope,
    pub scope_id: Uuid,
    pub metric: Metric,
    pub limit_value: u32,
}
#[derive(Deserialize)]
struct Model {
    id: Uuid,
    public_name: String,
    max_input_tokens: u32,
    max_output_tokens: u32,
    capabilities: ModelCapabilities,
}
#[derive(Deserialize)]
struct ModelCapabilities {
    #[serde(default)]
    streaming: bool,
    tools: bool,
}

#[derive(Deserialize)]
struct Candidate {
    deployment_id: Uuid,
    priority: u32,
    weight: u32,
}
#[derive(Deserialize)]
struct Route {
    organization_id: Uuid,
    model_id: Uuid,
    candidates: Vec<Candidate>,
}

#[derive(Deserialize)]
pub struct Deployment {
    pub id: Uuid,
    model_id: Uuid,
    pub upstream_model: String,
    #[serde(default)]
    pub region: String,
    pub base_url: url::Url,
    pub credential_id: Uuid,
    pub credential_version: i32,
    #[serde(default)]
    pub upstream_version: i32,
    protocol_family: String,
    usage_format: Option<String>,
    relay_profile: Option<Profile>,
}

#[derive(Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum UpstreamProtocol {
    AnthropicMessages,
    OpenaiResponses,
    BedrockConverse,
}

impl UpstreamProtocol {
    // The Bedrock adapter translates frames to Messages before the existing
    // downstream response/SSE conversion; metering always uses the actual upstream.
    pub fn representation(self) -> Endpoint {
        match self {
            Self::AnthropicMessages | Self::BedrockConverse => Endpoint::AnthropicMessages,
            Self::OpenaiResponses => Endpoint::OpenaiResponses,
        }
    }
    pub fn usage_format(self) -> &'static str {
        match self {
            Self::BedrockConverse => crate::bedrock::FORMAT,
            _ => self.representation().usage_format(),
        }
    }
}
impl From<Endpoint> for UpstreamProtocol {
    fn from(ep: Endpoint) -> Self {
        match ep {
            Endpoint::AnthropicMessages => Self::AnthropicMessages,
            Endpoint::OpenaiResponses => Self::OpenaiResponses,
        }
    }
}

#[derive(Deserialize)]
struct Profile {
    convert_from: Option<Endpoint>,
    #[serde(default)]
    streaming: bool,
    #[serde(default)]
    continuation: bool,
    #[serde(default)]
    cancellation: bool,
    endpoint: UpstreamProtocol,
    transports: Vec<String>,
    tools: bool,
    input_ceiling: u32,
    output_ceiling: u32,
    count_rule: String,
    bound_reference: String,
}

#[derive(Deserialize)]
struct Resources {
    models: Vec<Model>,
    key_verifiers: Vec<Key>,
    deployments: Vec<Deployment>,
    access_policies: Vec<Access>,
    quota_policies: Vec<Quota>,
    routes: Vec<Route>,
    credential_refs: Vec<CredentialRef>,
}

pub struct Index {
    pub cluster: Uuid,
    pub node: Uuid,
    pub revision: i64,
    pub digest: String,
    keys: HashMap<String, Key>,
    models: HashMap<String, Model>,
    deployments: HashMap<Uuid, Deployment>,
    routes: HashMap<(Uuid, Uuid), Vec<Candidate>>,
    policies: HashMap<Uuid, Vec<Access>>,
    pub quotas: Vec<Quota>,
    pub credentials: HashMap<Uuid, CredentialRef>,
}

fn unique<K: Eq + std::hash::Hash, V>(
    map: &mut HashMap<K, V>,
    key: K,
    value: V,
) -> Result<(), ControlError> {
    if map.insert(key, value).is_some() {
        return Err(ControlError::Invalid("duplicate resource"));
    }
    Ok(())
}

#[derive(Debug, thiserror::Error)]
pub enum Rejection {
    #[error("invalid_api_key")]
    Key,
    #[error("model_forbidden")]
    Forbidden,
    #[error("unsupported_request")]
    Unsupported,
    #[error("deployment_unavailable")]
    Unavailable,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Binding {
    pub deployment: Uuid,
    pub model: Uuid,
    pub organization: Uuid,
    pub project: Uuid,
    pub key: Uuid,
    pub credential: Uuid,
    pub credential_version: i32,
    pub upstream_version: i32,
    pub upstream_model: String,
    pub base_url: url::Url,
}

pub struct Selection<'a> {
    pub upstream: UpstreamProtocol,
    pub deployment: &'a Deployment,
    pub model: Uuid,
    pub key: &'a Key,
    pub bounds: ReservationBounds,
    pub body: serde_json::Value,
}

impl Deployment {
    pub(crate) fn is_bedrock(&self) -> bool {
        self.relay_profile
            .as_ref()
            .is_some_and(|p| p.endpoint == UpstreamProtocol::BedrockConverse)
    }
}

impl Selection<'_> {
    pub fn binding(&self) -> Binding {
        let d = self.deployment;
        Binding {
            deployment: d.id,
            model: self.model,
            organization: self.key.organization_id,
            project: self.key.project_id,
            key: self.key.id,
            credential: d.credential_id,
            credential_version: d.credential_version,
            upstream_version: d.upstream_version,
            upstream_model: d.upstream_model.clone(),
            base_url: d.base_url.clone(),
        }
    }
    pub fn cancellation(&self) -> bool {
        self.deployment
            .relay_profile
            .as_ref()
            .is_some_and(|p| p.cancellation)
    }
}

impl Index {
    pub fn build(snapshot: &mut Snapshot) -> Result<Self, ControlError> {
        let r: Resources = serde_json::from_value(snapshot.resources.take())
            .map_err(|_| ControlError::Invalid("resource schema"))?;
        let mut index = Self {
            cluster: snapshot.cluster_id,
            node: snapshot.node_id,
            revision: snapshot.revision,
            digest: snapshot.digest.clone(),
            keys: HashMap::new(),
            models: HashMap::new(),
            deployments: HashMap::new(),
            routes: HashMap::new(),
            policies: HashMap::new(),
            quotas: r.quota_policies,
            credentials: HashMap::new(),
        };
        let mut models = HashMap::new();
        for model in &r.models {
            unique(
                &mut models,
                model.id,
                (model.max_input_tokens, model.max_output_tokens),
            )?;
        }
        for m in r.models {
            unique(&mut index.models, m.public_name.clone(), m)?;
        }
        for key in r.key_verifiers {
            if key.digest.len() != 64 || !key.digest.bytes().all(|b| b.is_ascii_hexdigit()) {
                return Err(ControlError::Invalid("key digest"));
            }
            unique(&mut index.keys, key.digest.clone(), key)?;
        }
        for c in r.credential_refs {
            if c.algorithm != "libsodium-sealed-box"
                || c.key_version <= 0
                || c.credential_version <= 0
            {
                return Err(ControlError::Invalid("credential reference"));
            }
            unique(&mut index.credentials, c.credential_id, c)?;
        }
        for d in r.deployments {
            let (inputs, outputs) = models
                .get(&d.model_id)
                .ok_or(ControlError::Invalid("deployment model"))?;
            if !index
                .credentials
                .get(&d.credential_id)
                .is_some_and(|c| c.credential_version == d.credential_version)
                || d.base_url.scheme() != "https"
                || d.base_url.host().is_none()
                || !d.base_url.username().is_empty()
                || d.base_url.password().is_some()
                || d.base_url.query().is_some()
                || d.base_url.fragment().is_some()
            {
                return Err(ControlError::Invalid("deployment binding"));
            }
            if let Some(p) = &d.relay_profile {
                let family = match p.endpoint {
                    UpstreamProtocol::BedrockConverse => "aws_bedrock",
                    UpstreamProtocol::AnthropicMessages => "anthropic",
                    UpstreamProtocol::OpenaiResponses => "openai_compatible",
                };
                if p.endpoint == UpstreamProtocol::BedrockConverse
                    && (p.convert_from.is_some()
                        || p.transports.as_slice() != ["http"]
                        || p.continuation
                        || p.cancellation
                        || p.count_rule != "deployment_ceiling.v1"
                        || d.region.is_empty()
                        || !snapshot
                            .required_capabilities
                            .iter()
                            .any(|c| c == "relay.bedrock-converse.v1"))
                {
                    return Err(ControlError::Invalid("Bedrock profile"));
                }
                if let Some(source) = p.convert_from {
                    let capability = match source {
                        Endpoint::AnthropicMessages => "relay.messages-to-responses.v1",
                        Endpoint::OpenaiResponses => "relay.responses-to-messages.v1",
                    };
                    if UpstreamProtocol::from(source) == p.endpoint
                        || !p.transports.iter().any(|t| t == "http")
                        || !snapshot
                            .required_capabilities
                            .iter()
                            .any(|c| c == capability)
                    {
                        return Err(ControlError::Invalid("conversion profile"));
                    }
                }
                if d.protocol_family != family
                    || d.usage_format.as_deref() != Some(p.endpoint.usage_format())
                    || !snapshot
                        .required_capabilities
                        .iter()
                        .any(|c| c == "relay.native.v1")
                    || p.transports.is_empty()
                    || p.transports.iter().any(|t| t != "http" && t != "websocket")
                    || (p.transports.iter().any(|t| t == "websocket")
                        && (p.endpoint != UpstreamProtocol::OpenaiResponses
                            || !snapshot
                                .required_capabilities
                                .iter()
                                .any(|c| c == "relay.responses-ws.v1")))
                    || p.bound_reference.trim().is_empty()
                    || !matches!(
                        p.count_rule.as_str(),
                        "cl100k_base.v1" | "o200k_base.v1" | "deployment_ceiling.v1"
                    )
                    || p.input_ceiling == 0
                    || p.output_ceiling == 0
                    || p.input_ceiling > *inputs
                    || p.output_ceiling > *outputs
                {
                    return Err(ControlError::Invalid("native profile"));
                }
            }
            unique(&mut index.deployments, d.id, d)?;
        }
        for r in r.routes {
            if r.candidates.is_empty()
                || r.candidates.iter().any(|c| {
                    c.weight == 0
                        || c.weight > 10000
                        || !index
                            .deployments
                            .get(&c.deployment_id)
                            .is_some_and(|d| d.model_id == r.model_id)
                })
            {
                return Err(ControlError::Invalid("route"));
            }
            unique(
                &mut index.routes,
                (r.organization_id, r.model_id),
                r.candidates,
            )?;
        }
        for p in r.access_policies {
            index.policies.entry(p.organization_id).or_default().push(p);
        }
        index.quotas.sort_by_key(|q| q.id);
        Ok(index)
    }

    pub fn authenticate(&self, secret: &str) -> Result<&Key, Rejection> {
        let digest = format!("{:x}", Sha256::digest(secret.as_bytes()));
        self.keys
            .get(&digest)
            .filter(|k| k.expires_at.is_none_or(|t| t > Utc::now()))
            .ok_or(Rejection::Key)
    }
    pub fn select(&self, secret: &str, request: &Generate) -> Result<Selection<'_>, Rejection> {
        self.route(secret, request, false, None)
    }
    pub fn route(
        &self,
        secret: &str,
        request: &Generate,
        ws: bool,
        pinned: Option<&Binding>,
    ) -> Result<Selection<'_>, Rejection> {
        self.choose(secret, request, ws, pinned, None)
    }
    /// Choose the next HTTP candidate after the `failed` deployments provably did not execute
    /// this request. The best remaining priority group is drawn by weight, so a group is left
    /// only once all of its eligible members have failed. A candidate must also fit the frozen
    /// reservation: its input ceiling cannot exceed it and the reserved output limit is sent.
    pub fn fallback(
        &self,
        secret: &str,
        request: &Generate,
        failed: &[Uuid],
        frozen: ReservationBounds,
    ) -> Option<Selection<'_>> {
        self.choose(secret, request, false, None, Some((failed, frozen)))
            .ok()
    }
    fn choose(
        &self,
        secret: &str,
        request: &Generate,
        ws: bool,
        pinned: Option<&Binding>,
        retry: Option<(&[Uuid], ReservationBounds)>,
    ) -> Result<Selection<'_>, Rejection> {
        let key = self.authenticate(secret)?;
        let m = self
            .models
            .get(&request.model)
            .ok_or(Rejection::Forbidden)?;
        let mut platform = false;
        for p in self
            .policies
            .get(&key.organization_id)
            .into_iter()
            .flatten()
            .filter(|p| p.scope_id == key.scope(p.scope_type))
        {
            if !p.allowed_models.contains(&m.id) {
                return Err(Rejection::Forbidden);
            }
            platform |= p.scope_type == Scope::Organization && p.layer == Layer::Platform;
        }
        if !platform {
            return Err(Rejection::Forbidden);
        }
        if (request.stream && !ws && !m.capabilities.streaming)
            || request.has_tools() && !m.capabilities.tools
        {
            return Err(Rejection::Unsupported);
        }
        let route = self
            .routes
            .get(&(key.organization_id, m.id))
            .ok_or(Rejection::Forbidden)?;
        let mut selected = None;
        let mut conversion = None;
        let mut bedrock_body = None;
        let mut incompatible = false;
        let mut best = u32::MAX;
        let mut weight = 0;
        for candidate in route {
            let dep = &self.deployments[&candidate.deployment_id];
            let Some(profile) = &dep.relay_profile else {
                continue;
            };
            let cross = profile.endpoint != UpstreamProtocol::BedrockConverse
                && profile.endpoint != UpstreamProtocol::from(request.endpoint);
            if (cross && (ws || profile.convert_from != Some(request.endpoint)))
                || !profile
                    .transports
                    .iter()
                    .any(|t| t == if ws { "websocket" } else { "http" })
                || (request.stream && !ws && !profile.streaming)
                || (request.previous_response_id().is_some() && !profile.continuation)
                || pinned.is_some_and(|p| p.deployment != dep.id)
                || (request.has_tools() && !profile.tools)
                || request
                    .requested_output
                    .is_some_and(|n| n > profile.output_ceiling)
                || retry.is_some_and(|(failed, frozen)| {
                    failed.contains(&dep.id)
                        || profile.input_ceiling > frozen.inputs
                        || profile.output_ceiling < frozen.outputs
                })
                || candidate.priority > best
            {
                continue;
            }
            if profile.endpoint == UpstreamProtocol::BedrockConverse
                && bedrock_body
                    .get_or_insert_with(|| crate::bedrock::prepare(request))
                    .is_err()
            {
                incompatible = true;
                continue;
            }
            if cross && conversion.get_or_insert_with(|| request.convert()).is_err() {
                incompatible = true;
                continue;
            }
            if candidate.priority < best {
                best = candidate.priority;
                weight = 0;
            }
            weight += u64::from(candidate.weight);
            if rand::thread_rng().gen_range(0..weight) < u64::from(candidate.weight) {
                selected = Some((dep, profile));
            }
        }
        let (dep, p) = selected.ok_or(if incompatible {
            Rejection::Unsupported
        } else {
            Rejection::Unavailable
        })?;
        // A later attempt sends exactly the output limit already reserved for this request.
        let requested = retry.map_or(request.requested_output, |(_, frozen)| Some(frozen.outputs));
        let (body, bounds) = if p.endpoint == UpstreamProtocol::BedrockConverse {
            let mut body = bedrock_body
                .and_then(Result::ok)
                .ok_or(Rejection::Unsupported)?;
            let bounds =
                ReservationBounds::for_deployment(p.input_ceiling, p.output_ceiling, requested)
                    .map_err(|_| Rejection::Unsupported)?;
            body["inferenceConfig"]["maxTokens"] = bounds.outputs.into();
            (body, bounds)
        } else if p.endpoint == UpstreamProtocol::from(request.endpoint) {
            request
                .prepare_bounded(
                    &dep.upstream_model,
                    p.input_ceiling,
                    p.output_ceiling,
                    requested,
                )
                .map_err(|_| Rejection::Unsupported)?
        } else {
            let mut body = conversion
                .and_then(Result::ok)
                .ok_or(Rejection::Unsupported)?;
            let bounds =
                ReservationBounds::for_deployment(p.input_ceiling, p.output_ceiling, requested)
                    .map_err(|_| Rejection::Unsupported)?;
            body["model"] = dep.upstream_model.clone().into();
            body[match p.endpoint.representation() {
                Endpoint::AnthropicMessages => "max_tokens",
                Endpoint::OpenaiResponses => "max_output_tokens",
            }] = bounds.outputs.into();
            (body, bounds)
        };
        let selection = Selection {
            upstream: p.endpoint,
            deployment: dep,
            key,
            model: m.id,
            body,
            bounds,
        };
        if pinned.is_some_and(|b| *b != selection.binding())
            || retry.is_some_and(|(_, frozen)| {
                selection.bounds.inputs > frozen.inputs || selection.bounds.outputs > frozen.outputs
            })
        {
            return Err(Rejection::Unavailable);
        }
        Ok(selection)
    }
}
