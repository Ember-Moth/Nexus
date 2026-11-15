use super::{ProtocolError, fields, object};
use serde_json::{Map, Value};
use std::collections::BTreeMap;

// The top-level Claude counters describe non-compaction iterations. The signed
// block has no monetary value on replay; only a new compaction iteration is billed.
const KEYS: [&str; 6] = [
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
];
const COMPACT: [&str; 6] = [
    "compaction_input_tokens",
    "compaction_output_tokens",
    "compaction_cache_read_input_tokens",
    "compaction_cache_creation_input_tokens",
    "compaction_cache_write_5m_tokens",
    "compaction_cache_write_1h_tokens",
];

fn count(v: &Value) -> Result<u32, ProtocolError> {
    v.as_u64()
        .filter(|n| *n <= 1_000_000_000)
        .map(|n| n as u32)
        .ok_or(ProtocolError::Invalid("iteration token count"))
}
fn add(a: u32, b: u32) -> Result<u32, ProtocolError> {
    a.checked_add(b)
        .filter(|n| *n <= 1_000_000_000)
        .ok_or(ProtocolError::Invalid("iteration token total"))
}

/// Message deltas can omit top-level input until combined with message_start.
pub(crate) fn terminal_totals(usage: Option<&Value>) -> bool {
    usage
        .filter(|v| v.get("iterations").is_some_and(|rows| !rows.is_null()))
        .is_none_or(|v| {
            ["input_tokens", "output_tokens"]
                .iter()
                .all(|k| v[*k].as_u64().is_some())
        })
}

pub(super) fn apply(
    value: &Value,
    model: Option<&str>,
    counts: &mut BTreeMap<&'static str, u32>,
) -> Result<(), ProtocolError> {
    let rows = value
        .as_array()
        .filter(|v| !v.is_empty())
        .ok_or(ProtocolError::Invalid("usage iterations"))?;
    let mut message = [0u32; 6];
    let mut compact = [0u32; 6];
    let mut compacted = false;
    for row in rows {
        fields(
            object(row)?,
            &[
                "type",
                "model",
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "cache_creation",
            ],
        )?;
        if let Some(v) = row.get("model").filter(|v| !v.is_null())
            && (v.as_str().is_none() || v.as_str() != model)
        {
            return Err(ProtocolError::Unsupported(
                "differently priced iteration model",
            ));
        }
        let sum = match row["type"].as_str() {
            Some("message") => &mut message,
            Some("compaction") => {
                compacted = true;
                &mut compact
            }
            _ => return Err(ProtocolError::Unsupported("usage iteration type")),
        };
        let mut units = [0u32; 6];
        for (i, key) in KEYS[..4].iter().enumerate() {
            units[i] = count(&row[*key])?;
        }
        if let Some(cache) = row.get("cache_creation").filter(|v| !v.is_null()) {
            fields(
                object(cache)?,
                &["ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"],
            )?;
            units[4] = count(&cache["ephemeral_5m_input_tokens"])?;
            units[5] = count(&cache["ephemeral_1h_input_tokens"])?;
        }
        if units[3] != add(units[4], units[5])? {
            return Err(ProtocolError::Invalid("iteration cache creation breakdown"));
        }
        for (total, n) in sum.iter_mut().zip(units) {
            *total = add(*total, n)?;
        }
    }
    for i in 0..KEYS.len() {
        if counts.get(KEYS[i]).is_some_and(|&n| n != message[i]) {
            return Err(ProtocolError::Invalid("conflicting iteration total"));
        }
        counts.insert(KEYS[i], add(message[i], compact[i])?);
        if compacted {
            counts.insert(COMPACT[i], compact[i]);
        }
    }
    Ok(())
}

pub(crate) fn cumulative(old: &Value, new: &Value) -> bool {
    let (Some(old), Some(new)) = (old.as_array(), new.as_array()) else {
        return old.is_null();
    };
    if old.is_empty() {
        return true;
    }
    if new.len() < old.len() || old[..old.len() - 1] != new[..old.len() - 1] {
        return false;
    }
    let a = &old[old.len() - 1];
    let b = &new[old.len() - 1];
    fn extends(a: &Map<String, Value>, b: &Map<String, Value>) -> bool {
        a.iter().all(|(k, old)| {
            b.get(k).is_some_and(|v| {
                old.is_null()
                    || v == old
                    || old.as_u64().zip(v.as_u64()).is_some_and(|(x, y)| x <= y)
                    || old
                        .as_object()
                        .zip(v.as_object())
                        .is_some_and(|(x, y)| extends(x, y))
            })
        })
    }
    a.as_object()
        .zip(b.as_object())
        .is_some_and(|(a, b)| extends(a, b))
}
