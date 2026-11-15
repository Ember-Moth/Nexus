"use client";

import { TriangleAlertIcon, WandSparklesIcon } from "lucide-react";
import { useMemo } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useT } from "@/i18n/provider";
import { cn } from "@/lib/utils";

import { CheckboxGroup, Field, SwitchRow } from "./form";
import {
  fromDraft,
  RELAY_FIELDS,
  RELAY_GROUPS,
  relayWarnings,
  type DraftValue,
  type FieldProblem,
  type RelayContext,
  type RelayDraft,
  type RelayField,
  type RelayKey,
} from "./relay-profile";

const NONE = "__none";

/**
 * Form for deployment.relay_profile generated from RELAY_FIELDS. Values are kept as a draft
 * (see relay-profile.ts); `errors` are per-field messages from the surrounding form's validation.
 */
export function RelayProfileEditor({
  value,
  onChange,
  errors,
  context,
  extraKeys,
  onSuggest,
  idPrefix = "relay",
}: {
  value: RelayDraft;
  onChange: (next: RelayDraft) => void;
  errors?: Partial<Record<RelayKey, FieldProblem>>;
  context: RelayContext;
  extraKeys?: string[];
  onSuggest?: () => void;
  idPrefix?: string;
}) {
  const t = useT();
  const set = (key: string, next: DraftValue) => onChange({ ...value, [key]: next });
  const warnings = useMemo(() => relayWarnings(fromDraft(value).profile, context), [value, context]);

  const renderField = (field: RelayField) => {
    const id = `${idPrefix}-${field.key}`;
    const problem = errors?.[field.key];
    const error = problem ? t(problem.message, problem.vars) : undefined;
    const label = (
      <>
        {t(field.label)}
        {field.required ? <span className="text-destructive"> *</span> : null}
        <code className="ml-1.5 font-mono text-[11px] font-normal text-muted-foreground">{field.key}</code>
      </>
    );
    const hint = field.hint ? t(field.hint) : undefined;
    const raw = value[field.key];

    switch (field.kind) {
      case "boolean":
        return (
          <SwitchRow key={field.key} id={id} label={label} hint={hint} checked={raw === true} onChange={(next) => set(field.key, next)} />
        );
      case "multi":
        return (
          <Field key={field.key} label={label} hint={hint} error={error} className={field.wide ? "sm:col-span-2" : undefined}>
            <CheckboxGroup
              idPrefix={id}
              options={field.options.map((o) => o.value)}
              value={Array.isArray(raw) ? raw : []}
              onChange={(next) => set(field.key, next)}
              label={(option) => {
                const labelKey = field.options.find((o) => o.value === option)?.label;
                return labelKey ? t(labelKey) : <code className="font-mono">{option}</code>;
              }}
              invalid={Boolean(error)}
            />
          </Field>
        );
      case "enum": {
        const current = typeof raw === "string" && raw ? raw : field.required ? "" : NONE;
        return (
          <Field key={field.key} id={id} label={label} hint={hint} error={error} className={field.wide ? "sm:col-span-2" : undefined}>
            <Select value={current} onValueChange={(next) => set(field.key, next === NONE ? "" : next)}>
              <SelectTrigger id={id} className="w-full" aria-invalid={Boolean(error) || undefined}>
                <SelectValue placeholder={t("pc.common.select")} />
              </SelectTrigger>
              <SelectContent>
                {field.required ? null : (
                  <SelectItem value={NONE}>
                    <span className="text-muted-foreground">
                      {field.kind === "enum" && field.default ? `${t("pc.common.notSet")} (${field.default})` : t("pc.common.notSet")}
                    </span>
                  </SelectItem>
                )}
                {field.options.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label ? t(option.label) : <code className="font-mono">{option.value}</code>}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
        );
      }
      case "json":
        return (
          <Field key={field.key} id={id} label={label} hint={hint} error={error} className="sm:col-span-2">
            <Textarea
              id={id}
              rows={3}
              spellCheck={false}
              className="min-h-20 font-mono text-[12.5px]"
              placeholder={field.placeholder}
              aria-invalid={Boolean(error) || undefined}
              value={typeof raw === "string" ? raw : ""}
              onChange={(event) => set(field.key, event.target.value)}
            />
          </Field>
        );
      case "integer":
      case "string":
        return (
          <Field key={field.key} id={id} label={label} hint={hint} error={error} className={field.wide ? "sm:col-span-2" : undefined}>
            <Input
              id={id}
              inputMode={field.kind === "integer" ? "numeric" : undefined}
              className={cn(field.kind === "integer" && "tabular-nums")}
              maxLength={field.kind === "string" ? field.maxLength : 10}
              placeholder={field.kind === "integer" && field.default !== undefined ? String(field.default) : undefined}
              aria-invalid={Boolean(error) || undefined}
              value={typeof raw === "string" ? raw : ""}
              onChange={(event) => set(field.key, event.target.value)}
            />
          </Field>
        );
    }
  };

  return (
    <div className="flex flex-col gap-5">
      {onSuggest ? (
        <Button type="button" variant="outline" size="sm" className="w-fit" onClick={onSuggest}>
          <WandSparklesIcon />
          {t("pc.relay.preset")}
        </Button>
      ) : null}
      {extraKeys && extraKeys.length > 0 ? (
        <p className="rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          {t("pc.relay.extraKeys", { keys: extraKeys.join(", ") })}
        </p>
      ) : null}
      {RELAY_GROUPS.map((group) => {
        const fields = RELAY_FIELDS.filter((field) => field.group === group);
        if (fields.length === 0) return null;
        return (
          <fieldset key={group} className="grid gap-3">
            <legend className="mb-1 text-sm font-semibold">{t(`pc.relay.groups.${group}`)}</legend>
            <div className="grid gap-3 sm:grid-cols-2">{fields.map(renderField)}</div>
          </fieldset>
        );
      })}
      {warnings.length > 0 ? (
        <div role="status" className="flex gap-2.5 rounded-md border border-amber-200 bg-amber-50 px-3 py-2.5 text-[13px] text-amber-900">
          <TriangleAlertIcon className="mt-0.5 size-4 shrink-0 text-amber-700" aria-hidden />
          <div className="flex flex-col gap-1">
            <span className="font-medium">{t("pc.relay.warningsTitle")}</span>
            <ul className="list-disc pl-4">
              {warnings.map((warning) => (
                <li key={warning.message}>{t(warning.message, warning.vars)}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
    </div>
  );
}
