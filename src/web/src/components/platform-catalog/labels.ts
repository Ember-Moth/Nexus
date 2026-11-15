import type { CatalogStatus, ProtocolFamily, TokenDimension, ToolDimension } from "./types";

// Enum values in contract order. Labels live under pc.enums.* and are looked up with typed
// template keys, so a missing translation is a type error.

export const PROTOCOLS: readonly ProtocolFamily[] = ["openai_compatible", "anthropic", "gemini", "aws_bedrock"];

export const STATUSES: readonly CatalogStatus[] = ["active", "disabled", "archived"];

export const TOKEN_DIMENSIONS: readonly TokenDimension[] = [
  "input_uncached_tokens",
  "input_cache_read_tokens",
  "input_cache_write_5m_tokens",
  "input_cache_write_1h_tokens",
  "input_cache_write_30m_tokens",
  "input_cache_write_12h_tokens",
  "output_tokens",
];

/** Required by the model schema and by every price version. */
export const REQUIRED_TOKEN_DIMENSIONS: readonly TokenDimension[] = ["input_uncached_tokens", "output_tokens"];

export const TOOL_DIMENSIONS: readonly ToolDimension[] = [
  "web_search_requests",
  "file_search_calls",
  "vector_storage_byte_seconds",
  "code_execution_seconds",
  "container_1g_minutes",
  "container_4g_minutes",
  "container_16g_minutes",
  "container_64g_minutes",
  "guardrail_content_units",
  "guardrail_image_units",
  "guardrail_topic_units",
  "guardrail_sensitive_units",
  "guardrail_grounding_units",
  "guardrail_reasoning_units",
];

export type AnyDimension = TokenDimension | ToolDimension;

export const isTokenDimension = (name: string): name is TokenDimension =>
  (TOKEN_DIMENSIONS as readonly string[]).includes(name);

export const isToolDimension = (name: string): name is ToolDimension =>
  (TOOL_DIMENSIONS as readonly string[]).includes(name);

/** All billable dimensions of a model, token dimensions first, in contract order. */
export function modelDimensions(model: { token_dimensions?: TokenDimension[]; tool_dimensions?: ToolDimension[] }) {
  const tokens = TOKEN_DIMENSIONS.filter((d) => model.token_dimensions?.includes(d));
  const tools = TOOL_DIMENSIONS.filter((d) => model.tool_dimensions?.includes(d));
  return [...tokens, ...tools] as AnyDimension[];
}

/** Sorts values by their position in the contract enum (the API returns them sorted by name). */
export function inEnumOrder<T extends string>(values: readonly T[], order: readonly T[]): T[] {
  return order.filter((value) => values.includes(value));
}
