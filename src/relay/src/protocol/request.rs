use super::tokens::ReservationBounds;
use super::{Endpoint, ProtocolError};
use http::{HeaderMap, Method};
use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Route {
    Chat,
    Generate(Endpoint),
    Compact,
    Models,
    CountTokens,
    ResponsesWebSocket,
}

pub fn route(method: &Method, path: &str) -> Option<Route> {
    match (method.as_str(), path) {
        ("POST", "/v1/chat/completions") => Some(Route::Chat),
        ("POST", "/v1/messages") => Some(Route::Generate(Endpoint::AnthropicMessages)),
        ("POST", "/v1/responses") => Some(Route::Generate(Endpoint::OpenaiResponses)),
        ("POST", "/v1/responses/compact") => Some(Route::Compact),
        ("GET", "/v1/models") => Some(Route::Models),
        ("POST", "/v1/messages/count_tokens") => Some(Route::CountTokens),
        ("GET", "/v1/responses") => Some(Route::ResponsesWebSocket),
        _ => None,
    }
}

fn single_header<'a>(headers: &'a HeaderMap, name: &'static str) -> Result<&'a str, ProtocolError> {
    let mut all = headers.get_all(name).iter();
    let first = all
        .next()
        .and_then(|h| h.to_str().ok())
        .ok_or(ProtocolError::Invalid(name))?;
    if all.next().is_some() {
        return Err(ProtocolError::Invalid(name));
    }
    Ok(first)
}

pub fn cache_key_header(headers: &HeaderMap) -> Result<Option<String>, ProtocolError> {
    let name = "x-nexus-cache-key";
    if !headers.contains_key(name) {
        return Ok(None);
    }
    let key = single_header(headers, name)?;
    if !super::cache::valid_key(key) {
        return Err(ProtocolError::Invalid(name));
    }
    Ok(Some(key.to_owned()))
}

impl Route {
    pub fn key(self, headers: &HeaderMap) -> Result<&str, ProtocolError> {
        let key = match self {
            Self::Generate(Endpoint::AnthropicMessages) | Self::CountTokens => {
                single_header(headers, "x-api-key")?
            }
            _ => {
                let value = single_header(headers, "authorization")?;
                let (scheme, token) = value
                    .split_once(' ')
                    .ok_or(ProtocolError::Invalid("bearer credential"))?;
                if !scheme.eq_ignore_ascii_case("bearer") {
                    return Err(ProtocolError::Invalid("bearer credential"));
                }
                token
            }
        };
        if key.is_empty()
            || key
                .bytes()
                .any(|byte| !byte.is_ascii_graphic() || byte == b',')
        {
            return Err(ProtocolError::Invalid("API key"));
        }
        Ok(key)
    }
}

/// Only provider version/feature negotiation crosses the tenant HTTP boundary.
#[derive(Clone, Debug, Default)]
pub struct AnthropicHeaders {
    headers: HeaderMap,
}

impl AnthropicHeaders {
    pub fn parse(headers: &HeaderMap) -> Result<Self, ProtocolError> {
        let mut out = HeaderMap::new();
        if headers.contains_key("anthropic-version") {
            let version = single_header(headers, "anthropic-version")?;
            if version.len() != 10
                || !version.bytes().enumerate().all(|(i, b)| {
                    if i == 4 || i == 7 {
                        b == b'-'
                    } else {
                        b.is_ascii_digit()
                    }
                })
            {
                return Err(ProtocolError::Invalid("anthropic-version"));
            }
            out.insert("anthropic-version", headers["anthropic-version"].clone());
        }
        let mut flags = Vec::new();
        for value in headers.get_all("anthropic-beta") {
            for flag in value
                .to_str()
                .map_err(|_| ProtocolError::Invalid("anthropic-beta"))?
                .split(',')
            {
                let flag = flag.trim();
                if flag.is_empty()
                    || flag.len() > 128
                    || flags.len() >= 32
                    || !flag
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
                {
                    return Err(ProtocolError::Invalid("anthropic-beta"));
                }
                flags.push(flag);
            }
        }
        if !flags.is_empty() {
            out.insert(
                "anthropic-beta",
                flags
                    .join(",")
                    .parse()
                    .map_err(|_| ProtocolError::Invalid("anthropic-beta"))?,
            );
        }
        Ok(Self { headers: out })
    }
    pub(crate) fn negotiated(&self) -> serde_json::Value {
        serde_json::Value::Object(
            self.headers
                .iter()
                .map(|(k, v)| {
                    (
                        k.as_str().into(),
                        v.to_str().expect("validated ASCII header").into(),
                    )
                })
                .collect(),
        )
    }
    pub(crate) fn apply(&self, mut call: reqwest::RequestBuilder) -> reqwest::RequestBuilder {
        if !self.headers.contains_key("anthropic-version") {
            call = call.header("anthropic-version", "2023-06-01");
        }
        call.headers(self.headers.clone())
    }
    fn flags(&self) -> impl Iterator<Item = &str> {
        self.headers
            .get("anthropic-beta")
            .and_then(|s| s.to_str().ok())
            .into_iter()
            .flat_map(|s| s.split(','))
    }
    pub(crate) fn threshold(&self) -> bool {
        self.flags().any(|s| s == "compact-2026-01-12")
    }
    pub(crate) fn portable(&self, compact: bool, files: bool) -> bool {
        self.headers
            .get("anthropic-version")
            .is_none_or(|v| v == "2023-06-01")
            && self.flags().all(|s| {
                s == "compact-2026-01-12" && compact || s == "files-api-2025-04-14" && files
            })
    }
    pub(crate) fn for_converted(body: &Value) -> Self {
        let compact = body.get("context_management").is_some()
            || body["messages"]
                .as_array()
                .into_iter()
                .flatten()
                .flat_map(|m| m["content"].as_array().into_iter().flatten())
                .any(|b| b["type"] == "compaction");
        let flags = match (compact, uses_message_files(body)) {
            (true, true) => "compact-2026-01-12,files-api-2025-04-14",
            (true, false) => "compact-2026-01-12",
            (false, true) => "files-api-2025-04-14",
            (false, false) => return Self::default(),
        };
        let mut h = Self::default();
        h.headers
            .insert("anthropic-beta", http::HeaderValue::from_static(flags));
        h
    }
    pub(crate) fn requires_native(&self) -> bool {
        self.headers.contains_key("anthropic-beta")
            || self
                .headers
                .get("anthropic-version")
                .is_some_and(|v| v != "2023-06-01")
    }
}

/// Distinguishes HTTP operations that share the same native Responses usage schema.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Operation {
    CountTokens,
    Generate,
    Compact,
}

#[derive(Clone, Debug)]
pub(crate) struct ConversationId {
    pub public: String,
    pub provider: String,
}
impl ConversationId {
    pub fn handle(s: &str) -> Option<uuid::Uuid> {
        s.strip_prefix("conv_nx_")?.parse().ok()
    }
    pub fn rewrite(&self, response: &mut Value) -> Result<(), ProtocolError> {
        if let Some(conv) = response.get_mut("conversation").filter(|v| !v.is_null()) {
            if conv["id"] != self.provider {
                return Err(ProtocolError::Invalid("conversation binding"));
            }
            conv["id"] = self.public.clone().into();
        }
        Ok(())
    }
}

/// Keep the native body intact until a specific authorized deployment is selected.
#[derive(Debug)]
pub struct Generate {
    pub model: String,
    pub endpoint: Endpoint,
    pub(crate) operation: Operation,
    pub stream: bool,
    pub requested_output: Option<u32>,
    body: Value,
    pub(crate) restored: super::history::Restored,
    has_private_history: bool,
    affinity_key: Option<String>,
    pub(crate) anthropic: AnthropicHeaders,
    pub(crate) bedrock: Option<crate::bedrock::RequestOptions>,
    pub(crate) interpreter: Option<super::execution::Interpreter>,
    pub(crate) execution: Option<super::execution::Execution>,
    pub(crate) file_search: Option<super::hosted::FileSearch>,
    pub(crate) search: Option<super::hosted::WebSearch>,
    pub(crate) chat: Option<bool>,
    pub allow_approximation: super::approximation::Choices,
    pub(crate) chat_stop: Option<Vec<String>>,
    pub(crate) conversation: Option<ConversationId>,
    pub(crate) stored_binding: Option<(uuid::Uuid, uuid::Uuid)>,
}

impl Generate {
    pub fn parse(
        endpoint: Endpoint,
        mut body: Value,
        websocket: bool,
    ) -> Result<Self, ProtocolError> {
        let bedrock = body
            .as_object_mut()
            .and_then(|o| o.remove("bedrock"))
            .map(crate::bedrock::RequestOptions::parse)
            .transpose()?;
        if bedrock.is_some() && websocket {
            return Err(ProtocolError::Unsupported("Bedrock WebSocket"));
        }
        let obj = body
            .as_object()
            .ok_or(ProtocolError::Invalid("request object"))?;
        let model = obj
            .get("model")
            .and_then(Value::as_str)
            .filter(|v| !v.is_empty() && v.len() <= 200)
            .ok_or(ProtocolError::Invalid("model"))?
            .to_owned();
        let stream = match obj.get("stream") {
            Some(_) if websocket => {
                return Err(ProtocolError::Unsupported("stream in WebSocket create"));
            }
            Some(v) => v.as_bool().ok_or(ProtocolError::Invalid("stream"))?,
            None => websocket,
        };
        let output_field = match endpoint {
            Endpoint::AnthropicMessages => {
                super::hosted::mcp_servers(&body)?;

                if websocket {
                    return Err(ProtocolError::Unsupported("Messages WebSocket"));
                }
                if !obj
                    .get("messages")
                    .and_then(Value::as_array)
                    .is_some_and(|v| !v.is_empty())
                {
                    return Err(ProtocolError::Invalid("messages"));
                }
                "max_tokens"
            }
            Endpoint::OpenaiResponses => {
                if obj.get("store").is_some_and(|v| !v.is_boolean())
                    || obj.get("conversation").is_some_and(|v| {
                        !v.is_null()
                            && (v
                                .as_str()
                                .or_else(|| v["id"].as_str())
                                .and_then(ConversationId::handle)
                                .is_none()
                                || v.as_object().is_some_and(|o| o.len() != 1)
                                || obj
                                    .get("previous_response_id")
                                    .is_some_and(|p| !p.is_null()))
                    })
                    || obj.contains_key("prompt")
                    || obj.get("background").is_some_and(|v| {
                        !v.is_boolean() || v == true && obj.get("store").is_some_and(|s| s == false)
                    })
                    || (websocket && obj.contains_key("background"))
                {
                    return Err(ProtocolError::Unsupported("stored or background response"));
                }
                // Account-scoped references are checked against the output of an owned parent.
                Self::input_files(&body["input"])?;
                if !Self::references(&body)?.is_empty()
                    && obj
                        .get("previous_response_id")
                        .and_then(Value::as_str)
                        .and_then(Self::handle)
                        .is_none()
                {
                    return Err(ProtocolError::Unsupported(
                        "stored item requires an owned parent",
                    ));
                }
                if obj.get("previous_response_id").is_some_and(|v| {
                    !v.is_null()
                        && v.as_str().is_none_or(|s| {
                            if websocket {
                                s.is_empty()
                            } else {
                                Self::handle(s).is_none()
                            }
                        })
                }) {
                    return Err(ProtocolError::Unsupported("previous_response_id"));
                }
                let owned_context = obj
                    .get("previous_response_id")
                    .is_some_and(|v| !v.is_null())
                    || obj.get("conversation").is_some_and(|v| !v.is_null());
                if !obj.get("input").map_or(owned_context, |v| {
                    v.is_string() || v.is_array() || v.is_null() && owned_context
                }) {
                    return Err(ProtocolError::Invalid("input"));
                }
                "max_output_tokens"
            }
        };
        let execution = if endpoint == Endpoint::AnthropicMessages {
            super::execution::Execution::messages(&body)?
        } else {
            None
        };
        let mut interpreter = None;
        let mut search = None;
        let mut file_search = None;
        if let Some(tools) = obj.get("tools") {
            for (index, tool) in tools
                .as_array()
                .ok_or(ProtocolError::Invalid("tools"))?
                .iter()
                .enumerate()
            {
                if endpoint == Endpoint::OpenaiResponses && tool["type"] == "code_interpreter" {
                    if interpreter
                        .replace(super::execution::Interpreter::parse(index, tool)?)
                        .is_some()
                    {
                        return Err(ProtocolError::Invalid("duplicate code interpreter"));
                    }
                    continue;
                }
                if endpoint == Endpoint::OpenaiResponses && tool["type"] == "file_search" {
                    if file_search
                        .replace(super::hosted::FileSearch::parse(index, &body, tool)?)
                        .is_some()
                    {
                        return Err(ProtocolError::Invalid("duplicate file search tool"));
                    }
                    continue;
                }
                if let Some(config) = super::hosted::WebSearch::parse(endpoint, index, &body, tool)?
                {
                    if search.replace(config).is_some() {
                        return Err(ProtocolError::Invalid("duplicate web search tool"));
                    }
                    continue;
                }
                match (endpoint, tool["type"].as_str()) {
                    (Endpoint::AnthropicMessages, _)
                        if super::execution::code_tool(tool)
                            || super::execution::dynamic_fetch(tool) =>
                    {
                        continue;
                    }
                    (Endpoint::AnthropicMessages, Some("mcp_toolset")) => continue,
                    (Endpoint::AnthropicMessages, Some("web_fetch_20250910"))
                        if tool["name"] == "web_fetch" =>
                    {
                        continue;
                    }
                    (Endpoint::OpenaiResponses, Some("mcp"))
                        if super::hosted::https_url(&tool["server_url"])
                            && tool["server_label"].as_str().is_some_and(|v| !v.is_empty())
                            && tool.get("connector_id").is_none()
                            && tool.get("tunnel_id").is_none() =>
                    {
                        continue;
                    }
                    _ => {}
                }
                let tool = tool.as_object().ok_or(ProtocolError::Invalid("tool"))?;
                let custom = match endpoint {
                    Endpoint::OpenaiResponses => {
                        matches!(
                            tool.get("type").and_then(Value::as_str),
                            Some("function" | "custom")
                        )
                    }
                    Endpoint::AnthropicMessages => {
                        !tool.contains_key("type")
                            || tool.get("type").and_then(Value::as_str) == Some("custom")
                    }
                };
                if !custom {
                    return Err(ProtocolError::Unsupported("hosted tool"));
                }
                if !tool
                    .get("name")
                    .and_then(Value::as_str)
                    .is_some_and(|s| !s.is_empty())
                {
                    return Err(ProtocolError::Invalid("tool name"));
                }
            }
        }
        if let Some(t) = &mut interpreter {
            t.target.preloaded |= body["input"].as_array().into_iter().flatten().any(|item| {
                item["content"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .chain(item["output"].as_array().into_iter().flatten())
                    .any(|p| matches!(p["type"].as_str(), Some("input_file" | "input_image")))
            });
        }
        let requested_output = obj
            .get(output_field)
            .map(|v| {
                v.as_u64()
                    .filter(|n| (1..=1_000_000_000).contains(n))
                    .map(|n| n as u32)
                    .ok_or(ProtocolError::Invalid(output_field))
            })
            .transpose()?;
        if endpoint == Endpoint::AnthropicMessages && requested_output.is_none() {
            return Err(ProtocolError::Invalid(output_field));
        }
        let has_private_history =
            private_entries(endpoint, &body).any(|(item, _)| super::history::reserved(item));
        Ok(Self {
            model,
            endpoint,
            operation: Operation::Generate,
            stream,
            requested_output,
            affinity_key: None,
            restored: Default::default(),
            has_private_history,
            anthropic: AnthropicHeaders::default(),
            bedrock,
            interpreter,
            execution,
            file_search,
            search,
            chat: None,
            allow_approximation: Default::default(),
            chat_stop: None,
            conversation: None,
            stored_binding: None,
            body,
        })
    }

    pub fn parse_chat(mut body: Value) -> Result<Self, ProtocolError> {
        let stop = body.as_object_mut().and_then(|o| o.remove("stop"));
        let stop = match stop {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) if !s.is_empty() => Some(vec![s]),
            Some(Value::Array(values)) if values.len() <= 4 => {
                let stops = values
                    .into_iter()
                    .map(|s| match s {
                        Value::String(s) if !s.is_empty() => Ok(s),
                        _ => Err(ProtocolError::Invalid("stop sequence")),
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                (!stops.is_empty()).then_some(stops)
            }
            _ => return Err(ProtocolError::Invalid("stop sequences")),
        };
        let (body, include_usage) = super::chat::request(&body)?;
        let mut request = Self::parse(Endpoint::OpenaiResponses, body, false)?;
        request.chat = Some(include_usage);
        request.chat_stop = stop;
        Ok(request)
    }

    pub fn parse_count(mut body: Value) -> Result<Self, ProtocolError> {
        let obj = body
            .as_object_mut()
            .ok_or(ProtocolError::Invalid("count request"))?;
        if obj.contains_key("max_tokens") || obj.contains_key("stream") {
            return Err(ProtocolError::Invalid("count request"));
        }
        // Generation and counting have the same explicit message history. This sentinel
        // is used only during validation and is removed before routing or transmission.
        obj.insert("max_tokens".into(), 1.into());
        let mut req = Self::parse(Endpoint::AnthropicMessages, body, false)?;
        req.body.as_object_mut().unwrap().remove("max_tokens");
        req.requested_output = None;
        req.operation = Operation::CountTokens;
        Ok(req)
    }

    pub fn parse_compact(body: Value) -> Result<Self, ProtocolError> {
        // Both explicit input and references to a tenant-owned parent are supported.
        let obj = super::convert::fields(
            &body,
            &[
                "model",
                "input",
                "instructions",
                "image",
                "previous_response_id",
                "prompt_cache_key",
                "prompt_cache_options",
                "prompt_cache_retention",
                "service_tier",
            ],
        )?;
        if obj.get("input").is_none_or(Value::is_null)
            && obj
                .get("previous_response_id")
                .and_then(Value::as_str)
                .and_then(Self::handle)
                .is_some()
        {
            let mut explicit = body;
            explicit["input"] = serde_json::json!([]);
            let mut request = Self::parse(Endpoint::OpenaiResponses, explicit, false)?;
            request.body.as_object_mut().unwrap().remove("input");
            request.operation = Operation::Compact;
            return Ok(request);
        }
        if obj
            .get("input")
            .and_then(Value::as_array)
            .is_some_and(|items| items.iter().any(|item| !item.is_object()))
        {
            return Err(ProtocolError::Invalid("explicit compaction history"));
        }
        let mut request = Self::parse(Endpoint::OpenaiResponses, body, false)?;
        request.operation = Operation::Compact;
        Ok(request)
    }

    /// References with an optional required provider output type.
    pub(crate) fn references(
        body: &Value,
    ) -> Result<Vec<(&str, Option<&'static str>)>, ProtocolError> {
        let mut references = Vec::new();
        for item in body["input"].as_array().into_iter().flatten() {
            let (key, kind) = match item["type"].as_str() {
                Some("item_reference") => ("id", None),
                None if item.get("id").is_some()
                    && item.as_object().is_some_and(|o| o.len() == 1) =>
                {
                    ("id", None)
                }
                Some("mcp_approval_response") => {
                    ("approval_request_id", Some("mcp_approval_request"))
                }
                _ => continue,
            };
            let id = item[key]
                .as_str()
                .filter(|s| !s.is_empty() && s.len() <= 255)
                .ok_or(ProtocolError::Invalid("stored item ID"))?;
            references.push((id, kind));
        }
        Ok(references)
    }

    pub fn handle(value: &str) -> Option<uuid::Uuid> {
        value
            .strip_prefix("resp_nx_")
            .and_then(|s| uuid::Uuid::parse_str(s).ok())
    }
    pub(crate) fn conversation_handle(&self) -> Option<uuid::Uuid> {
        let v = &self.body["conversation"];
        v.as_str()
            .or_else(|| v["id"].as_str())
            .and_then(ConversationId::handle)
    }
    pub(crate) fn bind_conversation(&mut self, id: ConversationId) {
        if self.body["conversation"].is_object() {
            self.body["conversation"]["id"] = id.provider.clone().into();
        } else {
            self.body["conversation"] = id.provider.clone().into();
        }
        self.conversation = Some(id);
    }

    pub fn background(&self) -> bool {
        self.body["background"] == true
    }
    pub fn stored(&self) -> bool {
        self.body["store"] == true || self.background()
    }
    pub fn needs_stored_upstream(&self) -> bool {
        self.conversation.is_some()
            || self.stored()
            || self.previous_response_id().is_some() && self.stored_binding.is_some()
    }
    pub(crate) fn bind_previous(
        &mut self,
        stored: crate::control::database::StoredResponse,
    ) -> Result<(), ProtocolError> {
        self.bind_deployment(stored.model, stored.deployment)?;
        self.body["previous_response_id"] = stored.provider_id.into();
        Ok(())
    }

    pub(crate) fn item_file_fields<'a>(
        item: &'a Value,
        path: &str,
    ) -> Result<Vec<(String, &'a str)>, ProtocolError> {
        let mut fields = Vec::new();
        for field in ["content", "output"] {
            for (j, part) in item[field].as_array().into_iter().flatten().enumerate() {
                if let Some(id) = part.get("file_id").filter(|v| !v.is_null()) {
                    if !matches!(part["type"].as_str(), Some("input_file" | "input_image")) {
                        return Err(ProtocolError::Unsupported("file content part"));
                    }
                    fields.push((
                        format!("{path}/{field}/{j}/file_id"),
                        id.as_str().ok_or(ProtocolError::Invalid("file ID"))?,
                    ));
                }
            }
        }
        if item["output"].get("file_id").is_some_and(|v| !v.is_null()) {
            return Err(ProtocolError::Unsupported("screenshot file reference"));
        }
        Ok(fields)
    }

    pub(crate) fn input_files(items: &Value) -> Result<Vec<(String, uuid::Uuid)>, ProtocolError> {
        let mut refs = Vec::new();
        for (i, item) in items.as_array().into_iter().flatten().enumerate() {
            let root = format!("/{i}");
            let mut fields = Self::item_file_fields(item, &root)?;
            for (path, vector) in super::hosted::resource_fields(item) {
                if vector {
                    return Err(ProtocolError::Unsupported("vector store in history"));
                }
                let pid = item
                    .pointer(&path)
                    .and_then(Value::as_str)
                    .ok_or(ProtocolError::Invalid("file citation"))?;
                fields.push((format!("{root}{path}"), pid));
            }
            for (path, id) in fields {
                let id = id
                    .strip_prefix("file_nx_")
                    .and_then(|s| s.parse().ok())
                    .ok_or(ProtocolError::Invalid("owned OpenAI file reference"))?;
                refs.push((path, id));
            }
        }
        Ok(refs)
    }

    pub(crate) fn file_handles(&self) -> Result<Vec<(String, uuid::Uuid)>, ProtocolError> {
        if self.endpoint == Endpoint::OpenaiResponses {
            let mut refs = Self::input_files(&self.body["input"])?
                .into_iter()
                .map(|(path, id)| (format!("/input{path}"), id))
                .collect::<Vec<_>>();
            if let Some(tool) = &self.interpreter {
                for (i, file) in self.body["tools"][tool.index]["container"]["file_ids"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .enumerate()
                {
                    let id = file
                        .as_str()
                        .and_then(|s| s.strip_prefix("file_nx_"))
                        .and_then(|s| s.parse().ok())
                        .ok_or(ProtocolError::Invalid("owned interpreter file"))?;
                    refs.push((format!("/tools/{}/container/file_ids/{i}", tool.index), id));
                }
            }
            return Ok(refs);
        }
        fn collect(
            block: &Value,
            path: &str,
            refs: &mut Vec<(String, uuid::Uuid)>,
        ) -> Result<(), ProtocolError> {
            if block["source"]["type"] == "file" {
                if !matches!(block["type"].as_str(), Some("image" | "document")) {
                    return Err(ProtocolError::Unsupported("file content block"));
                }
                let id = block["source"]["file_id"]
                    .as_str()
                    .and_then(|s| s.strip_prefix("file_nx_"))
                    .and_then(|s| s.parse().ok())
                    .ok_or(ProtocolError::Invalid("owned file ID"))?;
                refs.push((format!("{path}/source/file_id"), id));
            }
            // Traverse provider content blocks, leaving arbitrary client tool arguments alone.
            let mut artifacts = Vec::new();
            if block["type"] == "container_upload" {
                artifacts.push(format!("{path}/file_id"));
            }
            super::execution::result_files(block, path, &mut artifacts);
            for field in artifacts {
                let relative = field.strip_prefix(path).expect("current block prefix");
                let id = block
                    .pointer(relative)
                    .and_then(Value::as_str)
                    .and_then(|s| s.strip_prefix("file_nx_"))
                    .and_then(|s| s.parse().ok())
                    .ok_or(ProtocolError::Invalid("owned execution file"))?;
                refs.push((field, id));
            }
            let children = match block["type"].as_str() {
                Some("tool_result" | "mcp_tool_result" | "search_result") => {
                    Some(("content", &block["content"]))
                }
                Some("document") if block["source"]["type"] == "content" => {
                    Some(("source/content", &block["source"]["content"]))
                }
                _ => None,
            };
            if let Some((key, children)) = children {
                for (index, child) in children.as_array().into_iter().flatten().enumerate() {
                    collect(child, &format!("{path}/{key}/{index}"), refs)?;
                }
            }
            Ok(())
        }
        let mut refs = Vec::new();
        if self.endpoint == Endpoint::AnthropicMessages {
            for (i, m) in self.body["messages"]
                .as_array()
                .into_iter()
                .flatten()
                .enumerate()
            {
                for (j, b) in m["content"].as_array().into_iter().flatten().enumerate() {
                    collect(b, &format!("/messages/{i}/content/{j}"), &mut refs)?;
                }
            }
        }
        Ok(refs)
    }
    pub(crate) fn bind_interpreter(&mut self, pid: String, tier: super::execution::MemoryTier) {
        if let Some(tool) = &mut self.interpreter {
            tool.target.tier = tier;
            self.body["tools"][tool.index]["container"] = pid.into();
        }
    }
    pub(crate) fn historical_container_fields(&self) -> Vec<super::execution::ContainerField> {
        let mut refs = Vec::new();
        if self.endpoint == Endpoint::OpenaiResponses {
            for (i, item) in self.body["input"]
                .as_array()
                .into_iter()
                .flatten()
                .enumerate()
            {
                for mut f in super::execution::openai_fields(item) {
                    f.path = format!("/input/{i}{}", f.path);
                    f.file = f.file.map(|p| format!("/input/{i}{p}"));
                    refs.push(f);
                }
            }
        }
        refs
    }

    pub(crate) fn container_provider(&self) -> Option<&str> {
        self.execution.as_ref()?.container?;
        self.body["container"]
            .as_str()
            .or_else(|| self.body["container"]["id"].as_str())
    }
    pub(crate) fn set_container(&mut self, pid: String) {
        if self.body["container"].is_object() {
            self.body["container"]["id"] = pid.into();
        } else {
            self.body["container"] = pid.into();
        }
    }

    pub(crate) fn set_vector_stores(&mut self, ids: Vec<String>) {
        if let Some(ref f) = self.file_search {
            self.body["tools"][f.tool]["vector_store_ids"] = serde_json::json!(ids);
        }
    }
    pub(crate) fn set_file(&mut self, path: &str, provider: String) {
        *self
            .body
            .pointer_mut(path)
            .expect("validated request file field") = provider.into();
    }

    pub(crate) fn bind_deployment(
        &mut self,
        model: uuid::Uuid,
        dep: uuid::Uuid,
    ) -> Result<(), ProtocolError> {
        if self
            .stored_binding
            .is_some_and(|binding| binding != (model, dep))
        {
            return Err(ProtocolError::Invalid("resource deployment mismatch"));
        }
        self.stored_binding = Some((model, dep));
        Ok(())
    }

    pub fn set_affinity_key(&mut self, key: Option<String>) {
        self.affinity_key = key;
    }

    pub fn affinity_key(&self) -> Option<&str> {
        self.affinity_key.as_deref()
    }

    pub fn has_tools(&self) -> bool {
        // Tool calls in a compact input are historical items, not tools to execute.
        if self.operation == Operation::Compact {
            return false;
        }
        self.restored.changes_tools()
            || self.execution.is_some()
            || self.body.get("mcp_servers").is_some()
            || self
                .body
                .get("tools")
                .and_then(Value::as_array)
                .is_some_and(|tools| !tools.is_empty())
            || self.body.get("tool_choice").is_some_and(|choice| {
                choice != "none" && choice.get("type").is_none_or(|kind| kind != "none")
            })
            || self
                .body
                .get("input")
                .and_then(Value::as_array)
                .is_some_and(|items| {
                    items.iter().any(|i| {
                        matches!(
                            i["type"].as_str(),
                            Some(
                                "function_call"
                                    | "function_call_output"
                                    | "custom_tool_call"
                                    | "custom_tool_call_output"
                            )
                        )
                    })
                })
            || self
                .body
                .get("messages")
                .and_then(Value::as_array)
                .is_some_and(|messages| {
                    messages
                        .iter()
                        .filter_map(|m| m["content"].as_array())
                        .flatten()
                        .any(|b| matches!(b["type"].as_str(), Some("tool_use" | "tool_result")))
                })
    }

    pub(crate) fn body(&self) -> &Value {
        &self.body
    }

    pub fn convert(&self) -> Result<Value, ProtocolError> {
        self.convert_history(false, false)
    }
    pub(crate) fn convert_history(
        &self,
        cache: bool,
        converse: bool,
    ) -> Result<Value, ProtocolError> {
        super::convert::request::with_history(
            self.endpoint,
            &self.body,
            cache,
            converse,
            &self.restored,
        )
    }

    pub(crate) fn has_private_history(&self) -> bool {
        self.has_private_history
    }
    pub(crate) fn bind_history(
        &mut self,
        codec: &super::history::HistoryKey,
        index: &crate::control::snapshot::Index,
        enterprise: uuid::Uuid,
    ) -> Result<(), ProtocolError> {
        if self.endpoint == Endpoint::AnthropicMessages {
            for (t, m) in self.body["messages"]
                .as_array()
                .into_iter()
                .flatten()
                .enumerate()
            {
                if let Some(parts) = m["content"].as_array()
                    && parts.iter().any(super::history::reserved)
                {
                    if m["role"] != "assistant" {
                        return Err(ProtocolError::Invalid("private history role"));
                    }
                    self.restored
                        .read(parts, t, codec, index, enterprise, self.endpoint)?;
                }
            }
        } else if let Some(items) = self.body["input"].as_array() {
            self.restored
                .read(items, 0, codec, index, enterprise, self.endpoint)?;
        }
        self.restored.validate_tools(&self.body)?;
        if let Some((model, dep)) = self.restored.binding {
            if !index.history_source(&self.model, model, dep) {
                return Err(ProtocolError::Invalid("private history model"));
            }
            self.bind_deployment(model, dep)?;
        }
        Ok(())
    }

    pub(crate) fn portable_headers(&self) -> bool {
        self.anthropic.portable(
            self.body.get("context_management").is_some() || self.restored.has_compaction(),
            uses_message_files(&self.body) || self.restored.has_file_annotations(),
        )
    }
    pub(crate) fn strip_citation_file(&mut self, path: &str) {
        let parent = path.strip_suffix("/file_id").expect("citation file path");
        self.body
            .pointer_mut(parent)
            .and_then(Value::as_object_mut)
            .expect("citation object")
            .remove("file_id");
    }
    pub(crate) fn needs_document_citations(&self) -> bool {
        self.endpoint == Endpoint::OpenaiResponses
            && self.body["input"]
                .as_array()
                .into_iter()
                .flatten()
                .any(|i| {
                    i["content"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .chain(i["output"].as_array().into_iter().flatten())
                        .any(|p| p["type"] == "input_file" && p.get("citations").is_some())
                })
    }
    pub(crate) fn needs_responses_tools(&self) -> bool {
        self.endpoint == Endpoint::OpenaiResponses
            && (self.body["tools"]
                .as_array()
                .is_some_and(|a| a.iter().any(|t| t["type"] == "custom"))
                || self.body["input"].as_array().is_some_and(|a| {
                    a.iter().any(|v| {
                        matches!(
                            v["type"].as_str(),
                            Some("custom_tool_call" | "custom_tool_call_output")
                        )
                    })
                }))
    }

    pub fn previous_response_id(&self) -> Option<&str> {
        self.body
            .get("previous_response_id")
            .and_then(Value::as_str)
    }

    pub fn prepare_native(
        &self,
        upstream_model: &str,
        inputs: u32,
        outputs: u32,
    ) -> Result<(Value, ReservationBounds), ProtocolError> {
        self.prepare_bounded(upstream_model, inputs, outputs, self.requested_output)
    }

    /// Prepare the native body with an explicit output limit, such as one already reserved.
    pub fn prepare_bounded(
        &self,
        upstream_model: &str,
        inputs: u32,
        outputs: u32,
        requested_output: Option<u32>,
    ) -> Result<(Value, ReservationBounds), ProtocolError> {
        if self.has_private_history() {
            return Err(ProtocolError::Unsupported(
                "converted history on native route",
            ));
        }
        let bounds = ReservationBounds::for_deployment(inputs, outputs, requested_output)?;
        let mut body = self.body.clone();
        body["model"] = upstream_model.into();
        if self.endpoint == Endpoint::OpenaiResponses && self.operation == Operation::Generate {
            body["store"] = self.stored().into();
            body["max_output_tokens"] = bounds.outputs.into();
        }
        Ok((body, bounds))
    }
}

fn private_entries(endpoint: Endpoint, body: &Value) -> impl Iterator<Item = (&Value, bool)> {
    let responses = body["input"]
        .as_array()
        .filter(|_| endpoint == Endpoint::OpenaiResponses)
        .into_iter()
        .flatten()
        .map(|v| (v, true));
    let messages = body["messages"]
        .as_array()
        .filter(|_| endpoint == Endpoint::AnthropicMessages)
        .into_iter()
        .flatten()
        .flat_map(|m| {
            m["content"]
                .as_array()
                .into_iter()
                .flatten()
                .map(move |b| (b, m["role"] == "assistant"))
        });
    responses.chain(messages)
}

fn uses_message_files(body: &Value) -> bool {
    fn has(block: &Value) -> bool {
        block["source"]["type"] == "file"
            || matches!(
                block["type"].as_str(),
                Some("tool_result" | "search_result")
            ) && block["content"].as_array().into_iter().flatten().any(has)
    }
    body["messages"]
        .as_array()
        .into_iter()
        .flatten()
        .flat_map(|m| m["content"].as_array().into_iter().flatten())
        .any(has)
}
