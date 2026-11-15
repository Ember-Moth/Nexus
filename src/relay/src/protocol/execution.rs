//! Claude's execution count is a call count. Duration is a separate supplier meter.
use super::ProtocolError;
use serde::Serialize;
use serde_json::Value;
use uuid::Uuid;

#[derive(Debug, Default, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ExecutionMode {
    #[default]
    None,
    Bundled,
    Metered,
    Preloaded,
}
impl ExecutionMode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Bundled => "bundled",
            Self::Metered => "metered",
            Self::Preloaded => "preloaded",
        }
    }
}

#[derive(Debug)]
pub(crate) struct Execution {
    pub mode: ExecutionMode,
    pub container: Option<Uuid>,
    pub implicit: bool,
}
pub(crate) fn code_tool(tool: &Value) -> bool {
    matches!(
        tool["type"].as_str(),
        Some(
            "code_execution_20250522"
                | "code_execution_20250825"
                | "code_execution_20260120"
                | "code_execution_20260521"
        )
    ) && tool["name"] == "code_execution"
}
pub(crate) fn dynamic_fetch(t: &Value) -> bool {
    matches!(
        t["type"].as_str(),
        Some("web_fetch_20260209" | "web_fetch_20260309" | "web_fetch_20260318")
    ) && t["name"] == "web_fetch"
}
impl Execution {
    pub fn messages(body: &Value) -> Result<Option<Self>, ProtocolError> {
        let tools = body["tools"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or_default();
        let declared = tools.iter().filter(|t| code_tool(t)).count();
        if declared > 1 {
            return Err(ProtocolError::Invalid("duplicate code execution tool"));
        }
        let bundled = tools.iter().any(|t| {
            dynamic_fetch(t)
                || (matches!(
                    t["type"].as_str(),
                    Some("web_search_20260209" | "web_search_20260318")
                ) && t["name"] == "web_search")
        });
        let container = match body.get("container").filter(|v| !v.is_null()) {
            None => None,
            Some(v) => {
                let raw = if let Some(obj) = v.as_object() {
                    if obj.len() != 1 {
                        return Err(ProtocolError::Unsupported("container configuration"));
                    }
                    v["id"].as_str()
                } else {
                    v.as_str()
                };
                Some(
                    raw.and_then(|s| s.strip_prefix("cntr_nx_"))
                        .and_then(|s| s.parse().ok())
                        .ok_or(ProtocolError::Invalid("owned container ID"))?,
                )
            }
        };
        let preload = body["messages"].as_array().into_iter().flatten().any(|m| {
            m["content"].as_array().into_iter().flatten().any(|b| {
                b["type"] == "container_upload"
                    || declared > 0
                        && matches!(b["type"].as_str(), Some("document" | "image"))
                        && b["source"]["type"] == "file"
            })
        });
        if !bundled && declared == 0 && container.is_none() && !preload {
            return Ok(None);
        }
        Ok(Some(Self {
            container,
            implicit: bundled && declared == 0 && container.is_none() && !preload,
            mode: if bundled {
                ExecutionMode::Bundled
            } else if preload {
                ExecutionMode::Preloaded
            } else {
                ExecutionMode::Metered
            },
        }))
    }
}

// Only the supplier's result union carries newly created files. Text, arbitrary
// tool arguments and client tool results may contain arbitrary customer JSON.
pub(crate) fn result_files(block: &Value, root: &str, out: &mut Vec<String>) {
    if !matches!(
        block["type"].as_str(),
        Some("bash_code_execution_tool_result" | "code_execution_tool_result")
    ) {
        return;
    }
    let result = &block["content"];
    if !matches!(
        result["type"].as_str(),
        Some("bash_code_execution_result" | "code_execution_result")
    ) {
        return;
    }
    for (i, item) in result["content"]
        .as_array()
        .into_iter()
        .flatten()
        .enumerate()
    {
        if matches!(
            item["type"].as_str(),
            Some("bash_code_execution_output" | "code_execution_output")
        ) {
            out.push(format!("{root}/content/content/{i}/file_id"));
        }
    }
}

pub(crate) fn output_fields(value: &Value) -> (Vec<String>, Vec<String>) {
    let mut containers = Vec::new();
    let mut files = Vec::new();
    for (root, v) in [
        ("", value),
        ("/message", &value["message"]),
        ("/delta", &value["delta"]),
    ] {
        if (v["type"] == "message"
            || root == "/message" && value["type"] == "message_start"
            || root == "/delta" && value["type"] == "message_delta")
            && !v["container"].is_null()
        {
            containers.push(format!("{root}/container/id"));
        }
        if v["type"] == "message" {
            for (i, block) in v["content"].as_array().into_iter().flatten().enumerate() {
                result_files(block, &format!("{root}/content/{i}"), &mut files);
            }
        }
    }
    if value["type"] == "content_block_start" {
        result_files(&value["content_block"], "/content_block", &mut files);
    }
    (containers, files)
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
pub enum MemoryTier {
    #[serde(rename = "1g")]
    One,
    #[serde(rename = "4g")]
    Four,
    #[serde(rename = "16g")]
    Sixteen,
    #[serde(rename = "64g")]
    SixtyFour,
}
impl MemoryTier {
    pub fn parse(value: &str) -> Result<Self, ProtocolError> {
        match value {
            "1g" => Ok(Self::One),
            "4g" => Ok(Self::Four),
            "16g" => Ok(Self::Sixteen),
            "64g" => Ok(Self::SixtyFour),
            _ => Err(ProtocolError::Invalid("container memory limit")),
        }
    }
    pub fn name(self) -> &'static str {
        match self {
            Self::One => "1g",
            Self::Four => "4g",
            Self::Sixteen => "16g",
            Self::SixtyFour => "64g",
        }
    }
    pub fn dimension(self) -> &'static str {
        match self {
            Self::One => "container_1g_minutes",
            Self::Four => "container_4g_minutes",
            Self::Sixteen => "container_16g_minutes",
            Self::SixtyFour => "container_64g_minutes",
        }
    }
}
#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
pub struct ContainerTarget {
    pub existing: Option<Uuid>,
    pub tier: MemoryTier,
    pub preloaded: bool,
}
#[derive(Debug)]
pub(crate) struct Interpreter {
    pub index: usize,
    pub target: ContainerTarget,
}
pub(crate) fn isolated(policy: Option<&Value>) -> bool {
    policy.is_none_or(|p| {
        p.is_null()
            || p.as_object()
                .is_some_and(|o| o.len() == 1 && p["type"] == "disabled")
    })
}
impl Interpreter {
    pub fn parse(index: usize, tool: &Value) -> Result<Self, ProtocolError> {
        super::convert::fields(tool, &["type", "container"])?;
        let container = &tool["container"];
        let (existing, tier) = if container.is_object() {
            super::convert::fields(
                container,
                &["type", "file_ids", "memory_limit", "network_policy"],
            )?;
            if container["type"] != "auto" || !isolated(container.get("network_policy")) {
                return Err(ProtocolError::Unsupported("container configuration"));
            }
            if container
                .get("file_ids")
                .is_some_and(|v| !v.is_null() && !v.is_array())
            {
                return Err(ProtocolError::Invalid("container files"));
            }
            (
                None,
                MemoryTier::parse(
                    container
                        .get("memory_limit")
                        .map_or(Some("1g"), Value::as_str)
                        .ok_or(ProtocolError::Invalid("memory limit"))?,
                )?,
            )
        } else {
            let id = container
                .as_str()
                .and_then(|s| s.strip_prefix("cntr_nx_"))
                .and_then(|s| s.parse().ok())
                .ok_or(ProtocolError::Invalid("owned container ID"))?;
            (Some(id), MemoryTier::One)
        };
        Ok(Self {
            index,
            target: ContainerTarget {
                existing,
                tier,
                preloaded: container["file_ids"]
                    .as_array()
                    .is_some_and(|v| !v.is_empty()),
            },
        })
    }
}

pub(crate) struct ContainerField {
    pub path: String,
    pub call: bool,
    pub completed: bool,
    pub file: Option<String>,
}
pub(crate) fn openai_fields(value: &Value) -> Vec<ContainerField> {
    fn visit(v: &Value, path: &str, refs: &mut Vec<ContainerField>) {
        let kind = v["type"].as_str();
        match kind {
            Some("code_interpreter_call") if !v["container_id"].is_null() => {
                refs.push(ContainerField {
                    path: format!("{path}/container_id"),
                    call: true,
                    completed: v["status"] == "completed",
                    file: None,
                })
            }
            Some("container_file_citation") => refs.push(ContainerField {
                path: format!("{path}/container_id"),
                call: false,
                completed: false,
                file: Some(format!("{path}/file_id")),
            }),
            Some("code_interpreter") if v["container"].is_string() => refs.push(ContainerField {
                path: format!("{path}/container"),
                call: false,
                completed: false,
                file: None,
            }),
            _ => {}
        }
        for k in ["response", "item", "part", "annotation"] {
            if let Some(sub) = v.get(k).filter(|s| s.is_object()) {
                visit(sub, &format!("{path}/{k}"), refs);
            }
        }
        for k in ["output", "tools", "content", "annotations", "data"] {
            for (i, sub) in v[k].as_array().into_iter().flatten().enumerate() {
                if sub.is_object() {
                    visit(sub, &format!("{path}/{k}/{i}"), refs);
                }
            }
        }
    }
    let mut refs = Vec::new();
    visit(value, "", &mut refs);
    refs.sort_by_key(|f| !f.call);
    refs
}
