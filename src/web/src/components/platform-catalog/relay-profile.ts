import type { MessageKey } from "@/i18n/provider";

import type { CatalogModel, ProtocolFamily, RelayProfile, Upstream } from "./types";

// Declarative description of deployment.relay_profile (components.schemas.RelayProfile).
// The editor, the draft <-> payload conversion and the validation are all driven by
// RELAY_FIELDS: a new capability switch from the contract is one entry here plus its label in
// pc.relay.fields. Keys the list does not know yet are kept unchanged on save.

export type RelayKey = keyof RelayProfile;
export type RelayGroup = "core" | "capabilities" | "tools" | "bedrock";

interface FieldBase {
  key: RelayKey;
  group: RelayGroup;
  label: MessageKey;
  hint?: MessageKey;
  /** Contract-required: the profile is rejected without it. */
  required?: boolean;
  /** Spans the full editor width (JSON and multi-select fields). */
  wide?: boolean;
}

export interface Option {
  value: string;
  /** Option label; raw values (e.g. rule identifiers) are shown in monospace when omitted. */
  label?: MessageKey;
}

export type RelayField =
  | (FieldBase & { kind: "boolean"; default: boolean })
  | (FieldBase & { kind: "integer"; default?: number; min: number; max: number })
  | (FieldBase & { kind: "string"; default?: string; maxLength: number; placeholder?: string })
  | (FieldBase & { kind: "enum"; options: readonly Option[]; default?: string; nullable?: boolean })
  | (FieldBase & { kind: "multi"; options: readonly Option[]; default: string[]; minItems?: number })
  | (FieldBase & { kind: "json"; shape: "object" | "array"; nullable?: boolean; placeholder?: string });

const MAX = 1_000_000_000;

const ENDPOINTS: readonly Option[] = [
  { value: "anthropic_messages", label: "pc.relay.options.anthropic_messages" },
  { value: "openai_responses", label: "pc.relay.options.openai_responses" },
  { value: "openai_chat_completions", label: "pc.relay.options.openai_chat_completions" },
  { value: "bedrock_converse", label: "pc.relay.options.bedrock_converse" },
];

export const RELAY_FIELDS: readonly RelayField[] = [
  // Endpoint and bounds
  { key: "endpoint", group: "core", kind: "enum", required: true, options: ENDPOINTS, label: "pc.relay.fields.endpoint.label", hint: "pc.relay.fields.endpoint.hint" },
  {
    key: "convert_from", group: "core", kind: "enum", nullable: true,
    options: [
      { value: "anthropic_messages", label: "pc.relay.options.anthropic_messages" },
      { value: "openai_responses", label: "pc.relay.options.openai_responses" },
    ],
    label: "pc.relay.fields.convert_from.label", hint: "pc.relay.fields.convert_from.hint",
  },
  {
    key: "transports", group: "core", kind: "multi", required: true, default: ["http"], minItems: 1,
    options: [
      { value: "http", label: "pc.relay.options.http" },
      { value: "websocket", label: "pc.relay.options.websocket" },
    ],
    label: "pc.relay.fields.transports.label", hint: "pc.relay.fields.transports.hint",
  },
  {
    key: "count_rule", group: "core", kind: "enum", required: true,
    options: [{ value: "deployment_ceiling.v1" }, { value: "o200k_base.v1" }, { value: "cl100k_base.v1" }],
    label: "pc.relay.fields.count_rule.label", hint: "pc.relay.fields.count_rule.hint",
  },
  { key: "input_ceiling", group: "core", kind: "integer", required: true, min: 1, max: MAX, label: "pc.relay.fields.input_ceiling.label", hint: "pc.relay.fields.input_ceiling.hint" },
  { key: "output_ceiling", group: "core", kind: "integer", required: true, min: 1, max: MAX, label: "pc.relay.fields.output_ceiling.label", hint: "pc.relay.fields.output_ceiling.hint" },
  { key: "bound_reference", group: "core", kind: "string", required: true, maxLength: 200, wide: true, label: "pc.relay.fields.bound_reference.label", hint: "pc.relay.fields.bound_reference.hint" },

  // Capability switches
  { key: "streaming", group: "capabilities", kind: "boolean", default: false, label: "pc.relay.fields.streaming.label", hint: "pc.relay.fields.streaming.hint" },
  { key: "tools", group: "capabilities", kind: "boolean", default: false, label: "pc.relay.fields.tools.label", hint: "pc.relay.fields.tools.hint" },
  { key: "continuation", group: "capabilities", kind: "boolean", default: false, label: "pc.relay.fields.continuation.label", hint: "pc.relay.fields.continuation.hint" },
  { key: "cancellation", group: "capabilities", kind: "boolean", default: false, label: "pc.relay.fields.cancellation.label", hint: "pc.relay.fields.cancellation.hint" },
  { key: "auto_prompt_cache", group: "capabilities", kind: "boolean", default: false, label: "pc.relay.fields.auto_prompt_cache.label", hint: "pc.relay.fields.auto_prompt_cache.hint" },

  // Hosted tools and their billing ceilings
  { key: "web_search_ceiling", group: "tools", kind: "integer", default: 0, min: 0, max: MAX, label: "pc.relay.fields.web_search_ceiling.label", hint: "pc.relay.fields.web_search_ceiling.hint" },
  {
    key: "web_search_variant", group: "tools", kind: "enum", default: "standard",
    options: [
      { value: "standard", label: "pc.relay.options.standard" },
      { value: "preview", label: "pc.relay.options.preview" },
    ],
    label: "pc.relay.fields.web_search_variant.label", hint: "pc.relay.fields.web_search_variant.hint",
  },
  { key: "file_search_ceiling", group: "tools", kind: "integer", default: 0, min: 0, max: MAX, label: "pc.relay.fields.file_search_ceiling.label", hint: "pc.relay.fields.file_search_ceiling.hint" },
  { key: "code_execution", group: "tools", kind: "boolean", default: false, label: "pc.relay.fields.code_execution.label", hint: "pc.relay.fields.code_execution.hint" },
  { key: "execution_reserve_seconds", group: "tools", kind: "integer", default: 0, min: 0, max: MAX, label: "pc.relay.fields.execution_reserve_seconds.label", hint: "pc.relay.fields.execution_reserve_seconds.hint" },
  {
    key: "container_tariff", group: "tools", kind: "enum", default: "session_20m",
    options: [
      { value: "session_20m", label: "pc.relay.options.session_20m" },
      { value: "minute_min_5", label: "pc.relay.options.minute_min_5" },
    ],
    label: "pc.relay.fields.container_tariff.label", hint: "pc.relay.fields.container_tariff.hint",
  },
  { key: "container_reserve_minutes", group: "tools", kind: "integer", default: 0, min: 0, max: MAX, label: "pc.relay.fields.container_reserve_minutes.label", hint: "pc.relay.fields.container_reserve_minutes.hint" },

  // Amazon Bedrock
  { key: "bedrock_invoke", group: "bedrock", kind: "boolean", default: false, label: "pc.relay.fields.bedrock_invoke.label", hint: "pc.relay.fields.bedrock_invoke.hint" },
  { key: "bedrock_guardrail_unit_ceiling", group: "bedrock", kind: "integer", default: 0, min: 0, max: MAX, label: "pc.relay.fields.bedrock_guardrail_unit_ceiling.label", hint: "pc.relay.fields.bedrock_guardrail_unit_ceiling.hint" },
  {
    key: "bedrock_guardrail", group: "bedrock", kind: "json", shape: "object", nullable: true, wide: true,
    placeholder: '{"guardrailIdentifier": "abcd", "guardrailVersion": "1", "trace": "disabled"}',
    label: "pc.relay.fields.bedrock_guardrail.label", hint: "pc.relay.fields.bedrock_guardrail.hint",
  },
  {
    key: "bedrock_model_fields", group: "bedrock", kind: "json", shape: "object", wide: true,
    placeholder: '{"top_k": 7}',
    label: "pc.relay.fields.bedrock_model_fields.label", hint: "pc.relay.fields.bedrock_model_fields.hint",
  },
  {
    key: "bedrock_request_options", group: "bedrock", kind: "json", shape: "array", wide: true,
    placeholder: '[{"guardrailConfig": {"guardrailIdentifier": "abcd", "guardrailVersion": "2"}, "additionalModelRequestFields": {"top_k": 7}}]',
    label: "pc.relay.fields.bedrock_request_options.label", hint: "pc.relay.fields.bedrock_request_options.hint",
  },
];

export const RELAY_GROUPS: readonly RelayGroup[] = ["core", "capabilities", "tools", "bedrock"];

/** Editor state: text for numbers, strings and JSON; booleans; string arrays for multi-selects. */
export type DraftValue = string | boolean | string[];
export type RelayDraft = Record<string, DraftValue>;

const KNOWN = new Set<string>(RELAY_FIELDS.map((field) => field.key));

/** Draft for an existing profile, or the defaults (with optional suggestions) for a new one. */
export function toDraft(profile: Partial<RelayProfile> | null | undefined): RelayDraft {
  const source = (profile ?? {}) as Record<string, unknown>;
  const draft: RelayDraft = {};
  for (const field of RELAY_FIELDS) {
    const value = source[field.key];
    switch (field.kind) {
      case "boolean":
        draft[field.key] = typeof value === "boolean" ? value : field.default;
        break;
      case "multi":
        draft[field.key] = Array.isArray(value) ? value.map(String) : [...field.default];
        break;
      case "json": {
        const empty =
          value === null ||
          value === undefined ||
          (Array.isArray(value) && value.length === 0) ||
          (typeof value === "object" && !Array.isArray(value) && Object.keys(value as object).length === 0);
        draft[field.key] = empty ? "" : JSON.stringify(value, null, 2);
        break;
      }
      case "integer":
        draft[field.key] = typeof value === "number" ? String(value) : field.default !== undefined ? String(field.default) : "";
        break;
      default:
        draft[field.key] = typeof value === "string" ? value : (field.default ?? "");
    }
  }
  return draft;
}

/** Fields of the original profile that RELAY_FIELDS does not describe; preserved on save. */
export function extraFields(profile: Partial<RelayProfile> | null | undefined): Record<string, unknown> {
  if (!profile) return {};
  return Object.fromEntries(Object.entries(profile).filter(([key]) => !KNOWN.has(key)));
}

export interface FieldProblem {
  message: MessageKey;
  vars?: Record<string, string | number>;
}

/**
 * Converts the draft to the request payload. Empty optional fields are omitted so the server
 * applies its defaults; `profile` is always as complete as possible so cross-field rules can
 * run on partially invalid input.
 */
export function fromDraft(
  draft: RelayDraft,
  extra: Record<string, unknown> = {},
): { profile: RelayProfile; problems: Partial<Record<RelayKey, FieldProblem>> } {
  const profile: Record<string, unknown> = { ...extra };
  const problems: Partial<Record<RelayKey, FieldProblem>> = {};
  for (const field of RELAY_FIELDS) {
    const raw = draft[field.key];
    switch (field.kind) {
      case "boolean":
        profile[field.key] = raw === true;
        break;
      case "multi": {
        const values = Array.isArray(raw) ? raw : [];
        if (values.length < (field.minItems ?? 0)) problems[field.key] = { message: "pc.relay.multiRequired" };
        profile[field.key] = field.options.map((o) => o.value).filter((v) => values.includes(v));
        break;
      }
      case "integer": {
        const text = typeof raw === "string" ? raw.trim() : "";
        if (!text) {
          if (field.required) problems[field.key] = { message: "pc.common.required" };
          break;
        }
        const number = /^\d{1,10}$/.test(text) ? Number(text) : Number.NaN;
        if (!Number.isInteger(number) || number < field.min || number > field.max) {
          problems[field.key] = {
            message: "pc.relay.integerInvalid",
            vars: { min: field.min.toLocaleString("en-US"), max: field.max.toLocaleString("en-US") },
          };
          break;
        }
        profile[field.key] = number;
        break;
      }
      case "string": {
        const text = typeof raw === "string" ? raw.trim() : "";
        if (!text) {
          if (field.required) problems[field.key] = { message: "pc.common.required" };
          break;
        }
        if (text.length > field.maxLength) {
          problems[field.key] = { message: "pc.relay.stringInvalid", vars: { max: field.maxLength } };
          break;
        }
        profile[field.key] = text;
        break;
      }
      case "enum": {
        const value = typeof raw === "string" ? raw : "";
        if (!value) {
          if (field.required) problems[field.key] = { message: "pc.common.required" };
          break;
        }
        profile[field.key] = value;
        break;
      }
      case "json": {
        const text = typeof raw === "string" ? raw.trim() : "";
        if (!text) break;
        let parsed: unknown;
        try {
          parsed = JSON.parse(text);
        } catch {
          problems[field.key] = { message: "pc.relay.jsonInvalid" };
          break;
        }
        const isArray = Array.isArray(parsed);
        const isObject = typeof parsed === "object" && parsed !== null && !isArray;
        if (field.shape === "array" ? !isArray : !isObject) {
          problems[field.key] = { message: field.shape === "array" ? "pc.relay.jsonArray" : "pc.relay.jsonObject" };
          break;
        }
        profile[field.key] = parsed;
        break;
      }
    }
  }
  return { profile: profile as unknown as RelayProfile, problems };
}

// Cross-field rules mirrored from the API's RelayProfile validator and deployment checks. They
// are shown as warnings; the server stays authoritative and its 422/409 is still displayed.

export interface RelayContext {
  model?: CatalogModel;
  upstream?: Upstream;
  region?: string;
}

interface RelayRule {
  message: MessageKey;
  /** Returns true (or message variables) when the combination is invalid. */
  check: (p: Partial<RelayProfile>, ctx: RelayContext) => boolean | Record<string, string | number>;
}

const ENDPOINT_FAMILY: Record<RelayProfile["endpoint"], ProtocolFamily> = {
  anthropic_messages: "anthropic",
  openai_responses: "openai_compatible",
  openai_chat_completions: "openai_compatible",
  bedrock_converse: "aws_bedrock",
};

/** Suggested endpoint and counting rule for a new profile on this upstream. */
export function suggestedProfile(upstream?: Upstream, model?: CatalogModel): Partial<RelayProfile> {
  const endpoint = (Object.keys(ENDPOINT_FAMILY) as RelayProfile["endpoint"][]).find(
    (e) => ENDPOINT_FAMILY[e] === upstream?.protocol_family,
  );
  return {
    endpoint,
    transports: ["http"],
    count_rule:
      endpoint === "anthropic_messages" || endpoint === "bedrock_converse" ? "deployment_ceiling.v1" : undefined,
    input_ceiling: model?.max_input_tokens,
    output_ceiling: model?.max_output_tokens,
    streaming: model?.capabilities?.streaming ?? false,
    tools: model?.capabilities?.tools ?? false,
  };
}

const RESERVED_MODEL_FIELDS = [
  "model", "messages", "system", "tools", "tool_choice", "max_tokens", "maxTokens", "inferenceConfig",
  "guardrailConfig", "additionalModelRequestFields", "toolConfig", "anthropic_version", "anthropic_beta", "stream",
];

const objectKeys = (value: unknown) =>
  typeof value === "object" && value !== null && !Array.isArray(value) ? Object.keys(value) : [];

const requestOptions = (p: Partial<RelayProfile>) => (Array.isArray(p.bedrock_request_options) ? p.bedrock_request_options : []);

function cacheDimensions(endpoint: RelayProfile["endpoint"]) {
  return ["input_cache_read_tokens", endpoint === "openai_responses" ? "input_cache_write_30m_tokens" : "input_cache_write_5m_tokens"];
}

export const RELAY_RULES: readonly RelayRule[] = [
  {
    message: "pc.relay.rules.container",
    check: (p) =>
      Boolean(p.container_reserve_minutes) &&
      (p.endpoint !== "openai_responses" ||
        !p.tools ||
        (p.container_reserve_minutes ?? 0) < (p.container_tariff === "minute_min_5" ? 5 : 20)),
  },
  { message: "pc.relay.rules.codeExecution", check: (p) => Boolean(p.code_execution) && (p.endpoint !== "anthropic_messages" || !p.tools) },
  {
    message: "pc.relay.rules.executionReserve",
    check: (p) => Boolean(p.execution_reserve_seconds) && (!p.code_execution || (p.execution_reserve_seconds ?? 0) < 300),
  },
  { message: "pc.relay.rules.fileSearch", check: (p) => Boolean(p.file_search_ceiling) && (p.endpoint !== "openai_responses" || !p.tools) },
  {
    message: "pc.relay.rules.searchPreview",
    check: (p) => p.web_search_variant === "preview" && (!p.web_search_ceiling || p.endpoint !== "openai_responses"),
  },
  { message: "pc.relay.rules.webSearch", check: (p) => Boolean(p.web_search_ceiling) && (!p.tools || p.endpoint === "bedrock_converse") },
  {
    message: "pc.relay.rules.guardrailUnits",
    check: (p) => Boolean(p.bedrock_guardrail_unit_ceiling) && !(p.bedrock_guardrail || requestOptions(p).some((o) => o?.guardrailConfig)),
  },
  { message: "pc.relay.rules.invoke", check: (p) => Boolean(p.bedrock_invoke) && p.endpoint !== "bedrock_converse" },
  {
    message: "pc.relay.rules.converseSettings",
    check: (p) =>
      Boolean(p.bedrock_guardrail || objectKeys(p.bedrock_model_fields).length || requestOptions(p).length) &&
      (p.endpoint !== "bedrock_converse" || Boolean(p.bedrock_invoke)),
  },
  {
    message: "pc.relay.rules.reservedFields",
    check: (p) =>
      [p.bedrock_model_fields, ...requestOptions(p).map((o) => o?.additionalModelRequestFields)].some((fields) =>
        objectKeys(fields).some((key) => RESERVED_MODEL_FIELDS.includes(key)),
      ),
  },
  {
    message: "pc.relay.rules.chat",
    check: (p) =>
      p.endpoint === "openai_chat_completions" &&
      (Boolean(p.transports?.includes("websocket")) ||
        (p.convert_from !== undefined && p.convert_from !== null && p.convert_from !== "openai_responses") ||
        Boolean(p.auto_prompt_cache) ||
        Boolean(p.web_search_ceiling)),
  },
  {
    message: "pc.relay.rules.bedrock",
    check: (p) =>
      p.endpoint === "bedrock_converse" &&
      (Boolean(p.transports?.includes("websocket")) ||
        Boolean(p.convert_from) ||
        (p.count_rule !== undefined && p.count_rule !== "deployment_ceiling.v1")),
  },
  {
    message: "pc.relay.rules.conversion",
    check: (p) => Boolean(p.convert_from) && (p.convert_from === p.endpoint || !p.transports?.includes("http")),
  },
  {
    message: "pc.relay.rules.messages",
    check: (p) =>
      p.endpoint === "anthropic_messages" &&
      (Boolean(p.transports?.includes("websocket")) || (p.count_rule !== undefined && p.count_rule !== "deployment_ceiling.v1")),
  },
  {
    message: "pc.relay.rules.websocketControls",
    check: (p) => !p.transports?.includes("websocket") && Boolean(p.continuation || p.cancellation),
  },
  // Checks against the selected model, upstream and region.
  {
    message: "pc.relay.rules.family",
    check: (p, ctx) =>
      p.endpoint && ctx.upstream && ENDPOINT_FAMILY[p.endpoint] !== ctx.upstream.protocol_family
        ? { endpoint: p.endpoint, protocol: ctx.upstream.protocol_family }
        : false,
  },
  {
    message: "pc.relay.rules.bounds",
    check: (p, ctx) =>
      ctx.model &&
      ((p.input_ceiling ?? 0) > ctx.model.max_input_tokens || (p.output_ceiling ?? 0) > ctx.model.max_output_tokens)
        ? {
            input: ctx.model.max_input_tokens.toLocaleString("en-US"),
            output: ctx.model.max_output_tokens.toLocaleString("en-US"),
          }
        : false,
  },
  {
    message: "pc.relay.rules.region",
    check: (p, ctx) => p.endpoint === "bedrock_converse" && !/^[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+$/.test(ctx.region ?? ""),
  },
  {
    message: "pc.relay.rules.dimensions",
    check: (p, ctx) => {
      if (!ctx.model) return false;
      const tools = new Set<string>(ctx.model.tool_dimensions ?? []);
      const tokens = new Set<string>(ctx.model.token_dimensions ?? []);
      const missing = new Set<string>();
      if (p.web_search_ceiling && !tools.has("web_search_requests")) missing.add("web_search_requests");
      if (p.file_search_ceiling) {
        for (const d of ["file_search_calls", "vector_storage_byte_seconds"]) if (!tools.has(d)) missing.add(d);
      }
      if (p.execution_reserve_seconds && !tools.has("code_execution_seconds")) missing.add("code_execution_seconds");
      if (p.container_reserve_minutes && ![...tools].some((d) => d.startsWith("container_"))) missing.add("container_*_minutes");
      if (p.bedrock_guardrail_unit_ceiling && ![...tools].some((d) => d.startsWith("guardrail_"))) missing.add("guardrail_*_units");
      if (p.auto_prompt_cache && p.endpoint) {
        for (const d of cacheDimensions(p.endpoint)) if (!tokens.has(d)) missing.add(d);
      }
      return missing.size ? { dimensions: [...missing].join(", ") } : false;
    },
  },
  { message: "pc.relay.rules.modelStreaming", check: (p, ctx) => Boolean(p.streaming && ctx.model && !ctx.model.capabilities?.streaming) },
  { message: "pc.relay.rules.modelTools", check: (p, ctx) => Boolean(p.tools && ctx.model && !ctx.model.capabilities?.tools) },
];

export function relayWarnings(profile: Partial<RelayProfile>, ctx: RelayContext): FieldProblem[] {
  const warnings: FieldProblem[] = [];
  for (const rule of RELAY_RULES) {
    const result = rule.check(profile, ctx);
    if (result) warnings.push({ message: rule.message, vars: typeof result === "object" ? result : undefined });
  }
  return warnings;
}
