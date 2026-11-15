use super::{position, unexpected};
use crate::protocol::{
    ProtocolError,
    convert::{Result, array, text},
    history::Writer,
};
use serde_json::{Value, json};

#[derive(Default)]
struct Piece {
    data: String,
    sent: usize,
    separator: bool,
    done: bool,
}
fn update(parts: &mut Vec<Piece>, index: usize, value: &str, full: bool, done: bool) -> Result<()> {
    if index > parts.len() || index >= 4096 {
        return unexpected();
    }
    if index == parts.len() {
        parts.push(Piece::default());
    }
    let p = &mut parts[index];
    let suffix = if full {
        value
            .strip_prefix(&p.data)
            .ok_or(ProtocolError::UnexpectedEvent)?
    } else {
        value
    };
    if p.done && !suffix.is_empty() {
        return unexpected();
    }
    p.data.push_str(suffix);
    p.done |= done;
    Ok(())
}
fn reconcile(parts: &mut Vec<Piece>, value: Option<&Value>, kind: &str, done: bool) -> Result<()> {
    let rows = match value.filter(|v| !v.is_null()) {
        Some(v) => array(v, "reasoning parts")?.as_slice(),
        None => &[],
    };
    if rows.len() < parts.len() {
        return unexpected();
    }
    for (p, v) in rows.iter().enumerate() {
        if v["type"] != kind {
            return unexpected();
        }
        update(parts, p, text(v, "text")?, true, done)?;
    }
    Ok(())
}

#[derive(Default)]
pub(super) struct PrivateResponse {
    hide_summary: bool,
    compaction: bool,
    summary: Vec<Piece>,
    private_content: Vec<Piece>,
    final_item: Option<(Value, Value)>,
    cursor: usize,
    started: bool,
}
impl PrivateResponse {
    pub fn begin(v: &Value, hide_summary: bool) -> Result<Self> {
        let mut state = Self {
            compaction: v["type"] == "compaction",
            hide_summary,
            ..Self::default()
        };
        reconcile(&mut state.summary, v.get("summary"), "summary_text", false)?;
        reconcile(
            &mut state.private_content,
            v.get("content"),
            "reasoning_text",
            false,
        )?;
        Ok(state)
    }
    pub fn event(&mut self, kind: &str, v: &Value) -> Result<()> {
        if self.compaction {
            return if kind == "response.compaction.compacting" {
                Ok(())
            } else {
                unexpected()
            };
        }
        match kind {
            "response.reasoning_summary_part.added" | "response.reasoning_summary_part.done" => {
                if v["part"]["type"] != "summary_text" {
                    return unexpected();
                }
                update(
                    &mut self.summary,
                    position(v, "summary_index")?,
                    text(&v["part"], "text")?,
                    true,
                    kind.ends_with(".done"),
                )
            }
            "response.reasoning_summary_text.delta"
            | "response.reasoning_summary_text.done"
            | "response.reasoning_text.delta"
            | "response.reasoning_text.done" => {
                let summary = kind.starts_with("response.reasoning_summary");
                let done = kind.ends_with(".done");
                update(
                    if summary {
                        &mut self.summary
                    } else {
                        &mut self.private_content
                    },
                    position(
                        v,
                        if summary {
                            "summary_index"
                        } else {
                            "content_index"
                        },
                    )?,
                    text(v, if done { "text" } else { "delta" })?,
                    done,
                    done,
                )
            }
            "response.content_part.added" | "response.content_part.done"
                if v["part"]["type"] == "reasoning_text" =>
            {
                update(
                    &mut self.private_content,
                    position(v, "content_index")?,
                    text(&v["part"], "text")?,
                    true,
                    kind.ends_with(".done"),
                )
            }
            _ => unexpected(),
        }
    }

    pub fn finish(&mut self, v: &Value, w: &Writer, rid: &str, index: usize) -> Result<usize> {
        if v["type"]
            != if self.compaction {
                "compaction"
            } else {
                "reasoning"
            }
        {
            return unexpected();
        }
        // The added event can carry provisional ciphertext. Only a completed
        // item is authoritative; a terminal snapshot may omit optional fields.
        if let Some((prior, _)) = &self.final_item {
            let consistent = v.as_object().is_some_and(|o| {
                o.iter().all(|(k, f)| {
                    (f.is_null()
                        && matches!(k.as_str(), "encrypted_content" | "content" | "status"))
                        || prior.get(k) == Some(f)
                })
            });
            return if consistent { Ok(0) } else { unexpected() };
        }
        reconcile(&mut self.summary, v.get("summary"), "summary_text", true)?;
        reconcile(
            &mut self.private_content,
            v.get("content"),
            "reasoning_text",
            true,
        )?;
        let block = w.project(v, rid, index)?;
        let bytes = v.to_string().len() + block.to_string().len();
        self.final_item = Some((v.clone(), block));
        Ok(bytes)
    }

    pub fn flush(&mut self, n: usize, out: &mut Vec<Value>) -> Result<bool> {
        while !self.hide_summary
            && let Some(p) = self.summary.get_mut(self.cursor)
        {
            let mut suffix = String::new();
            if self.cursor > 0 && !p.separator {
                suffix.push_str("\n\n");
                p.separator = true;
            }
            suffix.push_str(&p.data[p.sent..]);
            p.sent = p.data.len();
            if !suffix.is_empty() {
                if !self.started {
                    out.push(json!({"type":"content_block_start","index":n,"content_block":{"type":"thinking","thinking":""}}));
                    self.started = true;
                }
                out.push(json!({"type":"content_block_delta","index":n,"delta":{"type":"thinking_delta","thinking":suffix}}));
            }
            if !p.done {
                return Ok(false);
            }
            self.cursor += 1;
        }
        let Some((_, block)) = &self.final_item else {
            return Ok(false);
        };
        if self.started {
            out.push(json!({"type":"content_block_delta","index":n,"delta":{"type":"signature_delta","signature":text(block,"signature")?}}));
        } else {
            out.push(json!({"type":"content_block_start","index":n,"content_block":block}));
        }
        out.push(json!({"type":"content_block_stop","index":n}));
        Ok(true)
    }
}
