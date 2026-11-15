//! Preserve source positions in their own coordinate system. The authenticated
//! continuation also retains vendor-only references that must not cross accounts.
use super::invalid;
use crate::protocol::{
    ProtocolError,
    convert::{Result, array, fields, text},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub(crate) fn annotated(item: &Value) -> bool {
    item["type"] == "message"
        && item["content"].as_array().is_some_and(|parts| {
            parts
                .iter()
                .any(|p| p["annotations"].as_array().is_some_and(|a| !a.is_empty()))
        })
}
pub(crate) fn file_handle(v: &Value, prefix: &str) -> Result<uuid::Uuid> {
    v.as_str()
        .and_then(|s| s.strip_prefix(prefix))
        .and_then(|s| s.parse().ok())
        .ok_or_else(invalid)
}

pub(crate) fn resource_annotations(v: &Value) -> bool {
    v["content"]
        .as_array()
        .into_iter()
        .flatten()
        .flat_map(|p| p["annotations"].as_array().into_iter().flatten())
        .any(|a| {
            matches!(
                a["type"].as_str(),
                Some("file_citation" | "file_path" | "container_file_citation")
            )
        })
}

fn file_annotation(a: &Value, length: u64) -> Result<()> {
    match a["type"].as_str() {
        Some("file_citation" | "file_path") => {
            fields(a, &["type", "file_id", "filename", "index"])?;
            file_handle(&a["file_id"], "file_nx_")?;
            if offset(a, "index")? > length {
                return Err(invalid());
            }
            if a["type"] == "file_citation" {
                text(a, "filename")?;
            }
        }
        Some("container_file_citation") => {
            fields(
                a,
                &[
                    "type",
                    "container_id",
                    "file_id",
                    "filename",
                    "start_index",
                    "end_index",
                ],
            )?;
            file_handle(&a["container_id"], "cntr_nx_")?;
            file_handle(&a["file_id"], "cfile_nx_")?;
            text(a, "filename")?;
            if offset(a, "start_index")? > offset(a, "end_index")?
                || offset(a, "end_index")? > length
            {
                return Err(invalid());
            }
        }
        _ => return Err(ProtocolError::Unsupported("file annotation")),
    }
    Ok(())
}
fn http_url(s: &str) -> bool {
    url::Url::parse(s).is_ok_and(|u| matches!(u.scheme(), "http" | "https") && u.host().is_some())
}
fn offset(v: &Value, k: &str) -> Result<u64> {
    v[k].as_u64().ok_or_else(invalid)
}

fn public_source(c: &Value) -> Result<Value> {
    let kind = text(c, "type")?;
    let (index, title, start, end) = match kind {
        "char_location" => (
            "document_index",
            "document_title",
            "start_char_index",
            "end_char_index",
        ),
        "page_location" => (
            "document_index",
            "document_title",
            "start_page_number",
            "end_page_number",
        ),
        "content_block_location" => (
            "document_index",
            "document_title",
            "start_block_index",
            "end_block_index",
        ),
        "search_result_location" => (
            "search_result_index",
            "title",
            "start_block_index",
            "end_block_index",
        ),
        "web_search_result_location" => {
            fields(
                c,
                &["type", "url", "title", "cited_text", "encrypted_index"],
            )?;
            if !http_url(text(c, "url")?) || text(c, "encrypted_index")?.is_empty() {
                return Err(invalid());
            }
            text(c, "cited_text")?;
            if c.get("title")
                .is_some_and(|v| !v.is_null() && !v.is_string())
            {
                return Err(invalid());
            }
            let mut public = c.clone();
            public.as_object_mut().unwrap().remove("encrypted_index");
            return Ok(public);
        }
        _ => return Err(ProtocolError::Unsupported("Messages citation type")),
    };
    fields(
        c,
        &[
            "type",
            "cited_text",
            title,
            index,
            start,
            end,
            "source",
            "file_id",
        ],
    )?;
    if let Some(fid) = c.get("file_id").filter(|v| !v.is_null()) {
        file_handle(fid, "file_nx_")?;
    }
    offset(c, index)?;
    if offset(c, start)? >= offset(c, end)? || kind == "page_location" && offset(c, start)? == 0 {
        return Err(invalid());
    }
    text(c, "cited_text")?;
    if c.get(title).is_some_and(|v| !v.is_null() && !v.is_string()) {
        return Err(invalid());
    }
    if kind == "search_result_location" {
        text(c, "source")?;
    } else if c.get("source").is_some_and(|v| !v.is_null()) {
        return Err(invalid());
    }
    Ok(c.clone())
}
pub(crate) fn to_responses(v: &Value, id: &str, token: &str) -> Result<Value> {
    fields(v, &["type", "text", "citations"])?;
    let body = text(v, "text")?;
    let end = body.chars().count();
    let mut annotations = Vec::new();
    let mut sources = Vec::new();
    for c in array(&v["citations"], "citations")? {
        let public = public_source(c)?;
        let url = c
            .get("url")
            .or_else(|| c.get("source"))
            .and_then(Value::as_str)
            .filter(|s| http_url(s));
        if let Some((url, title)) = url.zip(c["title"].as_str()) {
            annotations.push(json!({"type":"url_citation","url":url,"title":title,"start_index":0,"end_index":end}));
        }
        sources.push(public);
    }
    Ok(
        json!({"type":"message","id":id,"role":"assistant","status":"completed",
        "content":[{"type":"output_text","text":body,"annotations":annotations}],
        "nexus_citations":sources,"nexus_history":token}),
    )
}
pub(crate) fn part_text(part: &Value) -> Result<&str> {
    if part["type"] == "refusal" {
        fields(part, &["type", "refusal"])?;
        return text(part, "refusal");
    }
    fields(part, &["type", "text", "annotations", "logprobs"])?;
    if part["type"] != "output_text"
        || part
            .get("logprobs")
            .is_some_and(|v| !v.is_null() && v.as_array().is_none_or(|a| !a.is_empty()))
    {
        return Err(ProtocolError::Unsupported("Responses output part"));
    }
    text(part, "text")
}
pub(crate) fn to_messages(v: &Value, token: &str) -> Result<Vec<Value>> {
    fields(
        v,
        &[
            "type", "id", "role", "content", "status", "phase", "quality", "size",
        ],
    )?;
    if v["role"] != "assistant"
        || ["quality", "size"]
            .iter()
            .any(|k| v.get(*k).is_some_and(|x| !x.is_null() && x != ""))
    {
        return Err(invalid());
    }
    let group = Sha256::digest(token.as_bytes());
    let mut ordinal = 0;
    let files = resource_annotations(v);
    let mut file_notes = Vec::new();
    let mut blocks = Vec::new();
    for (index, part) in array(&v["content"], "output content")?.iter().enumerate() {
        let value = part_text(part)?;
        let length = value.chars().count() as u64;
        let mut block = json!({"type":"text","text":value});
        let mut citations = Vec::new();
        if let Some(entries) = part.get("annotations").filter(|v| !v.is_null()) {
            for a in array(entries, "annotations")? {
                if a["type"] != "url_citation" {
                    file_annotation(a, length)?;
                    file_notes.push(json!({"content_index":index,"annotation":a}));
                    continue;
                }
                fields(a, &["type", "url", "title", "start_index", "end_index"])?;
                let start = offset(a, "start_index")?;
                let end = offset(a, "end_index")?;
                if a["type"] != "url_citation"
                    || start > end
                    || end > length
                    || !http_url(text(a, "url")?)
                {
                    return Err(ProtocolError::Unsupported("unmapped URL citation"));
                }
                let reference = if ordinal == 0 && !files {
                    token.to_owned()
                } else {
                    format!("nxref1.{group:x}.{ordinal}")
                };
                // Responses supplies an answer span and a URL, but no source quotation.
                citations.push(json!({"type":"web_search_result_location","url":a["url"],"title":text(a,"title")?,"cited_text":"",
                    "encrypted_index":reference,"nexus_start_index":start,"nexus_end_index":end}));
                ordinal += 1;
            }
        }
        if !citations.is_empty() {
            block["citations"] = citations.into();
        }
        blocks.push(block);
    }
    if files {
        blocks.push(json!({"type":"redacted_thinking","data":token,"nexus_item_type":"annotations","nexus_annotations":file_notes}));
    } else if ordinal == 0 {
        return Err(invalid());
    }
    Ok(blocks)
}

/// Document citation file IDs in native Claude JSON or SSE. Do not traverse tool arguments.
pub(crate) fn claude_files(value: &Value) -> Vec<String> {
    fn visit(v: &Value, path: &str, refs: &mut Vec<String>) {
        if matches!(
            v["type"].as_str(),
            Some("char_location" | "page_location" | "content_block_location")
        ) && v.get("file_id").is_some_and(|id| !id.is_null())
        {
            refs.push(format!("{path}/file_id"));
        }
        for field in ["message", "content_block", "delta", "citation"] {
            if let Some(sub) = v.get(field).filter(|v| v.is_object()) {
                visit(sub, &format!("{path}/{field}"), refs);
            }
        }
        for field in ["content", "citations"] {
            for (i, sub) in v[field].as_array().into_iter().flatten().enumerate() {
                visit(sub, &format!("{path}/{field}/{i}"), refs);
            }
        }
    }
    let mut refs = Vec::new();
    visit(value, "", &mut refs);
    refs
}
