//! Claude document and caller-supplied search result citations on the Converse wire.
use super::Result;
use crate::protocol::{
    ProtocolError,
    convert::{array, fields, text},
};
use serde_json::{Value, json};

struct Location {
    aws: &'static str,
    claude: &'static str,
    start: &'static str,
    end: &'static str,
}

const LOCATIONS: [Location; 4] = [
    Location {
        aws: "documentChar",
        claude: "char_location",
        start: "start_char_index",
        end: "end_char_index",
    },
    Location {
        aws: "documentPage",
        claude: "page_location",
        start: "start_page_number",
        end: "end_page_number",
    },
    Location {
        aws: "documentChunk",
        claude: "content_block_location",
        start: "start_block_index",
        end: "end_block_index",
    },
    Location {
        aws: "searchResultLocation",
        claude: "search_result_location",
        start: "start_block_index",
        end: "end_block_index",
    },
];

impl Location {
    fn keys(&self) -> (&'static str, &'static str, &'static str) {
        if self.aws == "searchResultLocation" {
            ("searchResultIndex", "search_result_index", "title")
        } else {
            ("documentIndex", "document_index", "document_title")
        }
    }
    fn range(&self, from: &Value, keys: [&str; 3]) -> Result<[u32; 3]> {
        let number = |k| {
            from.get(k)
                .and_then(Value::as_u64)
                .and_then(|v| u32::try_from(v).ok())
                .filter(|v| *v <= i32::MAX as u32)
                .ok_or(ProtocolError::Invalid("citation position"))
        };
        let [index, start, end] = keys;
        let values = [number(index)?, number(start)?, number(end)?];
        if values[1] >= values[2] || self.aws == "documentPage" && values[1] == 0 {
            return Err(ProtocolError::Invalid("citation range"));
        }
        Ok(values)
    }
}

pub(super) fn join_text(v: &Value) -> Result<String> {
    let mut joined = String::new();
    for p in array(v, "citation text segments")? {
        fields(p, &["text"])?;
        joined.push_str(text(p, "text")?);
    }
    Ok(joined)
}

pub(super) fn to_messages(v: &Value) -> Result<Value> {
    fields(v, &["location", "source", "sourceContent", "title"])?;
    let loc = v["location"]
        .as_object()
        .filter(|v| v.len() == 1)
        .ok_or(ProtocolError::Invalid("citation location union"))?;
    let (key, pos) = loc.iter().next().expect("one location");
    let kind = LOCATIONS
        .iter()
        .find(|k| k.aws == key)
        .ok_or(ProtocolError::Unsupported("Bedrock citation location"))?;
    let (aws_index, index, title) = kind.keys();
    fields(pos, &[aws_index, "start", "end"])?;
    let [n, start, end] = kind.range(pos, [aws_index, "start", "end"])?;
    let name = v
        .get("title")
        .filter(|s| !s.is_null())
        .map(|_| text(v, "title"))
        .transpose()?;
    let mut out = json!({"type":kind.claude,"cited_text":join_text(&v["sourceContent"])?,index:n,title:name,kind.start:start,kind.end:end});
    if kind.aws == "searchResultLocation" {
        out["source"] = text(v, "source")?.into();
    } else if v.get("source").is_some_and(|s| !s.is_null()) {
        return Err(ProtocolError::Unsupported("document citation source"));
    }
    Ok(out)
}

fn to_converse(v: &Value) -> Result<Value> {
    let kind = LOCATIONS
        .iter()
        .find(|k| v["type"] == k.claude)
        .ok_or(ProtocolError::Unsupported("Messages citation location"))?;
    let (aws_index, index, title) = kind.keys();
    fields(
        v,
        &[
            "type",
            "cited_text",
            index,
            title,
            kind.start,
            kind.end,
            "source",
            "file_id",
        ],
    )?;
    if v.get("file_id").is_some_and(|f| !f.is_null()) {
        return Err(ProtocolError::Unsupported("provider file citation"));
    }
    let [n, start, end] = kind.range(v, [index, kind.start, kind.end])?;
    let mut out = json!({"location":{kind.aws:{aws_index:n,"start":start,"end":end}},
        "sourceContent":[{"text":text(v,"cited_text")?}]});
    if v.get(title).is_some_and(|v| !v.is_null()) {
        out["title"] = text(v, title)?.into();
    }
    if kind.aws == "searchResultLocation" {
        out["source"] = text(v, "source")?.into();
    } else if v.get("source").is_some() {
        return Err(ProtocolError::Unsupported("document citation source"));
    }
    Ok(out)
}

pub(super) fn history(block: &Value) -> Result<Value> {
    fields(block, &["type", "text", "citations"])?;
    let data = text(block, "text")?;
    let Some(citations) = block.get("citations").filter(|v| !v.is_null()) else {
        return Ok(json!({"text":data}));
    };
    let citations = array(citations, "message citations")?
        .iter()
        .map(to_converse)
        .collect::<Result<Vec<_>>>()?;
    if citations.is_empty() {
        return Ok(json!({"text":data}));
    }
    Ok(json!({"citationsContent":{"content":[{"text":data}],"citations":citations}}))
}

pub(super) fn block(value: &Value) -> Result<Value> {
    fields(value, &["content", "citations"])?;
    let mut block = json!({"type":"text","text":join_text(&value["content"])?});
    if let Some(cites) = value.get("citations") {
        block["citations"] = array(cites, "Bedrock citations")?
            .iter()
            .map(to_messages)
            .collect::<Result<Vec<_>>>()?
            .into();
    }
    Ok(block)
}

pub(crate) fn config(src: &Value, target: &mut Value) -> Result<()> {
    if let Some(c) = src.get("citations").filter(|v| !v.is_null()) {
        fields(c, &["enabled"])?;
        let enabled = c["enabled"]
            .as_bool()
            .ok_or(ProtocolError::Invalid("document citations"))?;
        target["citations"] = json!({"enabled":enabled});
    }
    Ok(())
}

pub(super) fn search_result(block: &Value) -> Result<Value> {
    fields(block, &["type", "source", "title", "content", "citations"])?;
    let mut data = json!({"source":text(block,"source")?,"title":text(block,"title")?,
        "content":content_blocks(&block["content"])?});
    config(block, &mut data)?;
    Ok(json!({"searchResult":data}))
}

pub(crate) fn content_blocks(value: &Value) -> Result<Vec<Value>> {
    array(value, "document content")?
        .iter()
        .map(|b| {
            fields(b, &["type", "text"])?;
            if b["type"] != "text" {
                return Err(ProtocolError::Unsupported("document content type"));
            }
            Ok(json!({"text":text(b,"text")?}))
        })
        .collect()
}

/// Bedrock can split the source excerpt from its location and title. Keep the
/// open citation until another location starts or the text block closes.
#[derive(Default)]
pub(super) struct Delta {
    location: Option<Value>,
    title: Option<String>,
    source: Option<String>,
    quote: Option<String>,
    bytes: usize,
}

impl Delta {
    pub fn push(
        &mut self,
        part: &Value,
        retained: &mut usize,
        limit: usize,
    ) -> Result<Option<Value>> {
        let map = fields(part, &["location", "title", "source", "sourceContent"])?;
        if map.is_empty() {
            return Err(ProtocolError::Invalid("empty citation fragment"));
        }
        let title = part.get("title").map(|_| text(part, "title")).transpose()?;
        let source = part
            .get("source")
            .map(|_| text(part, "source"))
            .transpose()?;
        let new_citation = self.location.is_some()
            && (part.get("location").is_some()
                || self
                    .title
                    .as_deref()
                    .zip(title)
                    .is_some_and(|(a, b)| a != b)
                || self
                    .source
                    .as_deref()
                    .zip(source)
                    .is_some_and(|(a, b)| a != b));
        let ready = if new_citation {
            self.finish(retained)?
        } else {
            None
        };
        let size = part.to_string().len();
        if size > limit.saturating_sub(*retained) {
            return Err(ProtocolError::EventTooLarge);
        }
        *retained += size;
        self.bytes += size;
        if let Some(location) = part.get("location") {
            self.location = Some(location.clone());
        }
        for (slot, value) in [(&mut self.title, title), (&mut self.source, source)] {
            if let Some(value) = value {
                if slot.as_deref().is_some_and(|prior| prior != value) {
                    return Err(ProtocolError::Invalid("ambiguous citation fragments"));
                }
                *slot = Some(value.to_owned());
            }
        }
        if let Some(content) = part.get("sourceContent") {
            self.quote
                .get_or_insert_with(String::new)
                .push_str(&join_text(content)?);
        }
        Ok(ready)
    }
    pub fn finish(&mut self, retained: &mut usize) -> Result<Option<Value>> {
        if self.bytes == 0 {
            return Ok(None);
        }
        let pending = std::mem::take(self);
        *retained -= pending.bytes;
        let quote = pending
            .quote
            .ok_or(ProtocolError::Invalid("missing cited text"))?;
        let v = json!({"location":pending.location,"title":pending.title,"source":pending.source,"sourceContent":[{"text":quote}]});
        to_messages(&v).map(Some)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_ranges_roundtrip_without_converting_document_coordinates_to_answer_offsets() {
        for (key, pos) in [
            ("documentChar", json!({"documentIndex":2,"start":0,"end":5})),
            ("documentPage", json!({"documentIndex":0,"start":1,"end":2})),
            (
                "documentChunk",
                json!({"documentIndex":1,"start":2,"end":3}),
            ),
            (
                "searchResultLocation",
                json!({"searchResultIndex":0,"start":1,"end":3}),
            ),
        ] {
            let mut aws =
                json!({"location":{key:pos},"sourceContent":[{"text":"quote"}],"title":"Source"});
            if key == "searchResultLocation" {
                aws["source"] = "result-1".into();
            }
            let expected = to_messages(&aws).unwrap();
            assert_eq!(to_converse(&expected).unwrap(), aws);
        }
        for loc in [
            json!({"documentPage":{"documentIndex":0,"start":0,"end":1}}),
            json!({"documentChar":{"documentIndex":-1,"start":0,"end":1}}),
            json!({"documentChunk":{"documentIndex":0,"start":1,"end":1}}),
            json!({"web":{"url":"https://example.org"}}),
        ] {
            assert!(
                to_messages(&json!({"location":loc,"sourceContent":[{"text":"source"}]})).is_err()
            );
        }
    }

    #[test]
    fn incomplete_or_ambiguous_citations_never_emit_a_completed_citation() {
        let loc = json!({"location":{"documentChar":{"documentIndex":0,"start":0,"end":4}}});
        let mut retained = 0;
        let mut pending = Delta::default();
        pending.push(&loc, &mut retained, 2048).unwrap();
        assert!(pending.finish(&mut retained).is_err());
        assert_eq!(retained, 0);
        pending
            .push(&json!({"title":"A"}), &mut retained, 2048)
            .unwrap();
        assert!(
            pending
                .push(&json!({"title":"B"}), &mut retained, 2048)
                .is_err()
        );
        assert!(
            pending
                .push(
                    &json!({"sourceContent":[{"text":"x".repeat(1024)}]}),
                    &mut retained,
                    128
                )
                .is_err()
        );
    }
}
