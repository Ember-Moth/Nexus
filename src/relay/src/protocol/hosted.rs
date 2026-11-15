//! Provider tools, native routing and search bounds. Account-bound resources require their own bindings.
use super::ProtocolError;
use serde_json::Value;
use std::collections::HashSet;

/// A remote tool endpoint the provider calls on the tenant's behalf. Private, loopback,
/// link-local and other non-public destinations are refused even though the provider,
/// not the node, opens the connection.
pub(super) fn https_url(value: &Value) -> bool {
    value
        .as_str()
        .and_then(|s| url::Url::parse(s).ok())
        .is_some_and(|url| {
            url.scheme() == "https"
                && url.host().is_some_and(public_host)
                && url.username().is_empty()
                && url.password().is_none()
        })
}

pub(crate) fn public_host(host: url::Host<&str>) -> bool {
    match host {
        url::Host::Ipv4(ip) => public_v4(ip),
        url::Host::Ipv6(ip) => public_v6(ip),
        url::Host::Domain(name) => public_name(name),
    }
}

fn public_v4(ip: std::net::Ipv4Addr) -> bool {
    let [a, b, c, _] = ip.octets();
    !(ip.is_unspecified()
        || ip.is_loopback()
        || ip.is_private()
        || ip.is_link_local()
        || ip.is_broadcast()
        || ip.is_multicast()
        || ip.is_documentation()
        || a == 0
        // Shared address space (CGNAT), IETF protocol assignments, benchmarking, reserved.
        || a == 100 && (64..128).contains(&b)
        || a == 192 && b == 0 && c == 0
        || a == 198 && (b == 18 || b == 19)
        || a >= 240)
}

fn public_v6(ip: std::net::Ipv6Addr) -> bool {
    let s = ip.segments();
    let embedded = |hi: u16, lo: u16| {
        public_v4(std::net::Ipv4Addr::from(
            (u32::from(hi) << 16) | u32::from(lo),
        ))
    };
    if let Some(v4) = ip.to_ipv4_mapped() {
        return public_v4(v4);
    }
    match s {
        // NAT64 well-known prefix and 6to4 carry an IPv4 destination.
        [0x64, 0xff9b, 0, 0, 0, 0, hi, lo] => embedded(hi, lo),
        [0x2002, hi, lo, ..] => embedded(hi, lo),
        // IPv4-compatible (deprecated) addresses, including :: and ::1.
        [0, 0, 0, 0, 0, 0, _, _] => false,
        _ => {
            !(ip.is_multicast()
                || s[0] & 0xfe00 == 0xfc00
                || s[0] & 0xffc0 == 0xfe80
                || s[0] & 0xffc0 == 0xfec0
                || s[0] == 0x2001 && s[1] == 0x0db8)
        }
    }
}

fn public_name(name: &str) -> bool {
    let name = name.strip_suffix('.').unwrap_or(name).to_ascii_lowercase();
    let Some((_, tld)) = name.rsplit_once('.') else {
        // Single-label names resolve through local search domains.
        return false;
    };
    !name.split('.').any(str::is_empty)
        && !matches!(
            tld,
            "localhost"
                | "local"
                | "localdomain"
                | "internal"
                | "intranet"
                | "private"
                | "corp"
                | "home"
                | "lan"
                | "arpa"
                | "test"
                | "invalid"
                | "example"
                | "onion"
        )
}

/// Domain filters of a provider fetch tool may only widen access to public hosts.
pub(super) fn public_domains(tool: &Value) -> bool {
    tool.get("allowed_domains")
        .filter(|v| !v.is_null())
        .is_none_or(|domains| {
            domains.as_array().is_some_and(|domains| {
                domains.iter().all(|d| {
                    d.as_str()
                        .map(|d| d.split(['/', '?', '#']).next().unwrap_or_default())
                        .and_then(|d| url::Url::parse(&format!("https://{d}")).ok())
                        .is_some_and(|u| u.host().is_some_and(public_host) && u.port().is_none())
                })
            })
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

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn remote_tool_urls_require_a_public_https_host() {
        for url in [
            "https://mcp.example.com/sse",
            "https://203.0.114.7/mcp",
            "https://[2606:4700::1111]/mcp",
            "https://tools.example.org.:8443/v1",
        ] {
            assert!(https_url(&json!(url)), "{url}");
        }
        for url in [
            "http://mcp.example.com/sse",
            "https://user@mcp.example.com/",
            "https://localhost/mcp",
            "https://api.localhost/mcp",
            "https://metadata.google.internal/",
            "https://printer.local/",
            "https://intranet/",
            "https://127.0.0.1/",
            "https://127.1/",
            "https://0x7f000001/",
            "https://10.1.2.3/",
            "https://172.16.0.9/",
            "https://192.168.1.1/",
            "https://169.254.169.254/latest/meta-data",
            "https://100.64.0.1/",
            "https://0.0.0.0/",
            "https://[::1]/",
            "https://[::]/",
            "https://[fe80::1]/",
            "https://[fd00::1]/",
            "https://[::ffff:10.0.0.1]/",
            "https://[64:ff9b::a9fe:a9fe]/",
            "https://[2002:c0a8:0101::1]/",
            "https://[ff02::1]/",
        ] {
            assert!(!https_url(&json!(url)), "{url}");
        }
    }

    #[test]
    fn fetch_domain_filters_cannot_allow_private_hosts() {
        assert!(public_domains(&json!({"type":"web_fetch_20250910"})));
        assert!(public_domains(
            &json!({"allowed_domains":["example.com","docs.example.com/guide"]})
        ));
        assert!(public_domains(
            &json!({"blocked_domains":["localhost","10.0.0.1"]})
        ));
        for domains in [
            json!(["localhost"]),
            json!(["169.254.169.254"]),
            json!(["svc.internal"]),
            json!(["example.com:8080"]),
            json!("example.com"),
            json!([1]),
        ] {
            assert!(!public_domains(&json!({ "allowed_domains": domains })));
        }
    }
}
