"use client";

import type { FieldErrors, UseFormRegister } from "react-hook-form";
import { Controller, type Control } from "react-hook-form";

import { Field } from "@/components/platform-finance/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/i18n/provider";

// Adapter configuration forms are generated from the JSON schema returned by
// GET /payment-gateway-types. Strings, enums, numbers, booleans and string lists get fields;
// any other construct switches the whole configuration to a raw JSON editor.

type JsonSchema = Record<string, unknown>;

export type ConfigFieldKind = "string" | "text" | "secret" | "enum" | "integer" | "number" | "boolean" | "list";

export interface ConfigField {
  name: string;
  title: string;
  kind: ConfigFieldKind;
  required: boolean;
  enumValues?: string[];
  pattern?: string;
  minLength?: number;
  maxLength?: number;
  minimum?: number;
  maximum?: number;
  minItems?: number;
  maxItems?: number;
  defaultValue?: unknown;
}

export interface ConfigSpec {
  fields: ConfigField[];
  /** False when the schema uses constructs the generated form cannot represent. */
  supported: boolean;
  secretNames: Set<string>;
}

export type ConfigValues = Record<string, string | boolean>;

const isObject = (value: unknown): value is JsonSchema => typeof value === "object" && value !== null && !Array.isArray(value);

/** Long PEM-like values get a textarea; everything else a single-line input. */
const MULTILINE = /public_key|certificate|pem/i;

/** Unwraps Pydantic's `anyOf: [X, {type: null}]` for optional fields. */
function unwrapNullable(schema: JsonSchema): JsonSchema | null {
  const anyOf = schema.anyOf;
  if (!Array.isArray(anyOf)) return schema;
  const branches = anyOf.filter((branch) => !(isObject(branch) && branch.type === "null"));
  return branches.length === 1 && isObject(branches[0]) ? { ...branches[0], title: schema.title, default: schema.default } : null;
}

export function describeConfigSchema(schema: unknown): ConfigSpec {
  const fields: ConfigField[] = [];
  const secretNames = new Set<string>();
  if (!isObject(schema) || schema.type !== "object" || !isObject(schema.properties)) {
    return { fields, supported: false, secretNames };
  }
  const required = new Set(Array.isArray(schema.required) ? (schema.required as string[]) : []);
  let supported = true;
  for (const [name, raw] of Object.entries(schema.properties)) {
    const property = isObject(raw) ? unwrapNullable(raw) : null;
    if (!property) {
      supported = false;
      continue;
    }
    const base: ConfigField = {
      name,
      title: typeof property.title === "string" ? property.title : name,
      kind: "string",
      required: required.has(name),
      defaultValue: property.default,
    };
    const type = property.type;
    if (type === "string") {
      if (Array.isArray(property.enum)) {
        fields.push({ ...base, kind: "enum", enumValues: property.enum.map(String) });
      } else {
        const secret = property.format === "password" || property.writeOnly === true;
        if (secret) secretNames.add(name);
        fields.push({
          ...base,
          kind: secret ? "secret" : MULTILINE.test(name) ? "text" : "string",
          pattern: typeof property.pattern === "string" ? property.pattern : undefined,
          minLength: typeof property.minLength === "number" ? property.minLength : undefined,
          maxLength: typeof property.maxLength === "number" ? property.maxLength : undefined,
        });
      }
    } else if (type === "integer" || type === "number") {
      fields.push({
        ...base,
        kind: type,
        minimum: typeof property.minimum === "number" ? property.minimum : undefined,
        maximum: typeof property.maximum === "number" ? property.maximum : undefined,
      });
    } else if (type === "boolean") {
      fields.push({ ...base, kind: "boolean" });
    } else if (type === "array" && isObject(property.items) && property.items.type === "string") {
      fields.push({
        ...base,
        kind: "list",
        minItems: typeof property.minItems === "number" ? property.minItems : undefined,
        maxItems: typeof property.maxItems === "number" ? property.maxItems : undefined,
      });
    } else {
      supported = false;
    }
  }
  return { fields, supported, secretNames };
}

/** Initial form values: non-secret fields from the current public configuration, else defaults. */
export function initialConfigValues(spec: ConfigSpec, current?: Record<string, unknown>): ConfigValues {
  const values: ConfigValues = {};
  for (const field of spec.fields) {
    const source = field.kind === "secret" ? undefined : (current?.[field.name] ?? field.defaultValue);
    if (field.kind === "boolean") values[field.name] = source === true;
    else if (field.kind === "list") values[field.name] = Array.isArray(source) ? source.map(String).join("\n") : "";
    else values[field.name] = source === undefined || source === null || field.kind === "secret" ? "" : String(source);
  }
  return values;
}

/** Validates the form values and builds the configuration object sent to the API. */
export function buildConfiguration(
  spec: ConfigSpec,
  values: ConfigValues,
): { configuration: Record<string, unknown>; errors: Record<string, string> } {
  const configuration: Record<string, unknown> = {};
  const errors: Record<string, string> = {};
  for (const field of spec.fields) {
    const raw = values[field.name];
    if (field.kind === "boolean") {
      configuration[field.name] = raw === true;
      continue;
    }
    const text = typeof raw === "string" ? raw.trim() : "";
    if (field.kind === "list") {
      const items = text
        .split(/[\n,]/)
        .map((item) => item.trim())
        .filter(Boolean);
      if (items.length === 0) {
        if (field.required) errors[field.name] = "required";
        continue;
      }
      if (field.minItems !== undefined && items.length < field.minItems) errors[field.name] = "listTooShort";
      else if (field.maxItems !== undefined && items.length > field.maxItems) errors[field.name] = "listTooLong";
      configuration[field.name] = items;
      continue;
    }
    if (!text) {
      if (field.required) errors[field.name] = "required";
      continue;
    }
    if (field.kind === "integer" || field.kind === "number") {
      const valid = field.kind === "integer" ? /^-?\d+$/.test(text) : /^-?\d+(\.\d+)?$/.test(text);
      const number = Number(text);
      if (!valid || !Number.isFinite(number)) errors[field.name] = "format";
      else if (field.minimum !== undefined && number < field.minimum) errors[field.name] = "tooSmall";
      else if (field.maximum !== undefined && number > field.maximum) errors[field.name] = "tooLarge";
      else configuration[field.name] = number;
      continue;
    }
    if (field.kind === "enum" && field.enumValues && !field.enumValues.includes(text)) {
      errors[field.name] = "format";
      continue;
    }
    if (field.pattern) {
      let pattern: RegExp | null = null;
      try {
        pattern = new RegExp(field.pattern);
      } catch {
        pattern = null; // A server-side pattern JS cannot compile is left to the API.
      }
      if (pattern && !pattern.test(text)) {
        errors[field.name] = "pattern";
        continue;
      }
    }
    if (field.minLength !== undefined && text.length < field.minLength) errors[field.name] = "tooShort";
    else if (field.maxLength !== undefined && text.length > field.maxLength) errors[field.name] = "tooLong";
    else configuration[field.name] = text;
  }
  return { configuration, errors };
}

/** Removes secret fields from a configuration shown to the user (defence in depth). */
export function redactConfiguration(spec: ConfigSpec | null, value: Record<string, unknown>) {
  const secretLike = /secret|private_key|merchant_key|api_key|password|token/i;
  return Object.fromEntries(
    Object.entries(value).filter(([key]) => !(spec?.secretNames.has(key) ?? false) && !secretLike.test(key)),
  );
}

type FormWithConfig = { config: ConfigValues };

/** Renders the generated fields bound to `config.<name>` of the surrounding form. */
export function ConfigFields<T extends FormWithConfig>({
  spec,
  control,
  register,
  errors,
  idPrefix,
}: {
  spec: ConfigSpec;
  control: Control<T>;
  register: UseFormRegister<T>;
  errors: FieldErrors<T>;
  idPrefix: string;
}) {
  const { t, maybeT } = useI18n();
  // The generic form type cannot express `config.${string}` paths; narrow once here.
  const c = control as unknown as Control<FormWithConfig>;
  const r = register as unknown as UseFormRegister<FormWithConfig>;
  const configErrors = (errors as FieldErrors<FormWithConfig>).config as Record<string, { message?: string } | undefined> | undefined;
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {spec.fields.map((field) => {
        const id = `${idPrefix}-${field.name}`;
        const error = configErrors?.[field.name]?.message;
        const label = (
          <span className="inline-flex flex-wrap items-baseline gap-1.5">
            {maybeT(`pf.channels.configFields.${field.name}`) ?? field.title}
            <code className="font-mono text-[11px] font-normal text-muted-foreground">{field.name}</code>
            {field.required ? null : <span className="text-[11px] font-normal text-muted-foreground">{t("pf.channels.optional")}</span>}
          </span>
        );
        const hint =
          field.kind === "secret"
            ? t("pf.channels.secretHint")
            : field.kind === "list"
              ? t("pf.channels.listHint")
              : field.pattern
                ? t("pf.channels.patternHint", { pattern: field.pattern })
                : field.defaultValue !== undefined && field.defaultValue !== null
                  ? t("pf.channels.defaultHint", { value: JSON.stringify(field.defaultValue) })
                  : undefined;
        const wide = field.kind === "text" || field.kind === "list" || field.kind === "secret";
        return (
          <Field key={field.name} id={id} label={label} hint={hint} error={error} className={wide ? "sm:col-span-2" : undefined}>
            {field.kind === "enum" ? (
              <Controller
                control={c}
                name={`config.${field.name}`}
                render={({ field: input, fieldState }) => (
                  <Select value={String(input.value ?? "")} onValueChange={input.onChange}>
                    <SelectTrigger id={id} className="w-full font-mono text-xs" aria-invalid={fieldState.invalid || undefined}>
                      <SelectValue placeholder={t("pf.channels.selectValue")} />
                    </SelectTrigger>
                    <SelectContent>
                      {(field.enumValues ?? []).map((value) => (
                        <SelectItem key={value} value={value} className="font-mono text-xs">
                          {value}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
            ) : field.kind === "boolean" ? (
              <Controller
                control={c}
                name={`config.${field.name}`}
                render={({ field: input }) => (
                  <Switch id={id} checked={input.value === true} onCheckedChange={input.onChange} />
                )}
              />
            ) : field.kind === "secret" ? (
              <Input
                id={id}
                type="password"
                autoComplete="new-password"
                spellCheck={false}
                className="font-mono text-xs"
                {...r(`config.${field.name}`)}
                aria-invalid={Boolean(error) || undefined}
              />
            ) : field.kind === "text" || field.kind === "list" ? (
              <Textarea
                id={id}
                rows={field.kind === "list" ? 2 : 3}
                spellCheck={false}
                className="font-mono text-xs"
                {...r(`config.${field.name}`)}
                aria-invalid={Boolean(error) || undefined}
              />
            ) : (
              <Input
                id={id}
                inputMode={field.kind === "integer" || field.kind === "number" ? "decimal" : undefined}
                autoComplete="off"
                spellCheck={false}
                className="font-mono text-xs"
                {...r(`config.${field.name}`)}
                aria-invalid={Boolean(error) || undefined}
              />
            )}
          </Field>
        );
      })}
    </div>
  );
}
