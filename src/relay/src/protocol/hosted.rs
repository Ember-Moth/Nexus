//! Provider tools, native routing and search bounds. Account-bound resources require their own bindings.
use super::ProtocolError;
use serde_json::Value;
use std::collections::HashSet;

pub(super) fn https_url(value: &Value) -> bool {
    value
        .as_str()
        .and_then(|s| url::Url::parse(s).ok())
        .is_some_and(|url| {
            url.scheme() == "https"
                && url.host().is_some()
                && url.username().is_empty()
                && url.password().is_none()
        })
}

pub(super) fn mcp_servers(body: &Value) -> Result<(), ProtocolError> {
    let mut names = HashSet::new();
    if let Some(servers) = body.get("mcp_servers") {
        for server in servers
            .as_array()
            .ok_or(ProtocolError::Invalid("mcp_servers"))?
        {
            let name = server["name"]
                .as_str()
                .filter(|s| !s.is_empty())
                .ok_or(ProtocolError::Invalid("MCP server name"))?;
            if server["type"] != "url"
                || !https_url(&server["url"])
                || server.get("tunnel_id").is_some()
                || !names.insert(name)
            {
                return Err(ProtocolError::Invalid("MCP server definition"));
            }
        }
    }
    let mut used = HashSet::new();
    for t in body["tools"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|t| t["type"] == "mcp_toolset")
    {
        let name = t["mcp_server_name"]
            .as_str()
            .ok_or(ProtocolError::Invalid("MCP toolset"))?;
        if !names.contains(name) || !used.insert(name) {
            return Err(ProtocolError::Invalid("MCP toolset binding"));
        }
    }
    Ok(())
}

pub(crate) fn native(body: &Value) -> bool {
    body.get("mcp_servers").is_some()
        || body["tools"].as_array().is_some_and(|tools| {
            tools.iter().any(|t| {
                matches!(
                    t["type"].as_str(),
                    Some("mcp" | "mcp_toolset" | "web_fetch_20250910")
                )
            })
        })
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum SearchVariant {
    #[default]
    Standard,
    Preview,
}

impl SearchVariant {
    fn responses(tool: &Value) -> Option<Self> {
        match tool["type"].as_str() {
            Some("web_search" | "web_search_2025_08_26") => Some(Self::Standard),
            Some("web_search_preview" | "web_search_preview_2025_03_11") => Some(Self::Preview),
            _ => None,
        }
    }
}
#[derive(Debug)]
pub(crate) struct WebSearch {
    pub variant: SearchVariant,
    messages_tool: Option<usize>,
    pub requested_limit: Option<u32>,
}

impl WebSearch {
    pub fn parse(
        endpoint: super::Endpoint,
        index: usize,
        body: &Value,
        tool: &Value,
    ) -> Result<Option<Self>, ProtocolError> {
        let (messages_tool, variant, limit) = match (endpoint, tool["type"].as_str()) {
            (
                super::Endpoint::AnthropicMessages,
                Some("web_search_20250305" | "web_search_20260209" | "web_search_20260318"),
            ) => {
                if tool["name"] != "web_search" {
                    return Err(ProtocolError::Invalid("web search name"));
                }
                (Some(index), SearchVariant::Standard, tool.get("max_uses"))
            }
            (super::Endpoint::OpenaiResponses, _) => {
                let Some(variant) = SearchVariant::responses(tool) else {
                    return Ok(None);
                };
                (None, variant, body.get("max_tool_calls"))
            }
            _ => return Ok(None),
        };
        let requested_limit = limit
            .filter(|v| !v.is_null())
            .map(|v| {
                v.as_u64()
                    .filter(|n| (1..=1_000_000_000).contains(n))
                    .map(|n| n as u32)
                    .ok_or(ProtocolError::Invalid("web search limit"))
            })
            .transpose()?;
        Ok(Some(Self {
            messages_tool,
            variant,
            requested_limit,
        }))
    }
    pub fn apply(&self, body: &mut Value, limit: u32) {
        if let Some(i) = self.messages_tool {
            body["tools"][i]["max_uses"] = limit.into();
        } else {
            body["max_tool_calls"] = limit.into();
        }
    }
}

pub(crate) fn responses_search(body: &Value) -> bool {
    body["tools"]
        .as_array()
        .is_some_and(|tools| tools.iter().any(|t| SearchVariant::responses(t).is_some()))
}

#[derive(Debug)]
pub(crate) struct FileSearch {
    pub tool: usize,
    pub stores: Vec<uuid::Uuid>,
    pub requested_limit: Option<u32>,
}
impl FileSearch {
    pub fn parse(tool: usize, body: &Value, spec: &Value) -> Result<Self, ProtocolError> {
        let ids = spec["vector_store_ids"]
            .as_array()
            .filter(|ids| !ids.is_empty())
            .ok_or(ProtocolError::Invalid("file search vector stores"))?;
        let mut stores = Vec::with_capacity(ids.len());
        let mut seen = HashSet::new();
        for id in ids {
            let handle = id
                .as_str()
                .and_then(|s| s.strip_prefix("vs_nx_"))
                .and_then(|s| s.parse().ok())
                .ok_or(ProtocolError::Invalid("owned vector store ID"))?;
            if !seen.insert(handle) {
                return Err(ProtocolError::Invalid("duplicate vector store"));
            }
            stores.push(handle);
        }
        let requested_limit = body
            .get("max_tool_calls")
            .filter(|v| !v.is_null())
            .map(|v| {
                v.as_u64()
                    .filter(|n| (1..=1_000_000_000).contains(n))
                    .map(|n| n as u32)
                    .ok_or(ProtocolError::Invalid("max_tool_calls"))
            })
            .transpose()?;
        Ok(Self {
            tool,
            stores,
            requested_limit,
        })
    }
}

#[derive(Debug, Default, Clone, Copy)]
pub(crate) struct SearchMeters {
    pub web: bool,
    pub files: bool,
}
impl SearchMeters {
    pub fn for_body(body: &Value) -> Self {
        Self {
            web: responses_search(body),
            files: body["tools"]
                .as_array()
                .is_some_and(|tools| tools.iter().any(|t| t["type"] == "file_search")),
        }
    }
}
impl From<super::tokens::ReservationBounds> for SearchMeters {
    fn from(b: super::tokens::ReservationBounds) -> Self {
        Self {
            web: b.searches > 0,
            files: b.file_searches > 0,
        }
    }
}

// Visit only provider-owned protocol fields. Tool arguments and customer metadata are opaque.
pub(crate) fn resource_fields(value: &Value) -> Vec<(String, bool)> {
    fn visit(value: &Value, path: &str, out: &mut Vec<(String, bool)>) {
        match value["type"].as_str() {
            Some("file_citation" | "file_path") if value.get("file_id").is_some() => {
                out.push((format!("{path}/file_id"), false))
            }
            Some("code_interpreter") if value["container"].is_object() => {
                for (i, _) in value["container"]["file_ids"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .enumerate()
                {
                    out.push((format!("{path}/container/file_ids/{i}"), false));
                }
            }
            Some("file_search") => {
                for (i, _) in value["vector_store_ids"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .enumerate()
                {
                    out.push((format!("{path}/vector_store_ids/{i}"), true));
                }
            }
            Some("file_search_call") => {
                for (i, r) in value["results"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .enumerate()
                {
                    if r.get("file_id").is_some() {
                        out.push((format!("{path}/results/{i}/file_id"), false));
                    }
                }
            }
            _ => {}
        }
        for key in ["response", "item", "part", "annotation"] {
            if let Some(v) = value.get(key).filter(|v| v.is_object()) {
                visit(v, &format!("{path}/{key}"), out);
            }
        }
        for key in ["output", "tools", "content", "annotations", "data"] {
            for (i, v) in value[key].as_array().into_iter().flatten().enumerate() {
                if v.is_object() {
                    visit(v, &format!("{path}/{key}/{i}"), out);
                }
            }
        }
    }
    let mut fields = Vec::new();
    visit(value, "", &mut fields);
    fields
}
