//! Only numerical Guardrail assessment metrics may enter settlement evidence.
use super::Result;
use crate::protocol::{ProtocolError, convert, usage::UsageEvidence};
use serde_json::Value;
use std::collections::BTreeMap;

const COUNTERS: &[(&str, &str)] = &[
    ("contentPolicyUnits", "guardrail_content_units"),
    ("topicPolicyUnits", "guardrail_topic_units"),
    (
        "sensitiveInformationPolicyUnits",
        "guardrail_sensitive_units",
    ),
    (
        "contextualGroundingPolicyUnits",
        "guardrail_grounding_units",
    ),
    (
        "sensitiveInformationPolicyFreeUnits",
        "guardrail_sensitive_free_units",
    ),
    ("wordPolicyUnits", "guardrail_word_units"),
];

pub(crate) const PRICED: &[&str] = &[
    "guardrail_content_units",
    "guardrail_image_units",
    "guardrail_topic_units",
    "guardrail_sensitive_units",
    "guardrail_grounding_units",
    "guardrail_reasoning_units",
];

fn number(v: &Value) -> Result<u32> {
    v.as_u64()
        .filter(|&v| v <= 1_000_000_000)
        .map(|v| v as u32)
        .ok_or(ProtocolError::Invalid("Guardrail counter"))
}

fn add(out: &mut BTreeMap<&'static str, u32>, key: &'static str, n: u32) -> Result<()> {
    let total = out.entry(key).or_default();
    *total = total
        .checked_add(n)
        .filter(|v| *v <= 1_000_000_000)
        .ok_or(ProtocolError::Invalid("Guardrail counter total"))?;
    Ok(())
}

fn assessment(v: &Value, out: &mut BTreeMap<&'static str, u32>) -> Result<()> {
    let usage = &v["invocationMetrics"]["usage"];
    let fields = usage
        .as_object()
        .ok_or(ProtocolError::Invalid("missing Guardrail usage"))?;
    if fields.keys().any(|k| {
        !COUNTERS.iter().any(|(aws, _)| k == aws)
            && !matches!(
                k.as_str(),
                "contentPolicyImageUnits"
                    | "automatedReasoningPolicyUnits"
                    | "automatedReasoningPolicies"
            )
    }) {
        return Err(ProtocolError::Unsupported("unknown Guardrail usage"));
    }
    for &(aws, key) in COUNTERS {
        add(out, key, number(&usage[aws])?)?;
    }
    add(
        out,
        "guardrail_image_units",
        fields
            .get("contentPolicyImageUnits")
            .map(number)
            .transpose()?
            .unwrap_or(0),
    )?;
    let units = fields
        .get("automatedReasoningPolicyUnits")
        .map(number)
        .transpose()?;
    let policies = fields
        .get("automatedReasoningPolicies")
        .map(number)
        .transpose()?;
    // A single-policy receipt gives an unambiguous unit count. Retain multi-policy
    // observations for reconciliation until the supplier establishes its billed units.
    add(out, "guardrail_reasoning_text_units", units.unwrap_or(0))?;
    add(out, "guardrail_reasoning_policies", policies.unwrap_or(0))?;
    match (units, policies) {
        (Some(0), _) | (None, None | Some(0)) => add(out, "guardrail_reasoning_units", 0)?,
        (Some(n), Some(1)) => add(out, "guardrail_reasoning_units", n)?,
        _ => {
            out.insert("guardrail_usage_unverified", 1);
        }
    }
    Ok(())
}

fn counters(trace: &Value) -> Result<BTreeMap<&'static str, u32>> {
    convert::fields(
        trace,
        &[
            "inputAssessment",
            "outputAssessments",
            "actionReason",
            "modelOutput",
        ],
    )?;
    let mut out = BTreeMap::new();
    for (field, sequence) in [("inputAssessment", false), ("outputAssessments", true)] {
        if let Some(map) = trace.get(field) {
            let map = map
                .as_object()
                .ok_or(ProtocolError::Invalid("Guardrail assessment map"))?;
            for v in map.values() {
                if sequence {
                    for a in convert::array(v, "Guardrail output assessments")? {
                        assessment(a, &mut out)?;
                    }
                } else {
                    assessment(v, &mut out)?;
                }
            }
        }
    }
    if out.contains_key("guardrail_usage_unverified") {
        out.remove("guardrail_reasoning_units");
    }
    if out.is_empty() {
        return Err(ProtocolError::Invalid("missing Guardrail assessments"));
    }
    Ok(out)
}

pub fn evidence(body: &Value, expected: bool) -> UsageEvidence {
    let mut e = super::wire::evidence(body.get("usage"));
    let trace = body["trace"].get("guardrail");
    if !expected && trace.is_none() {
        return e;
    }
    let raw = e.raw_usage.get_or_insert_with(BTreeMap::new);
    match trace.map(counters) {
        Some(Ok(counts)) => raw.extend(counts),
        _ => {
            raw.insert("guardrail_usage_unverified", 1);
        }
    }
    e
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn incomplete_traces_keep_observed_tokens_without_settling_unknown_filters() {
        let usage = json!({"inputTokens":1,"outputTokens":2,"totalTokens":3});
        for trace in [
            Value::Null,
            json!({}),
            json!({"outputAssessments":{"g":[{}]}}),
            json!({"inputAssessment":{"g":{"invocationMetrics":{"usage":{"contentPolicyUnits":1,"newPaidUnit":2}}}}}),
        ] {
            let observed = evidence(&json!({"usage":usage,"trace":{"guardrail":trace}}), true);
            let counts = observed.raw_usage.unwrap();
            assert_eq!(counts["guardrail_usage_unverified"], 1);
            assert_eq!(counts["output_tokens"], 2);
        }
        let body = json!({"usage":usage});
        assert_eq!(
            evidence(&body, true).raw_usage.unwrap()["guardrail_usage_unverified"],
            1
        );
        assert!(
            !evidence(&body, false)
                .raw_usage
                .unwrap()
                .contains_key("guardrail_usage_unverified")
        );
    }

    #[test]
    fn overflow_and_multiple_reasoning_policies_do_not_become_verified_units() {
        let usage = json!({"contentPolicyUnits":1_000_000_000,"topicPolicyUnits":0,"sensitiveInformationPolicyUnits":0,
                "contextualGroundingPolicyUnits":0,"wordPolicyUnits":1,"sensitiveInformationPolicyFreeUnits":0,
                "automatedReasoningPolicyUnits":3,"automatedReasoningPolicies":2});
        let a = json!({"invocationMetrics":{"usage":usage}});
        let counted = counters(&json!({"inputAssessment":{"a":a}})).unwrap();
        assert_eq!(counted["guardrail_reasoning_policies"], 2);
        assert_eq!(counted["guardrail_reasoning_text_units"], 3);
        assert_eq!(counted["guardrail_usage_unverified"], 1);
        assert!(!counted.contains_key("guardrail_reasoning_units"));
        assert!(
            counters(&json!({"inputAssessment":{"a":a},"outputAssessments":{"b":[a]}})).is_err()
        );
    }
}
