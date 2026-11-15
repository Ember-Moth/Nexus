//! Local stop matching is a delivery projection. The native terminal receipt remains authoritative.
use super::{
    ProtocolError,
    convert::{Result, array, text},
};
use aho_corasick::{
    Anchored,
    automaton::{Automaton, StateID},
    nfa::contiguous::NFA,
};
use serde_json::Value;
use std::{collections::BTreeMap, sync::Arc};

#[derive(Debug)]
pub(crate) struct Stops {
    machine: NFA,
    start: StateID,
    patterns: Vec<String>,
    longest: usize,
}
impl Stops {
    pub fn new(patterns: Vec<String>) -> Result<Arc<Self>> {
        let longest = patterns
            .iter()
            .map(String::len)
            .max()
            .filter(|n| *n > 0)
            .ok_or(ProtocolError::Invalid("stop sequences"))?;
        if patterns.iter().any(String::is_empty) {
            return Err(ProtocolError::Invalid("empty stop sequence"));
        }
        let machine = NFA::builder()
            .build(&patterns)
            .map_err(|_| ProtocolError::Invalid("stop sequences too large"))?;
        let start = machine
            .start_state(Anchored::No)
            .expect("unanchored stop matcher");
        Ok(Arc::new(Self {
            machine,
            start,
            patterns,
            longest,
        }))
    }
    // First completed match. If several finish at the same byte, prefer the longest,
    // then declaration order. Raw positions are bytes at valid UTF-8 boundaries.
    fn scan(&self, state: &mut StateID, s: &str, offset: usize) -> Option<(usize, usize)> {
        for (i, b) in s.bytes().enumerate() {
            *state = self.machine.next_state(Anchored::No, *state, b);
            if self.machine.is_match(*state) {
                let id = (0..self.machine.match_len(*state))
                    .map(|n| self.machine.match_pattern(*state, n).as_usize())
                    .min_by_key(|&id| (std::cmp::Reverse(self.patterns[id].len()), id))
                    .expect("matching state");
                return Some((offset + i + 1 - self.patterns[id].len(), id));
            }
        }
        None
    }
    pub fn clip(&self, r: &mut Value) -> Result<Option<String>> {
        if !matches!(r["status"].as_str(), Some("completed" | "incomplete")) {
            return Ok(None);
        }
        if r["status"] == "incomplete" && r["incomplete_details"]["reason"] != "max_output_tokens"
            || r["output"].as_array().into_iter().flatten().any(|i| {
                i["type"] == "message"
                    && i["content"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .any(|p| p["type"] == "refusal")
            })
        {
            return Ok(None);
        }
        let mut state = self.start;
        let mut offset = 0;
        let mut parts = Vec::new();
        let mut found = None;
        'items: for (i, item) in array(&r["output"], "Responses output")?.iter().enumerate() {
            if item["type"] != "message" || item["role"] != "assistant" {
                state = self.start;
                offset = 0;
                parts.clear();
                continue;
            }
            for (p, part) in array(&item["content"], "message content")?
                .iter()
                .enumerate()
            {
                if part["type"] != "output_text" {
                    state = self.start;
                    offset = 0;
                    parts.clear();
                    continue;
                }
                let value = text(part, "text")?;
                if value.is_empty() {
                    continue;
                }
                parts.push((offset, i, p));
                if let Some((at, id)) = self.scan(&mut state, value, offset) {
                    let (base, i, p) = parts[parts.partition_point(|(n, _, _)| *n <= at) - 1];
                    found = Some((i, p, at - base, id));
                    break 'items;
                }
                offset += value.len();
            }
        }
        let Some((i, p, bytes, id)) = found else {
            return Ok(None);
        };
        let items = r["output"].as_array_mut().expect("validated output");
        items.truncate(i + 1);
        let item = &mut items[i];
        item["status"] = "completed".into();
        let parts = item["content"].as_array_mut().expect("validated parts");
        parts.truncate(p + 1);
        let part = &mut parts[p];
        let before = &text(part, "text")?[..bytes];
        let length = before.chars().count() as u64;
        part["text"] = before.into();
        if let Some(a) = part["annotations"].as_array_mut() {
            a.retain(|v| match v["type"].as_str() {
                Some("url_citation" | "container_file_citation") => {
                    v["end_index"].as_u64().is_some_and(|n| n <= length)
                }
                Some("file_citation" | "file_path") => {
                    v["index"].as_u64().is_some_and(|n| n <= length)
                }
                _ => false,
            });
        }
        if part.get("logprobs").is_some() {
            part["logprobs"] = serde_json::json!([]);
        }
        r["status"] = "completed".into();
        r["incomplete_details"] = Value::Null;
        Ok(Some(self.patterns[id].clone()))
    }
}

// Emit safe deltas from the first ordered answer part. Once events interleave or a
// part finishes, the terminal output supplies the remaining ordered suffix and any
// citations. This also handles stop strings spanning adjacent text parts/items.
pub(crate) struct Preview {
    stops: Arc<Stops>,
    state: StateID,
    next: usize,
    current: Option<(String, bool)>,
    text_item: Option<(usize, String)>,
    observed: String,
    sent: usize,
    deferred: bool,
    completed: BTreeMap<usize, Value>,
    retained: usize,
    limit: usize,
    pub matched: Option<String>,
}
impl Preview {
    pub fn new(stops: Arc<Stops>, limit: usize) -> Self {
        Self {
            state: stops.start,
            stops,
            next: 0,
            current: None,
            text_item: None,
            observed: String::new(),
            sent: 0,
            deferred: false,
            completed: BTreeMap::new(),
            retained: 0,
            limit,
            matched: None,
        }
    }
    pub fn event(&mut self, v: &mut Value) -> Result<bool> {
        let kind = text(v, "type")?;
        if kind == "keepalive" {
            return Ok(false);
        }
        if matches!(
            kind,
            "error"
                | "response.failed"
                | "response.cancelled"
                | "response.created"
                | "response.in_progress"
                | "response.queued"
        ) {
            return Ok(true);
        }
        if matches!(kind, "response.completed" | "response.incomplete") {
            let r = &mut v["response"];
            let out = r["output"]
                .as_array_mut()
                .ok_or(ProtocolError::Invalid("terminal output"))?;
            for (i, done) in std::mem::take(&mut self.completed) {
                let item = out.get_mut(i).ok_or(ProtocolError::UnexpectedEvent)?;
                if item["id"] != done["id"] || item["type"] != done["type"] {
                    return Err(ProtocolError::UnexpectedEvent);
                }
                for (k, old) in done.as_object().ok_or(ProtocolError::UnexpectedEvent)? {
                    match item.get(k) {
                        Some(new) if new == old => {}
                        None | Some(Value::Null)
                            if matches!(
                                k.as_str(),
                                "encrypted_content" | "content" | "status" | "summary"
                            ) =>
                        {
                            item[k] = old.clone()
                        }
                        _ => return Err(ProtocolError::Invalid("conflicting terminal item")),
                    }
                }
            }
            if let Some((i, id)) = &self.text_item {
                let part = &out.get(*i).ok_or(ProtocolError::UnexpectedEvent)?;
                if part["id"] != id.as_str()
                    || !text(&part["content"][0], "text")?.starts_with(&self.observed)
                {
                    return Err(ProtocolError::UnexpectedEvent);
                }
            }
            self.matched = self.stops.clip(r)?;
            if self.matched.is_some() {
                v["type"] = "response.completed".into();
            }
            return Ok(true);
        }
        let i = v["output_index"]
            .as_u64()
            .and_then(|v| usize::try_from(v).ok());
        if kind == "response.output_item.done" {
            let n = i
                .filter(|n| *n < 4096)
                .ok_or(ProtocolError::UnexpectedEvent)?;
            self.retained += v["item"].to_string().len();
            if self.retained > self.limit || self.completed.insert(n, v["item"].clone()).is_some() {
                return Err(ProtocolError::UnexpectedEvent);
            }
        }
        if self.deferred {
            return Ok(false);
        }
        if i != Some(self.next) {
            self.deferred = true;
            return Ok(false);
        }
        if kind == "response.output_item.added" {
            let item = &v["item"];
            let message = item["type"] == "message";
            if self.current.is_some()
                || message && !item["content"].as_array().is_some_and(Vec::is_empty)
            {
                self.deferred = true;
                return Ok(false);
            }
            self.current = Some((text(item, "id")?.into(), message));
            return Ok(true);
        }
        let Some((id, message)) = &self.current else {
            self.deferred = true;
            return Ok(false);
        };
        if v.get("item_id").is_some_and(|v| v != id.as_str())
            || kind == "response.output_item.done" && v["item"]["id"] != id.as_str()
        {
            return Err(ProtocolError::UnexpectedEvent);
        }
        if !message {
            if kind == "response.output_item.done" {
                self.current = None;
                self.next += 1;
            }
            return Ok(true);
        }
        if v["item_id"] == id.as_str() && v["content_index"] == 0 {
            if kind == "response.content_part.added"
                && v["part"]["type"] == "output_text"
                && v["part"]
                    .get("annotations")
                    .is_none_or(|a| a.is_null() || a.as_array().is_some_and(Vec::is_empty))
                && v["part"]["text"] == ""
            {
                return Ok(true);
            }
            if kind == "response.output_text.delta" {
                let s = text(v, "delta")?;
                if self.observed.len() + s.len() > self.limit {
                    return Err(ProtocolError::EventTooLarge);
                }
                self.text_item
                    .get_or_insert_with(|| (self.next, id.clone()));
                let hit = self.stops.scan(&mut self.state, s, self.observed.len());
                self.observed.push_str(s);
                let mut safe = if let Some((n, _)) = hit {
                    self.deferred = true;
                    n
                } else if self.state == self.stops.start {
                    self.observed.len()
                } else {
                    self.observed
                        .len()
                        .saturating_sub(self.stops.longest - 1)
                        .max(self.sent)
                };
                while !self.observed.is_char_boundary(safe) {
                    safe -= 1;
                }
                if safe == self.sent {
                    return Ok(false);
                }
                let suffix = self
                    .observed
                    .get(self.sent..safe)
                    .ok_or(ProtocolError::UnexpectedEvent)?;
                v["delta"] = suffix.into();
                self.sent = safe;
                return Ok(true);
            }
        }
        self.deferred = true;
        Ok(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn body(parts: &[&str]) -> Value {
        json!({"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"},"output":[{
            "id":"m","type":"message","role":"assistant","content":parts.iter().map(|s|json!({"type":"output_text","text":s,"annotations":[]})).collect::<Vec<_>>()
        }],"usage":{"output_tokens":21}})
    }
    #[test]
    fn first_completed_marker_handles_unicode_overlap_and_partial_suffixes() {
        let stops = Stops::new(vec!["XY".into(), "aXY".into(), "日本語".into()]).unwrap();
        for (parts, wanted, first) in [
            (vec!["前a", "XY", "後"], Some("aXY"), "前"),
            (vec!["語日", "本", "語終"], Some("日本語"), "語"),
            (vec!["日本"], None, "日本"),
            (vec!["XY"], Some("XY"), ""),
        ] {
            let mut v = body(&parts);
            assert_eq!(stops.clip(&mut v).unwrap().as_deref(), wanted);
            assert_eq!(v["output"][0]["content"][0]["text"], first);
            assert_eq!(v["usage"]["output_tokens"], 21);
            assert_eq!(
                v["status"],
                if wanted.is_some() {
                    "completed"
                } else {
                    "incomplete"
                }
            );
        }
    }

    #[test]
    fn clipped_citations_do_not_refer_to_hidden_text() {
        let stops = Stops::new(vec!["STOP".into()]).unwrap();
        let mut v = body(&["ABSTOPCD"]);
        v["output"][0]["content"][0]["annotations"] = json!([
            {"type":"url_citation","start_index":0,"end_index":2},
            {"type":"url_citation","start_index":1,"end_index":6}]);
        stops.clip(&mut v).unwrap();
        assert_eq!(
            v["output"][0]["content"][0]["annotations"],
            json!([{"type":"url_citation","start_index":0,"end_index":2}])
        );
    }

    #[test]
    fn provisional_stop_cannot_turn_a_failure_or_conflicting_receipt_into_success() {
        let s = Stops::new(vec!["END".into()]).unwrap();
        let mut preview = Preview::new(s, 4096);
        for mut event in [
            json!({"type":"response.output_item.added","output_index":0,"item":{"type":"message","role":"assistant","id":"m","content":[]}}),
            json!({"type":"response.output_text.delta","output_index":0,"content_index":0,"item_id":"m","delta":"safeENDhidden"}),
        ] {
            assert!(preview.event(&mut event).unwrap());
            if event.get("delta").is_some() {
                assert_eq!(event["delta"], "safe");
            }
        }
        assert!(
            preview
                .event(&mut json!({"type":"response.failed","response":{"status":"failed"}}))
                .unwrap()
        );
        assert!(preview.matched.is_none());
        assert!(
            preview
                .event(&mut json!({"type":"response.completed","response":body(&["safe changed"])}))
                .is_err()
        );
    }
    #[test]
    fn provider_refusal_and_content_filter_remain_authoritative() {
        let set = Stops::new(vec!["END".into()]).unwrap();
        let mut filtered = body(&["beforeEND"]);
        filtered["incomplete_details"]["reason"] = "content_filter".into();
        let original = filtered.clone();
        assert!(set.clip(&mut filtered).unwrap().is_none());
        assert_eq!(filtered, original);
        filtered["status"] = "completed".into();
        filtered["output"][0]["content"]
            .as_array_mut()
            .unwrap()
            .push(json!({"type":"refusal","refusal":"refused"}));
        assert!(set.clip(&mut filtered).unwrap().is_none());
        assert_eq!(filtered["output"][0]["content"][1]["type"], "refusal");
    }
}
