//! Portable user images and documents. The relay never downloads a URL.
use super::{
    ProtocolError,
    convert::{fields, text},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};

fn image_format(mime: &str) -> Result<&str, ProtocolError> {
    match mime {
        "image/jpeg" => Ok("jpeg"),
        "image/png" => Ok("png"),
        "image/webp" => Ok("webp"),
        "image/gif" => Ok("gif"),
        _ => Err(ProtocolError::Unsupported("image media type")),
    }
}

pub fn from_messages(block: &Value) -> Result<Value, ProtocolError> {
    fields(block, &["type", "source", "title"])?;
    let source = &block["source"];
    let document = block["type"] == "document";
    let target = if document {
        "input_file"
    } else {
        "input_image"
    };
    if source["type"] == "file" {
        fields(source, &["type", "file_id"])?;
        if block.get("title").is_some_and(|v| !v.is_null()) {
            return Err(ProtocolError::Unsupported("file reference title"));
        }
        let id = text(source, "file_id")?;
        return Ok(if document {
            json!({"type":"input_file","file_id":id})
        } else {
            json!({"type":"input_image","file_id":id,"detail":"auto"})
        });
    }
    if document && source["type"] == "text" {
        fields(source, &["type", "media_type", "data"])?;
        if source["media_type"] != "text/plain" {
            return Err(ProtocolError::Unsupported("text document media type"));
        }
        let encoded = STANDARD.encode(text(source, "data")?.as_bytes());
        return Ok(
            json!({"type":"input_file", "file_data":format!("data:text/plain;base64,{encoded}"),
            "filename": block.get("title").cloned().unwrap_or(json!("document.txt"))}),
        );
    }
    let url = match text(source, "type")? {
        "base64" => {
            fields(source, &["type", "media_type", "data"])?;
            let mime = text(source, "media_type")?;
            if document {
                if mime != "application/pdf" {
                    return Err(ProtocolError::Unsupported("document media type"));
                }
            } else {
                image_format(mime)?;
            }
            format!("data:{mime};base64,{}", text(source, "data")?)
        }
        "url" => {
            fields(source, &["type", "url"])?;
            let url = text(source, "url")?;
            if !url.starts_with("https://") {
                return Err(ProtocolError::Unsupported("media URL"));
            }
            url.to_owned()
        }
        _ => return Err(ProtocolError::Unsupported("media source")),
    };
    if document {
        let mut out = json!({"type":target});
        if source["type"] == "base64" {
            out["file_data"] = url.into();
            out["filename"] = block.get("title").cloned().unwrap_or(json!("document.pdf"));
        } else {
            if block.get("title").is_some() {
                return Err(ProtocolError::Unsupported("remote document title"));
            }
            out["file_url"] = url.into();
        }
        Ok(out)
    } else {
        if block.get("title").is_some() {
            return Err(ProtocolError::Unsupported("image title"));
        }
        Ok(json!({"type":target,"image_url":url,"detail":"auto"}))
    }
}

pub fn to_messages(part: &Value) -> Result<Value, ProtocolError> {
    let document = part["type"] == "input_file";
    if part.get("file_id").is_some_and(|v| !v.is_null()) {
        fields(
            part,
            if document {
                &["type", "file_id", "citations"][..]
            } else {
                &["type", "file_id", "detail"][..]
            },
        )?;
        if part.get("detail").is_some_and(|v| v != "auto") {
            return Err(ProtocolError::Unsupported("image detail"));
        }
        let mut out = json!({"type":if document {"document"}else{"image"},"source":{"type":"file","file_id":text(part,"file_id")?}});
        if document {
            crate::bedrock::citations::config(part, &mut out)?;
        }
        return Ok(out);
    }
    let (value, kind) = if document {
        fields(
            part,
            &["type", "file_url", "file_data", "filename", "citations"],
        )?;
        if part.get("file_data").is_some() == part.get("file_url").is_some() {
            return Err(ProtocolError::Invalid("file source"));
        }
        (
            part.get("file_data")
                .or_else(|| part.get("file_url"))
                .and_then(Value::as_str)
                .ok_or(ProtocolError::Invalid("file source"))?,
            "document",
        )
    } else {
        fields(part, &["type", "image_url", "detail"])?;
        if part.get("detail").is_some_and(|v| v != "auto") {
            return Err(ProtocolError::Unsupported("image detail"));
        }
        (text(part, "image_url")?, "image")
    };
    let source = if let Some(data) = value.strip_prefix("data:") {
        let (mime, b64) = data
            .split_once(";base64,")
            .ok_or(ProtocolError::Invalid("media data URL"))?;
        if document && mime == "text/plain" {
            let decoded = STANDARD
                .decode(b64)
                .map_err(|_| ProtocolError::Invalid("text file base64"))?;
            let data = String::from_utf8(decoded)
                .map_err(|_| ProtocolError::Invalid("text file encoding"))?;
            json!({"type":"text","media_type":mime,"data":data})
        } else {
            if document && mime != "application/pdf" {
                return Err(ProtocolError::Unsupported("document media type"));
            }
            if !document {
                image_format(mime)?;
            }
            json!({"type":"base64","media_type":mime,"data":b64})
        }
    } else if value.starts_with("https://") {
        json!({"type":"url","url":value})
    } else {
        return Err(ProtocolError::Unsupported("media source"));
    };
    let mut out = json!({"type":kind,"source":source});
    if let Some(title) = part.get("filename") {
        out["title"] = title.clone();
    }
    if document {
        crate::bedrock::citations::config(part, &mut out)?;
    }
    Ok(out)
}

fn bedrock_file_format(mime: &str) -> Result<&'static str, ProtocolError> {
    match mime {
        "video/mp4" => Ok("mp4"),
        "video/webm" => Ok("webm"),
        "video/quicktime" => Ok("mov"),
        "video/x-matroska" => Ok("mkv"),
        "video/x-flv" => Ok("flv"),
        "video/mpeg" => Ok("mpeg"),
        "video/mpg" => Ok("mpg"),
        "video/x-ms-wmv" => Ok("wmv"),
        "video/3gpp" => Ok("three_gp"),
        "application/pdf" => Ok("pdf"),
        "text/csv" => Ok("csv"),
        "text/plain" => Ok("txt"),
        "text/markdown" => Ok("md"),
        "text/html" => Ok("html"),
        "application/msword" => Ok("doc"),
        "application/vnd.ms-excel" => Ok("xls"),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document" => Ok("docx"),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" => Ok("xlsx"),
        _ => Err(ProtocolError::Unsupported("Bedrock file media type")),
    }
}

/// Responses and Chat carry inline media files as data URLs. Only the Converse
/// translator accepts the additional document and video formats here.
pub(crate) fn converse_file(part: &Value) -> Result<Value, ProtocolError> {
    if part["type"] != "input_file" {
        return to_messages(part);
    }
    fields(part, &["type", "filename", "file_data", "citations"])?;
    let (mime, data) = text(part, "file_data")?
        .strip_prefix("data:")
        .and_then(|s| s.split_once(";base64,"))
        .ok_or(ProtocolError::Invalid("inline file"))?;
    bedrock_file_format(mime)?;
    let video = mime.starts_with("video/");
    let mut out = json!({"type":if video {"video"} else {"document"}, "source":{"type":"base64","media_type":mime,"data":data}});
    if let Some(name) = part.get("filename") {
        let name = name.as_str().ok_or(ProtocolError::Invalid("file name"))?;
        if !video {
            out["title"] = name.into();
        }
    }
    if video && part.get("citations").is_some() {
        return Err(ProtocolError::Unsupported("video citations"));
    }
    crate::bedrock::citations::config(part, &mut out)?;
    Ok(out)
}

fn document_name(block: &Value) -> Result<String, ProtocolError> {
    let Some(name) = block.get("title") else {
        return Ok("document".into());
    };
    let name = name
        .as_str()
        .ok_or(ProtocolError::Invalid("document title"))?;
    let name = name
        .split(|c: char| !c.is_alphanumeric() && !matches!(c, '-' | '(' | ')' | '[' | ']'))
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    if name.is_empty() || name.chars().count() > 200 {
        return Err(ProtocolError::Invalid("document title"));
    }
    Ok(name)
}

pub fn bedrock(block: &Value) -> Result<Value, ProtocolError> {
    let source = &block["source"];
    if block["type"] == "document" {
        fields(block, &["type", "source", "title", "context", "citations"])?;
        let mut doc = json!({"name":document_name(block)?});
        match text(source, "type")? {
            "text" => {
                fields(source, &["type", "media_type", "data"])?;
                if source["media_type"] != "text/plain" {
                    return Err(ProtocolError::Unsupported("text document media type"));
                }
                doc["format"] = "txt".into();
                doc["source"] = json!({"text":text(source,"data")?});
            }
            "content" => {
                fields(source, &["type", "content"])?;
                doc["source"] = json!({"content":crate::bedrock::citations::content_blocks(&source["content"])?});
            }
            "base64" => {
                fields(source, &["type", "media_type", "data"])?;
                let mime = text(source, "media_type")?;
                let data = text(source, "data")?;
                if mime.starts_with("video/") || data.is_empty() {
                    return Err(ProtocolError::Invalid("document source"));
                }
                doc["format"] = bedrock_file_format(mime)?.into();
                doc["source"] = json!({"bytes":data});
            }
            _ => return Err(ProtocolError::Unsupported("inline document required")),
        }
        if block.get("context").is_some_and(|c| !c.is_null()) {
            doc["context"] = text(block, "context")?.into();
        }
        crate::bedrock::citations::config(block, &mut doc)?;
        return Ok(json!({"document":doc}));
    }
    if source["type"] != "base64" {
        return Err(ProtocolError::Unsupported("Bedrock inline media required"));
    }
    fields(source, &["type", "media_type", "data"])?;
    let mime = text(source, "media_type")?;
    let data = text(source, "data")?;
    if data.is_empty() {
        return Err(ProtocolError::Invalid("empty media"));
    }
    match block["type"].as_str() {
        Some("image") => {
            fields(block, &["type", "source"])?;
            Ok(json!({"image":{"format":image_format(mime)?,"source":{"bytes":data}}}))
        }
        Some("video") if mime.starts_with("video/") => {
            fields(block, &["type", "source"])?;
            if data.len() >= 25_000_000 || STANDARD.decode(data).is_err() {
                return Err(ProtocolError::Invalid("inline video bytes"));
            }
            Ok(json!({"video":{"format":bedrock_file_format(mime)?,"source":{"bytes":data}}}))
        }
        _ => Err(ProtocolError::Unsupported("Bedrock media block")),
    }
}
