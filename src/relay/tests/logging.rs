//! Guards the log redaction contract of `nexus_relay::telemetry` in the relay source:
//! every tracing event uses allowlisted field names, a literal message without
//! interpolation, and values that do not name secret-bearing variables. Nothing may
//! bypass the logger with print macros. Test modules are excluded.
use nexus_relay::telemetry::LOG_FIELDS;
use std::{fs, path::Path};

const LEVELS: [&str; 5] = ["trace!(", "debug!(", "info!(", "warn!(", "error!("];
/// Variables that hold Keys, credentials, the private key, bodies or tool arguments.
const SECRET_NAMES: [&str; 11] = [
    "secret",
    "credential",
    "raw",
    "key",
    "private",
    "body",
    "frame",
    "text",
    "arguments",
    "authorization",
    "headers",
];

fn sources(dir: &Path, out: &mut Vec<(String, String)>) {
    for entry in fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            sources(&path, out);
        } else if path.extension().is_some_and(|e| e == "rs") {
            let text = fs::read_to_string(&path).unwrap();
            let code = text.split("#[cfg(test)]").next().unwrap().to_owned();
            out.push((path.display().to_string(), code));
        }
    }
}

/// Split macro arguments that start at `text` (just after the opening parenthesis) on
/// top-level commas, skipping nested brackets and string literals.
fn arguments(text: &str) -> Vec<String> {
    let (mut depth, mut args, mut current) = (0usize, Vec::new(), String::new());
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '"' => {
                current.push(c);
                while let Some(s) = chars.next() {
                    current.push(s);
                    if s == '\\' {
                        current.push(chars.next().unwrap());
                    } else if s == '"' {
                        break;
                    }
                }
                continue;
            }
            '(' | '[' | '{' => depth += 1,
            ')' | ']' | '}' if depth == 0 => {
                args.push(current.trim().to_owned());
                return args.into_iter().filter(|a| !a.is_empty()).collect();
            }
            ')' | ']' | '}' => depth -= 1,
            ',' if depth == 0 => {
                args.push(current.trim().to_owned());
                current.clear();
                continue;
            }
            _ => {}
        }
        current.push(c);
    }
    panic!("unterminated macro call");
}

fn words(expr: &str) -> impl Iterator<Item = &str> {
    expr.split(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
        .filter(|w| !w.is_empty())
}

#[test]
fn log_events_use_allowlisted_fields_and_static_messages() {
    let mut files = Vec::new();
    sources(
        &Path::new(env!("CARGO_MANIFEST_DIR")).join("src"),
        &mut files,
    );
    let mut events = 0;
    let mut problems = Vec::new();
    for (path, code) in &files {
        let calls = |name: &'static str| {
            code.match_indices(name).filter(|(at, _)| {
                let before = code[..*at].chars().next_back().unwrap_or(' ');
                !(before.is_ascii_alphanumeric() || before == '_')
            })
        };
        for banned in ["eprintln!", "eprint!", "println!", "print!", "dbg!"] {
            if calls(banned).next().is_some() {
                problems.push(format!("{path}: {banned} bypasses the redacting logger"));
            }
        }
        for level in LEVELS {
            for (at, _) in calls(level) {
                events += 1;
                let args = arguments(&code[at + level.len()..]);
                let (message, fields) = args.split_last().expect("an event has a message");
                if !(message.starts_with('"') && message.ends_with('"')) || message.contains('{') {
                    problems.push(format!("{path}: message must be a literal: {message}"));
                }
                for field in fields {
                    let (name, value) = match field.split_once('=') {
                        Some((name, value)) => (name.trim(), value.trim()),
                        None => (field.trim_start_matches(['%', '?']), field.as_str()),
                    };
                    if !LOG_FIELDS.contains(&name) {
                        problems.push(format!("{path}: field {name} is not in LOG_FIELDS"));
                    }
                    if let Some(word) = words(value).find(|w| SECRET_NAMES.contains(w)) {
                        problems.push(format!("{path}: {name} is derived from `{word}`"));
                    }
                }
            }
        }
    }
    assert!(problems.is_empty(), "{problems:#?}");
    assert!(
        events >= 25,
        "only {events} log events found; is the scan broken?"
    );
}
