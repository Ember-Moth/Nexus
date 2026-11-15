use std::collections::{HashMap, HashSet};

use chrono::{DateTime, Utc};
use rand::Rng;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use super::{ControlError, credential::CredentialRef, database::Snapshot};
use crate::protocol::{
    Endpoint, cache,
    features::{Feature, Features},
    request::{Generate, Operation},
    tokens::{CallLoop, ReservationBounds},
};

pub const CAPABILITIES: &[&str] = &[
    "reservation.v1",
    "usage-events.v1",
    "relay.native.v1",
    "relay.prompt-cache.v1",
    "relay.web-search.v1",
    "relay.file-search.v1",
    "relay.claude-execution.v1",
    "relay.openai-containers.v1",
    "relay.bedrock-converse.v1",
    "relay.bedrock-claude-invoke.v1",
    "relay.bedrock-settings.v1",
    "relay.bedrock-guardrail-usage.v1",
    "relay.bedrock-request-options.v1",
    "relay.sse.v1",
    "relay.responses-ws.v1",
    "relay.messages-to-responses.v1",
    "relay.responses-to-messages.v1",
    "relay.responses-to-chat.v1",
    "relay.provider-features.v1",
    "relay.stored-responses.v1",
    "relay.background-responses.v1",
    "relay.conversations.v1",
    "relay.files.v1",
    "relay.vector-stores.v1",
    "relay.message-batches.v1",
    "relay.code-execution.v1",
    "relay.remote-mcp.v1",
    "relay.web-fetch.v1",
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
    #[serde(default)]
    token_dimensions: Vec<String>,
    #[serde(default)]
    tool_dimensions: Vec<String>,
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
    OpenaiChatCompletions,
}

impl UpstreamProtocol {
    // The Bedrock adapter translates frames to Messages before the existing
    // downstream response/SSE conversion; metering always uses the actual upstream.
    pub fn representation(self) -> Endpoint {
        match self {
            Self::AnthropicMessages | Self::BedrockConverse => Endpoint::AnthropicMessages,
            Self::OpenaiChatCompletions | Self::OpenaiResponses => Endpoint::OpenaiResponses,
        }
    }
    pub fn usage_format(self) -> &'static str {
        match self {
            Self::BedrockConverse => crate::bedrock::FORMAT,
            Self::OpenaiChatCompletions => crate::protocol::chat_upstream::FORMAT,
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
    /// Features that keep content in, or send it through, the upstream account. None by default.
    #[serde(default)]
    provider_features: Vec<Feature>,
    #[serde(default)]
    container_reserve_minutes: u32,
    container_tariff: Option<String>,
    #[serde(default)]
    code_execution: bool,
    #[serde(default)]
    execution_reserve_seconds: u32,
    #[serde(default)]
    file_search_ceiling: u32,
    #[serde(default)]
    web_search_ceiling: u32,
    #[serde(default)]
    web_search_variant: crate::protocol::hosted::SearchVariant,
    #[serde(default)]
    bedrock_invoke: bool,
    bedrock_guardrail: Option<serde_json::Value>,
    #[serde(default)]
    bedrock_guardrail_unit_ceiling: u32,
    #[serde(default)]
    bedrock_request_options: Vec<crate::bedrock::RequestOptions>,
    #[serde(default)]
    bedrock_model_fields: serde_json::Map<String, serde_json::Value>,
    #[serde(default)]
    auto_prompt_cache: bool,
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

impl Profile {
    fn features(&self) -> Features {
        self.provider_features.iter().copied().collect()
    }
    fn permits_bedrock_options(&self, options: &crate::bedrock::RequestOptions) -> bool {
        // The caller may request the configured defaults or one explicitly published
        // alternative. Combining fragments of independently approved options is ambiguous.
        (options
            .guardrail_config
            .as_ref()
            .is_none_or(|g| Some(g) == self.bedrock_guardrail.as_ref())
            && options
                .additional_model_request_fields
                .iter()
                .all(|(k, v)| self.bedrock_model_fields.get(k) == Some(v)))
            || self.bedrock_request_options.contains(options)
    }

    fn model_fields_match(
        &self,
        options: Option<&crate::bedrock::RequestOptions>,
        body: &serde_json::Value,
    ) -> bool {
        body["additionalModelRequestFields"]
            .as_object()
            .is_none_or(|fields| {
                fields.iter().all(|(k, v)| {
                    options
                        .and_then(|opts| opts.additional_model_request_fields.get(k))
                        .or_else(|| self.bedrock_model_fields.get(k))
                        .is_none_or(|published| published == v)
                })
            })
    }
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
    #[error("feature_disabled")]
    FeatureDisabled,
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
    pub(crate) approximation: crate::protocol::approximation::Delivery,
    pub(crate) private_history: bool,
    pub(crate) operation: Operation,
    pub upstream: UpstreamProtocol,
    pub deployment: &'a Deployment,
    pub model: Uuid,
    pub key: &'a Key,
    pub bounds: ReservationBounds,
    pub body: serde_json::Value,
}

impl Deployment {
    pub(crate) fn protocol_family(&self) -> &str {
        &self.protocol_family
    }
    pub(crate) fn invoke(&self) -> bool {
        self.relay_profile
            .as_ref()
            .is_some_and(|p| p.bedrock_invoke)
    }
    pub(crate) fn is_chat(&self) -> bool {
        self.relay_profile
            .as_ref()
            .is_some_and(|p| p.endpoint == UpstreamProtocol::OpenaiChatCompletions)
    }
    pub(crate) fn is_bedrock(&self) -> bool {
        self.relay_profile
            .as_ref()
            .is_some_and(|p| p.endpoint == UpstreamProtocol::BedrockConverse)
    }
    /// Whether the operator enabled a provider feature for this deployment.
    pub(crate) fn allows(&self, feature: Feature) -> bool {
        self.relay_profile
            .as_ref()
            .is_some_and(|p| p.features().contains(feature))
    }
}

impl Selection<'_> {
    pub fn usage_format(&self) -> &'static str {
        if self.deployment.invoke() {
            Endpoint::AnthropicMessages.usage_format()
        } else {
            self.upstream.usage_format()
        }
    }
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
                (
                    model.max_input_tokens,
                    model.max_output_tokens,
                    model.token_dimensions.clone(),
                    model.tool_dimensions.clone(),
                ),
            )?;
        }
        for m in r.models {
            for dim in &m.tool_dimensions {
                let cap = match dim.as_str() {
                    "container_1g_minutes"
                    | "container_4g_minutes"
                    | "container_16g_minutes"
                    | "container_64g_minutes" => "relay.openai-containers.v1",
                    "code_execution_seconds" => "relay.claude-execution.v1",
                    dim if crate::bedrock::guardrail::PRICED.contains(&dim) => {
                        "relay.bedrock-guardrail-usage.v1"
                    }
                    "web_search_requests" => "relay.web-search.v1",
                    "file_search_calls" | "vector_storage_byte_seconds" => "relay.file-search.v1",
                    _ => return Err(ControlError::Invalid("unknown tool dimension")),
                };
                if !snapshot.required_capabilities.iter().any(|s| s == cap) {
                    return Err(ControlError::Invalid("tool pricing capability"));
                }
            }
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
            let (inputs, outputs, dimensions, tools_priced) = models
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
                let guarded = p.bedrock_guardrail.is_some()
                    || p.bedrock_request_options
                        .iter()
                        .any(|o| o.guardrail_config.is_some());
                // Every assessment is metered, so a guardrail without a unit bound cannot settle.
                if guarded && p.bedrock_guardrail_unit_ceiling == 0 {
                    return Err(ControlError::Invalid("Guardrail unit bounds"));
                }
                if p.bedrock_guardrail_unit_ceiling > 0
                    && (p.endpoint != UpstreamProtocol::BedrockConverse
                        || p.bedrock_invoke
                        || p.bedrock_guardrail_unit_ceiling > 1_000_000_000
                        || p.bedrock_guardrail.is_none()
                            && !p
                                .bedrock_request_options
                                .iter()
                                .any(|o| o.guardrail_config.is_some())
                        || !tools_priced
                            .iter()
                            .any(|d| crate::bedrock::guardrail::PRICED.contains(&d.as_str())))
                {
                    return Err(ControlError::Invalid("Guardrail unit bounds"));
                }
                if p.container_reserve_minutes > 0
                    && (!p.tools
                        || p.endpoint != UpstreamProtocol::OpenaiResponses
                        || p.container_reserve_minutes > 1_000_000_000
                        || !tools_priced.iter().any(|d| d.starts_with("container_"))
                        || match p.container_tariff.as_deref().unwrap_or("session_20m") {
                            "session_20m" => p.container_reserve_minutes < 20,
                            "minute_min_5" => p.container_reserve_minutes < 5,
                            _ => true,
                        })
                {
                    return Err(ControlError::Invalid("container pricing"));
                }
                if p.code_execution
                    && (!p.tools
                        || p.endpoint != UpstreamProtocol::AnthropicMessages
                        || !snapshot
                            .required_capabilities
                            .iter()
                            .any(|c| c == "relay.claude-execution.v1"))
                    || p.execution_reserve_seconds > 0
                        && (!p.code_execution
                            || !(300..=1_000_000_000).contains(&p.execution_reserve_seconds)
                            || !tools_priced.iter().any(|d| d == "code_execution_seconds"))
                {
                    return Err(ControlError::Invalid("code execution configuration"));
                }
                if p.file_search_ceiling > 0
                    && (p.file_search_ceiling > 1_000_000_000
                        || p.endpoint != UpstreamProtocol::OpenaiResponses
                        || !p.tools
                        || ["file_search_calls", "vector_storage_byte_seconds"]
                            .iter()
                            .any(|k| !tools_priced.iter().any(|v| v == k)))
                {
                    return Err(ControlError::Invalid("file search deployment"));
                }
                if p.web_search_variant == crate::protocol::hosted::SearchVariant::Preview
                    && (p.web_search_ceiling == 0
                        || p.endpoint != UpstreamProtocol::OpenaiResponses)
                {
                    return Err(ControlError::Invalid("web search variant"));
                }
                if p.web_search_ceiling > 0
                    && (!p.tools
                        || p.web_search_ceiling > 1_000_000_000
                        || d.is_bedrock()
                        || !tools_priced.iter().any(|d| d == "web_search_requests"))
                {
                    return Err(ControlError::Invalid("web search deployment"));
                }
                if (p.bedrock_guardrail.is_some() || !p.bedrock_model_fields.is_empty())
                    && (p.endpoint != UpstreamProtocol::BedrockConverse
                        || p.bedrock_invoke
                        || !snapshot
                            .required_capabilities
                            .iter()
                            .any(|v| v == "relay.bedrock-settings.v1"))
                {
                    return Err(ControlError::Invalid("Bedrock Converse settings"));
                }
                if !p.bedrock_request_options.is_empty()
                    && (p.endpoint != UpstreamProtocol::BedrockConverse
                        || p.bedrock_invoke
                        || !snapshot
                            .required_capabilities
                            .iter()
                            .any(|v| v == "relay.bedrock-request-options.v1"))
                {
                    return Err(ControlError::Invalid("Bedrock request options"));
                }
                if p.bedrock_invoke
                    && (p.endpoint != UpstreamProtocol::BedrockConverse
                        || !snapshot
                            .required_capabilities
                            .iter()
                            .any(|s| s == "relay.bedrock-claude-invoke.v1"))
                {
                    return Err(ControlError::Invalid("Bedrock Invoke profile"));
                }
                if p.auto_prompt_cache {
                    let write_dim = if p.endpoint == UpstreamProtocol::OpenaiResponses {
                        "input_cache_write_30m_tokens"
                    } else {
                        "input_cache_write_5m_tokens"
                    };
                    if !snapshot
                        .required_capabilities
                        .iter()
                        .any(|c| c == "relay.prompt-cache.v1")
                        || ![write_dim, "input_cache_read_tokens"]
                            .iter()
                            .all(|d| dimensions.iter().any(|v| v == d))
                    {
                        return Err(ControlError::Invalid("prompt cache configuration"));
                    }
                }
                let features = p.features();
                if features.iter().count() != p.provider_features.len()
                    || features.contains(Feature::Background)
                        && !features.contains(Feature::StoredResponses)
                    || features.iter().any(|f| {
                        let endpoint = match f {
                            Feature::StoredResponses
                            | Feature::Background
                            | Feature::Conversations
                            | Feature::VectorStores => {
                                p.endpoint == UpstreamProtocol::OpenaiResponses
                            }
                            Feature::Batches | Feature::WebFetch => {
                                p.endpoint == UpstreamProtocol::AnthropicMessages
                            }
                            Feature::Files
                            | Feature::CodeExecution
                            | Feature::RemoteMcp
                            | Feature::WebSearch => matches!(
                                p.endpoint,
                                UpstreamProtocol::OpenaiResponses
                                    | UpstreamProtocol::AnthropicMessages
                            ),
                        };
                        !endpoint
                            || !snapshot
                                .required_capabilities
                                .iter()
                                .any(|c| c == f.capability())
                    })
                {
                    return Err(ControlError::Invalid("provider features"));
                }
                let family = match p.endpoint {
                    UpstreamProtocol::BedrockConverse => "aws_bedrock",
                    UpstreamProtocol::AnthropicMessages => "anthropic",
                    UpstreamProtocol::OpenaiResponses | UpstreamProtocol::OpenaiChatCompletions => {
                        "openai_compatible"
                    }
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
                if p.endpoint == UpstreamProtocol::OpenaiChatCompletions {
                    if p.transports.as_slice() != ["http"]
                        || p.continuation
                        || p.cancellation
                        || p.convert_from
                            .is_some_and(|e| e != Endpoint::OpenaiResponses)
                        || p.auto_prompt_cache
                        || p.web_search_ceiling > 0
                        || !snapshot
                            .required_capabilities
                            .iter()
                            .any(|c| c == "relay.responses-to-chat.v1")
                    {
                        return Err(ControlError::Invalid("Chat Completions profile"));
                    }
                } else if let Some(source) = p.convert_from {
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
                    || d.usage_format.as_deref()
                        != Some(if p.bedrock_invoke {
                            Endpoint::AnthropicMessages.usage_format()
                        } else {
                            p.endpoint.usage_format()
                        })
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

    pub fn model_names(&self) -> Vec<&str> {
        let mut names = self.models.keys().map(String::as_str).collect::<Vec<_>>();
        names.sort_unstable();
        names
    }
    pub(crate) fn history_source(&self, alias: &str, model: Uuid, dep: Uuid) -> bool {
        self.models.get(alias).is_some_and(|m| m.id == model)
            && self
                .deployments
                .get(&dep)
                .is_some_and(|d| d.model_id == model)
    }
    pub fn deployment(&self, id: Uuid) -> Option<&Deployment> {
        self.deployments.get(&id)
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
    pub(crate) fn container_target(
        &self,
        raw: &str,
        request: &Generate,
        tier: crate::protocol::execution::MemoryTier,
    ) -> Result<Selection<'_>, Rejection> {
        let priced = self
            .models
            .get(&request.model)
            .is_some_and(|m| m.tool_dimensions.iter().any(|d| d == tier.dimension()));
        if !priced {
            return Err(Rejection::Unsupported);
        }
        self.choose(raw, request, false, None, None, |_, p| {
            p.container_reserve_minutes > 0 && p.endpoint == UpstreamProtocol::OpenaiResponses
        })
    }

    pub(crate) fn vector_target(
        &self,
        raw: &str,
        request: &Generate,
    ) -> Result<Selection<'_>, Rejection> {
        self.choose(raw, request, false, None, None, |_, profile| {
            profile.file_search_ceiling > 0 && profile.endpoint == UpstreamProtocol::OpenaiResponses
        })
    }
    pub(crate) fn native_messages(
        &self,
        secret: &str,
        request: &Generate,
        account: Option<&Deployment>,
    ) -> Result<Selection<'_>, Rejection> {
        let mut s = self.choose(secret, request, false, None, None, |dep, profile| {
            profile.endpoint == UpstreamProtocol::AnthropicMessages
                && account.is_none_or(|owner| {
                    owner.credential_id == dep.credential_id
                        && owner.credential_version == dep.credential_version
                        && owner.base_url == dep.base_url
                        && owner.upstream_version == dep.upstream_version
                })
        })?;
        // Batches have no documented server loop limit: only declared tool caps bound them.
        if request.operation == Operation::Generate {
            use crate::protocol::tokens::model_calls;
            let interactive = CallLoop::Messages { batch: false };
            let batch = CallLoop::Messages { batch: true };
            let single = model_calls(interactive, false, &mut s.body);
            let calls = model_calls(batch, false, &mut s.body);
            let (Ok(single), Ok(calls)) = (single, calls) else {
                return Err(Rejection::Unsupported);
            };
            s.bounds.inputs /= single;
            s.bounds
                .cover_model_calls(calls)
                .map_err(|_| Rejection::Unsupported)?;
        }
        Ok(s)
    }
    pub fn route(
        &self,
        secret: &str,
        request: &Generate,
        ws: bool,
        pinned: Option<&Binding>,
    ) -> Result<Selection<'_>, Rejection> {
        self.choose(secret, request, ws, pinned, None, |_, _| true)
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
        self.choose(
            secret,
            request,
            false,
            None,
            Some((failed, frozen)),
            |_, _| true,
        )
        .ok()
    }
    fn choose(
        &self,
        secret: &str,
        request: &Generate,
        ws: bool,
        pinned: Option<&Binding>,
        retry: Option<(&[Uuid], ReservationBounds)>,
        eligible: impl Fn(&Deployment, &Profile) -> bool,
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
        let mut chat_body = None;
        let mut conversion = None;
        let mut bedrock_body = None;
        let mut incompatible = false;
        let mut disabled = false;
        let mut best = (true, u32::MAX);
        let cache_enabled = route.iter().any(|c| {
            self.deployments[&c.deployment_id]
                .relay_profile
                .as_ref()
                .is_some_and(|p| p.auto_prompt_cache)
        });
        let keyed = request.affinity_key().is_some()
            || (request.endpoint == Endpoint::OpenaiResponses
                && request.body().get("prompt_cache_key").is_some());
        let affinity = if cache_enabled || keyed {
            cache::affinity(
                key.organization_id,
                m.id,
                request.endpoint,
                request.body(),
                request.affinity_key(),
                cache_enabled,
            )
        } else {
            None
        };
        let document_citations = request.needs_document_citations();
        let mut best_score = f64::INFINITY;
        let mut cache_conversion = None;
        let mut cache_bedrock = None;
        let mut invoke_body = None;
        let mut cached_invoke = None;
        let mut weight = 0;
        for candidate in route {
            let dep = &self.deployments[&candidate.deployment_id];
            let Some(profile) = &dep.relay_profile else {
                continue;
            };
            if let Some((_, frozen)) = retry {
                let guarded = profile.bedrock_guardrail.is_some()
                    || request
                        .bedrock
                        .as_ref()
                        .is_some_and(|b| b.guardrail_config.is_some());
                let cap = if guarded {
                    profile.bedrock_guardrail_unit_ceiling
                } else {
                    0
                };
                // Converse has no wire setting to clamp policy units on a backup.
                if (cap == 0) != (frozen.guardrail_units == 0) || cap > frozen.guardrail_units {
                    continue;
                }
            }
            if let Some(tool) = &request.interpreter
                && (profile.endpoint != UpstreamProtocol::OpenaiResponses
                    || profile.container_reserve_minutes == 0
                    || !m
                        .tool_dimensions
                        .iter()
                        .any(|d| d == tool.target.tier.dimension()))
            {
                continue;
            }
            if let Some(exec) = &request.execution
                && (profile.endpoint != UpstreamProtocol::AnthropicMessages
                    || !profile.code_execution && (!exec.implicit || request.search.is_none())
                    || exec.mode != crate::protocol::execution::ExecutionMode::Bundled
                        && profile.execution_reserve_seconds == 0
                    || retry.is_some_and(|(_, b)| {
                        b.execution_seconds > profile.execution_reserve_seconds
                    }))
            {
                continue;
            }
            if let Some(file) = &request.file_search
                && (profile.endpoint != UpstreamProtocol::OpenaiResponses
                    || profile.file_search_ceiling == 0
                    || retry
                        .map(|(_, b)| b.file_searches)
                        .or(file.requested_limit)
                        .is_some_and(|n| n > profile.file_search_ceiling))
            {
                continue;
            }
            if let Some(search) = &request.search
                && (profile.endpoint != UpstreamProtocol::from(request.endpoint)
                    || profile.web_search_variant != search.variant
                    || profile.web_search_ceiling == 0
                    || retry
                        .map(|(_, b)| b.searches)
                        .or(search.requested_limit)
                        .is_some_and(|n| n > profile.web_search_ceiling))
            {
                continue;
            }
            if !eligible(dep, profile) {
                continue;
            }
            if request.chat_stop.is_some()
                && profile.endpoint == UpstreamProtocol::OpenaiResponses
                && !request.allow_approximation.stop
            {
                incompatible = true;
                continue;
            }
            if let Some(options) = &request.bedrock
                && (profile.endpoint != UpstreamProtocol::BedrockConverse
                    || profile.bedrock_invoke
                    || !profile.permits_bedrock_options(options))
            {
                incompatible = true;
                continue;
            }
            if crate::protocol::hosted::native(request.body())
                && profile.endpoint != UpstreamProtocol::from(request.endpoint)
            {
                continue;
            }
            if request.endpoint == Endpoint::AnthropicMessages
                && profile.endpoint == UpstreamProtocol::OpenaiResponses
                && request.body().get("context_management").is_some()
                && !request.anthropic.threshold()
            {
                incompatible = true;
                continue;
            }
            if (request.operation == Operation::CountTokens
                && profile.endpoint == UpstreamProtocol::OpenaiResponses)
                || (request.anthropic.requires_native()
                    && profile.endpoint != UpstreamProtocol::AnthropicMessages
                    && !(profile.endpoint == UpstreamProtocol::OpenaiResponses
                        && request.portable_headers()))
            {
                continue;
            }
            if document_citations && profile.endpoint == UpstreamProtocol::OpenaiResponses {
                incompatible = true;
                continue;
            }
            if (request.needs_stored_upstream() || request.needs_responses_tools())
                && profile.endpoint != UpstreamProtocol::OpenaiResponses
                || request
                    .stored_binding
                    .is_some_and(|(model, dep_id)| m.id != model || dep_id != dep.id)
            {
                continue;
            }
            if request.operation == Operation::Compact
                && profile.endpoint != UpstreamProtocol::OpenaiResponses
            {
                continue;
            }
            if !profile.features().covers(request.features) {
                disabled = true;
                continue;
            }
            if !request.restored.is_empty() && profile.endpoint.representation() == request.endpoint
            {
                incompatible = true;
                continue;
            }
            if dep.is_chat()
                && (ws
                    || request.endpoint != Endpoint::OpenaiResponses
                    || request.operation != Operation::Generate)
            {
                continue;
            }
            let cross = !dep.is_chat()
                && profile.endpoint != UpstreamProtocol::BedrockConverse
                && profile.endpoint != UpstreamProtocol::from(request.endpoint);
            if (cross && (ws || profile.convert_from != Some(request.endpoint)))
                || !profile
                    .transports
                    .iter()
                    .any(|t| t == if ws { "websocket" } else { "http" })
                || (request.stream && !ws && !profile.streaming)
                || (ws && request.previous_response_id().is_some() && !profile.continuation)
                || pinned.is_some_and(|p| p.deployment != dep.id)
                || (request.has_tools() && !profile.tools)
                || request
                    .requested_output
                    .is_some_and(|n| n > profile.output_ceiling)
                || retry.is_some_and(|(failed, frozen)| {
                    let output_fits = match request.operation {
                        Operation::Generate => profile.output_ceiling >= frozen.outputs,
                        // Compact has no output-limit parameter; the entire backup bound must fit.
                        Operation::Compact | Operation::CountTokens => {
                            profile.output_ceiling <= frozen.outputs
                        }
                    };
                    failed.contains(&dep.id)
                        || profile.input_ceiling > frozen.inputs
                        || !output_fits
                })
            {
                continue;
            }
            if profile.auto_prompt_cache
                && request.endpoint == Endpoint::OpenaiResponses
                && request
                    .body()
                    .get("prompt_cache_key")
                    .is_some_and(|v| v.as_str().is_none_or(|s| !cache::valid_key(s)))
            {
                incompatible = true;
                continue;
            }
            let prepared = if dep.is_chat() {
                Some(chat_body.get_or_insert_with(|| {
                    crate::protocol::chat_upstream::request(request.body())
                        .map(|body| (body, Default::default()))
                }))
            } else if profile.endpoint == UpstreamProtocol::BedrockConverse {
                let slot = match (profile.bedrock_invoke, profile.auto_prompt_cache) {
                    (true, true) => &mut cached_invoke,
                    (true, false) => &mut invoke_body,
                    (false, true) => &mut cache_bedrock,
                    (false, false) => &mut bedrock_body,
                };
                Some(slot.get_or_insert_with(|| {
                    crate::bedrock::prepare_options(
                        request,
                        profile.auto_prompt_cache,
                        profile.bedrock_invoke,
                    )
                }))
            } else if cross {
                let slot = if profile.auto_prompt_cache {
                    &mut cache_conversion
                } else {
                    &mut conversion
                };
                Some(slot.get_or_insert_with(|| {
                    crate::protocol::convert::request::with_options(
                        request.endpoint,
                        request.body(),
                        profile.auto_prompt_cache,
                        false,
                        &request.restored,
                        request.allow_approximation,
                    )
                }))
            } else {
                None
            };
            let mut applied = match prepared {
                Some(Err(_)) => {
                    incompatible = true;
                    continue;
                }
                Some(Ok((body, p))) => {
                    if !p.priced(&m.token_dimensions)
                        || profile.endpoint == UpstreamProtocol::BedrockConverse
                            && !profile.bedrock_invoke
                            && !profile.model_fields_match(request.bedrock.as_ref(), body)
                    {
                        incompatible = true;
                        continue;
                    }
                    *p
                }
                None => Default::default(),
            };
            if request.chat_stop.is_some() && profile.endpoint == UpstreamProtocol::OpenaiResponses
            {
                applied.stop = true;
            }
            let rank = (applied.active(), candidate.priority);
            if rank > best {
                continue;
            }
            if rank < best {
                best = rank;
                weight = 0;
                best_score = f64::INFINITY;
            }
            let take = if let Some(seed) = affinity {
                let score = cache::score(&seed, dep.id, candidate.weight);
                if score < best_score {
                    best_score = score;
                    true
                } else {
                    false
                }
            } else {
                weight += u64::from(candidate.weight);
                rand::thread_rng().gen_range(0..weight) < u64::from(candidate.weight)
            };
            if take {
                selected = Some((dep, profile, applied));
            }
        }
        let (dep, p, applied) = selected.ok_or(if disabled {
            Rejection::FeatureDisabled
        } else if incompatible {
            Rejection::Unsupported
        } else {
            Rejection::Unavailable
        })?;
        // Generation can enforce the frozen limit on the wire; compaction uses deployment bounds.
        let requested = match (request.operation, retry) {
            (Operation::Generate, Some((_, frozen))) => Some(frozen.outputs),
            _ => request.requested_output,
        };
        let (mut body, mut bounds) = if dep.is_chat() {
            let (mut body, _) = chat_body
                .and_then(Result::ok)
                .ok_or(Rejection::Unsupported)?;
            let bounds =
                ReservationBounds::for_deployment(p.input_ceiling, p.output_ceiling, requested)
                    .map_err(|_| Rejection::Unsupported)?;
            body["model"] = dep.upstream_model.clone().into();
            body["max_completion_tokens"] = bounds.outputs.into();
            (body, bounds)
        } else if p.bedrock_invoke {
            let mut body = (if p.auto_prompt_cache {
                cached_invoke
            } else {
                invoke_body
            })
            .and_then(Result::ok)
            .map(|(body, _)| body)
            .ok_or(Rejection::Unsupported)?;
            let bounds =
                ReservationBounds::for_deployment(p.input_ceiling, p.output_ceiling, requested)
                    .map_err(|_| Rejection::Unsupported)?;
            if request.operation != Operation::CountTokens {
                body["max_tokens"] = bounds.outputs.into();
            }
            (body, bounds)
        } else if p.endpoint == UpstreamProtocol::BedrockConverse {
            let mut body = (if p.auto_prompt_cache {
                cache_bedrock
            } else {
                bedrock_body
            })
            .and_then(Result::ok)
            .map(|(body, _)| body)
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
            let mut body = (if p.auto_prompt_cache {
                cache_conversion
            } else {
                conversion
            })
            .and_then(Result::ok)
            .map(|(body, _)| body)
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
        if let Some(stop) = &request.chat_stop
            && !applied.stop
        {
            let seq = serde_json::json!(stop);
            if p.endpoint == UpstreamProtocol::BedrockConverse && !p.bedrock_invoke {
                body["inferenceConfig"]["stopSequences"] = seq;
            } else {
                body[if dep.is_chat() {
                    "stop"
                } else {
                    "stop_sequences"
                }] = seq;
            }
        }
        if let Some(search) = &request.search {
            bounds.searches = retry
                .map(|(_, b)| b.searches)
                .or(search.requested_limit)
                .unwrap_or(p.web_search_ceiling);
            search.apply(&mut body, bounds.searches);
        }
        if let Some(file) = &request.file_search {
            bounds.file_searches = retry
                .map(|(_, b)| b.file_searches)
                .or(file.requested_limit)
                .unwrap_or(p.file_search_ceiling);
            // Responses has one cap shared by all hosted tools. Reserve each possible
            // call type up to the enforced cap, and charge only the observed actions.
            if bounds.searches > 0 {
                let cap = bounds.searches.min(bounds.file_searches);
                bounds.searches = cap;
                bounds.file_searches = cap;
            }
            body["max_tool_calls"] = bounds.file_searches.into();
        }
        bounds.interpreter = request.interpreter.as_ref().map(|t| t.target);
        if let Some(exec) = &request.execution {
            bounds.execution_mode = exec.mode;
            if exec.mode != crate::protocol::execution::ExecutionMode::Bundled {
                bounds.execution_seconds = retry
                    .map(|(_, b)| b.execution_seconds)
                    .unwrap_or(p.execution_reserve_seconds);
            }
        }
        if let Some(config) = request
            .bedrock
            .as_ref()
            .and_then(|b| b.guardrail_config.as_ref())
            .or(p.bedrock_guardrail.as_ref())
        {
            body["guardrailConfig"] = config.clone();
            bounds.guardrail_units =
                retry.map_or(p.bedrock_guardrail_unit_ceiling, |(_, f)| f.guardrail_units);
        }
        for (k, v) in p.bedrock_model_fields.iter().chain(
            request
                .bedrock
                .iter()
                .flat_map(|opts| &opts.additional_model_request_fields),
        ) {
            body["additionalModelRequestFields"][k] = v.clone();
        }
        // Server tool loops and compaction bill several model calls, each up to the
        // per-call input ceiling: reserve input for all of them, or reject before admission.
        let calls = crate::protocol::tokens::model_calls(
            if dep.is_chat() || p.endpoint == UpstreamProtocol::BedrockConverse && !p.bedrock_invoke
            {
                CallLoop::Single
            } else if p.endpoint.representation() == Endpoint::AnthropicMessages {
                CallLoop::Messages { batch: false }
            } else {
                CallLoop::Responses
            },
            request.operation != Operation::Generate,
            &mut body,
        )
        .map_err(|_| Rejection::Unsupported)?;
        bounds
            .cover_model_calls(calls)
            .map_err(|_| Rejection::Unsupported)?;
        if p.auto_prompt_cache {
            match p.endpoint {
                UpstreamProtocol::OpenaiResponses => {
                    if body.get("prompt_cache_key").is_none() {
                        if let Some(seed) = affinity {
                            body["prompt_cache_key"] = cache::provider_key(seed).into();
                        }
                    } else if let Some(value) = body["prompt_cache_key"].as_str() {
                        body["prompt_cache_key"] =
                            cache::scoped_key(key.organization_id, m.id, value).into();
                    }
                }
                UpstreamProtocol::AnthropicMessages
                    if request.endpoint == Endpoint::AnthropicMessages
                        && !cache::has_messages_breakpoint(&body) =>
                {
                    body["cache_control"] = serde_json::json!({"type":"ephemeral"});
                }
                _ => {}
            }
        }
        let selection = Selection {
            approximation: crate::protocol::approximation::Delivery::new(applied, request)
                .map_err(|_| Rejection::Unsupported)?,
            private_history: !request.restored.is_empty(),
            operation: request.operation,
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
