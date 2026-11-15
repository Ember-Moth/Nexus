//! Per-request consent for conversions which change provider semantics.
use super::{
    Endpoint, ProtocolError,
    convert::{Result, fields, text},
};
use http::HeaderMap;
use serde_json::{Value, json};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Lifetime {
    FiveMinutes,
    OneHour,
    ThirtyMinutes,
    TwelveHours,
}
impl Lifetime {
    pub fn name(self) -> &'static str {
        match self {
            Self::FiveMinutes => "5m",
            Self::OneHour => "1h",
            Self::ThirtyMinutes => "30m",
            Self::TwelveHours => "12h",
        }
    }
    pub fn parse(s: &str) -> Result<Self> {
        match s {
            "5m" => Ok(Self::FiveMinutes),
            "1h" => Ok(Self::OneHour),
            "30m" => Ok(Self::ThirtyMinutes),
            "12h" => Ok(Self::TwelveHours),
            _ => Err(ProtocolError::Invalid("cache lifetime")),
        }
    }
    pub fn endpoint(self) -> Endpoint {
        match self {
            Self::FiveMinutes | Self::OneHour => Endpoint::AnthropicMessages,
            _ => Endpoint::OpenaiResponses,
        }
    }
    fn dimension(self) -> &'static str {
        match self {
            Self::FiveMinutes => "input_cache_write_5m_tokens",
            Self::OneHour => "input_cache_write_1h_tokens",
            Self::ThirtyMinutes => "input_cache_write_30m_tokens",
            Self::TwelveHours => "input_cache_write_12h_tokens",
        }
    }
}
const LIFETIMES: [Lifetime; 4] = [
    Lifetime::FiveMinutes,
    Lifetime::OneHour,
    Lifetime::ThirtyMinutes,
    Lifetime::TwelveHours,
];

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Choices {
    cache: [Option<Lifetime>; 4],
    pub(crate) stop: bool,
    pub(crate) prefill: bool,
}
impl Choices {
    pub fn parse(headers: &HeaderMap) -> Result<Self> {
        let mut out = Self::default();
        for h in headers.get_all("x-nexus-allow-approximation") {
            for opt in h
                .to_str()
                .map_err(|_| ProtocolError::Invalid("approximation header"))?
                .split(',')
                .map(str::trim)
            {
                match opt {
                    "stop=local" if !out.stop => out.stop = true,
                    "prefill=prompt" if !out.prefill => out.prefill = true,
                    _ => {
                        let (from, to) = opt
                            .strip_prefix("cache-")
                            .and_then(|s| s.split_once('='))
                            .ok_or(ProtocolError::Invalid("approximation option"))?;
                        let (from, to) = (Lifetime::parse(from)?, Lifetime::parse(to)?);
                        if from.endpoint() == to.endpoint()
                            || out.cache[from as usize].replace(to).is_some()
                        {
                            return Err(ProtocolError::Invalid("cache lifetime mapping"));
                        }
                    }
                }
            }
        }
        Ok(out)
    }
    pub(crate) fn active(self) -> bool {
        self != Self::default()
    }
    pub(crate) fn priced(self, dimensions: &[String]) -> bool {
        self.cache
            .into_iter()
            .flatten()
            .all(|ttl| dimensions.iter().any(|d| d == ttl.dimension()))
    }
    pub(crate) fn labels(self) -> (String, String) {
        let mut applied = Vec::new();
        let mut billing = Vec::new();
        for from in LIFETIMES {
            if let Some(to) = self.cache[from as usize] {
                applied.push(format!("cache-{}={}", from.name(), to.name()));
                if !billing.contains(&to.dimension()) {
                    billing.push(to.dimension());
                }
            }
        }
        if self.stop {
            applied.push("stop=local".into());
            billing.push("full_upstream_output");
        }
        if self.prefill {
            applied.push("prefill=prompt".into());
            billing.push("continuation_prompt_input");
        }
        (applied.join(", "), billing.join(", "))
    }
}

pub(super) struct Policy {
    pub allow: Choices,
    pub applied: Choices,
    pub default_ttl: Option<Lifetime>,
}
impl Policy {
    pub fn new(allow: Choices) -> Self {
        Self {
            allow,
            applied: Choices::default(),
            default_ttl: None,
        }
    }
    pub fn lifetime(&mut self, source: Endpoint, ttl: Lifetime) -> Result<Lifetime> {
        if ttl.endpoint() != source {
            return Err(ProtocolError::Invalid("source cache lifetime"));
        }
        let target = self.allow.cache[ttl as usize]
            .ok_or(ProtocolError::Unsupported("explicit cache lifetime"))?;
        self.applied.cache[ttl as usize] = Some(target);
        Ok(target)
    }
    pub fn marker(&mut self, source: Endpoint, marker: &Value, block: bool) -> Result<Value> {
        let (field, value) = if source == Endpoint::AnthropicMessages {
            ("type", "ephemeral")
        } else {
            ("mode", "explicit")
        };
        fields(marker, &[field, "ttl"])?;
        if marker[field] != value {
            return Err(ProtocolError::Unsupported("cache marker"));
        }
        let mut out = if source == Endpoint::AnthropicMessages {
            json!({"mode":"explicit"})
        } else {
            json!({"type":"ephemeral"})
        };
        let explicit = marker
            .get("ttl")
            .map(|_| Lifetime::parse(text(marker, "ttl")?))
            .transpose()?;
        if let Some(ttl) = explicit.or(self
            .default_ttl
            .filter(|_| source == Endpoint::OpenaiResponses))
        {
            out["ttl"] = self.lifetime(source, ttl)?.name().into();
        } else if block && source == Endpoint::AnthropicMessages && self.default_ttl.is_some() {
            // An explicit Messages block does not inherit the automatic marker's TTL.
            out["ttl"] = "30m".into();
        }
        Ok(out)
    }
    pub fn breakpoint(&mut self, source: Endpoint, block: &Value, out: &mut Value) -> Result<()> {
        let (from, to) = if source == Endpoint::AnthropicMessages {
            ("cache_control", "prompt_cache_breakpoint")
        } else {
            ("prompt_cache_breakpoint", "cache_control")
        };
        if let Some(marker) = block.get(from) {
            let mapped = self.marker(source, marker, true)?;
            if out.get(to).is_some_and(|prior| prior != &mapped) {
                return Err(ProtocolError::Unsupported("conflicting cache lifetimes"));
            }
            out[to] = mapped;
        }
        Ok(())
    }
}

pub(super) fn prefill(body: &Value) -> Result<()> {
    let last = body["messages"]
        .as_array()
        .and_then(|v| v.last())
        .ok_or(ProtocolError::Invalid("assistant prefill"))?;
    let v = &last["content"];
    if v.as_str().is_some_and(|s| !s.is_empty())
        || v.as_array().is_some_and(|p| {
            !p.is_empty()
                && p.iter()
                    .all(|b| b["type"] == "text" && b["text"].is_string())
                && p.iter()
                    .any(|b| b["text"].as_str().is_some_and(|s| !s.is_empty()))
        })
    {
        Ok(())
    } else {
        Err(ProtocolError::Unsupported(
            "text assistant prefill required",
        ))
    }
}

pub(super) fn validate_stops(value: &Value) -> Result<()> {
    if value
        .as_array()
        .is_none_or(|a| a.iter().any(|v| v.as_str().is_none_or(str::is_empty)))
    {
        return Err(ProtocolError::Invalid("stop sequences"));
    }
    Ok(())
}

impl Choices {
    pub(crate) fn mapped_cache(self) -> bool {
        self.cache.iter().any(Option::is_some)
    }
    pub(crate) fn headers(self, builder: http::response::Builder) -> http::response::Builder {
        if !self.active() {
            return builder;
        }
        let (rules, charges) = self.labels();
        builder
            .header("x-nexus-applied-approximation", rules)
            .header("x-nexus-approximation-billing", charges)
    }
}

// Claude orders rendered cache breakpoints by tools, system, conversation, then the automatic marker.
pub(super) fn check_messages_order(body: &Value) -> Result<()> {
    let mut short = false;
    let mut check = |marker: Option<&Value>| -> Result<()> {
        if let Some(m) = marker {
            if m["ttl"] == "1h" && short {
                return Err(ProtocolError::Invalid(
                    "long cache lifetime after short lifetime",
                ));
            }
            short |= m["ttl"] != "1h";
        }
        Ok(())
    };
    for k in ["tools", "system"] {
        for b in body[k].as_array().into_iter().flatten() {
            check(b.get("cache_control"))?;
        }
    }
    for m in body["messages"].as_array().into_iter().flatten() {
        for b in m["content"].as_array().into_iter().flatten() {
            if b["type"] == "tool_result" {
                for p in b["content"].as_array().into_iter().flatten() {
                    check(p.get("cache_control"))?;
                }
            }
            check(b.get("cache_control"))?;
        }
    }
    check(body.get("cache_control"))
}

#[derive(Clone, Debug, Default)]
pub(crate) struct Delivery {
    pub applied: Choices,
    pub stops: Option<std::sync::Arc<super::stop::Stops>>,
}
impl Delivery {
    pub fn new(applied: Choices, r: &super::request::Generate) -> Result<Self> {
        let stops = if applied.stop {
            let strings = if let Some(v) = &r.chat_stop {
                v.clone()
            } else {
                super::convert::array(&r.body()["stop_sequences"], "stop sequences")?
                    .iter()
                    .map(|s| {
                        s.as_str()
                            .map(str::to_owned)
                            .ok_or(ProtocolError::Invalid("stop sequence"))
                    })
                    .collect::<Result<Vec<_>>>()?
            };
            Some(super::stop::Stops::new(strings)?)
        } else {
            None
        };
        Ok(Self { applied, stops })
    }
}
