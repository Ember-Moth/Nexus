"use client";

import { useMutation, useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";
import { LockIcon } from "lucide-react";
import { useState, type ReactNode } from "react";
import { toast } from "sonner";

import { ConflictAlert, Spinner } from "@/components/feedback";
import { PcErrorAlert } from "@/components/platform-catalog/errors";
import { Field, SwitchRow } from "@/components/platform-catalog/form";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useT, type MessageKey } from "@/i18n/provider";
import type { ApiResult } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";
import { formatDateTime } from "@/lib/format";

type Json = Record<string, unknown>;
type ManagedSource = components["schemas"]["ManagedSource"];

/** One editable policy value, addressed by its dotted path in the PUT body. */
export interface PolicyField {
  path: string;
  label: MessageKey;
  hint?: MessageKey;
  kind: "int" | "float" | "bool" | "lines" | "select" | "currency";
  min?: number;
  max?: number;
  /** An empty value is sent as null (a cluster override that follows the default). */
  optional?: boolean;
  options?: { value: string; label: MessageKey }[];
}

export interface PolicyGroup {
  title?: MessageKey;
  fields: PolicyField[];
}

/** Response fields that are not part of the PUT body. */
const READ_ONLY = new Set(["version", "managed", "cluster_id", "effective", "overrides"]);

function read(view: Json, path: string): unknown {
  return path.split(".").reduce<unknown>((value, key) => (value as Json | undefined)?.[key], view);
}

function write(body: Json, path: string, value: unknown) {
  const keys = path.split(".");
  let node = body;
  for (const key of keys.slice(0, -1)) {
    node[key] = { ...(node[key] as Json) };
    node = node[key] as Json;
  }
  node[keys[keys.length - 1]] = value;
}

function initial(body: Json, fields: PolicyField[]) {
  return Object.fromEntries(
    fields.map((field) => {
      const value = read(body, field.path);
      if (field.kind === "bool") return [field.path, Boolean(value)];
      if (field.kind === "lines") return [field.path, ((value as string[]) ?? []).join("\n")];
      return [field.path, value === null || value === undefined ? "" : String(value)];
    }),
  ) as Record<string, string | boolean>;
}

function parsed(field: PolicyField, raw: string | boolean): { value: unknown; valid: boolean } {
  if (field.kind === "bool" || field.kind === "select") return { value: raw, valid: true };
  if (field.kind === "lines") {
    const lines = String(raw)
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    return { value: lines, valid: lines.length <= 1000 };
  }
  const text = String(raw).trim();
  if (field.kind === "currency") return { value: text, valid: /^[A-Z]{3}$/.test(text) };
  if (field.optional && text === "") return { value: null, valid: true };
  const pattern = field.kind === "int" ? /^\d+$/ : /^\d+(\.\d+)?$/;
  const number = pattern.test(text) ? Number(text) : NaN;
  const valid =
    Number.isFinite(number) && (field.min === undefined || number >= field.min) && (field.max === undefined || number <= field.max);
  return { value: number, valid };
}

/**
 * A policy resource edited as a whole: GET for the values and the ETag, PUT the complete body
 * with If-Match. A stale version (412) asks the operator to reload instead of overwriting. A
 * group locked by a policy file is shown read-only with the file it came from.
 */
export function PolicyCard<T extends { version: number }>({
  title,
  description,
  queryKey,
  load,
  save,
  groups,
  facts,
  footer,
  editable,
  placeholder,
}: {
  title: ReactNode;
  description?: ReactNode;
  queryKey: QueryKey;
  load: () => Promise<ApiResult<T>>;
  save: (body: Json, etag: string) => Promise<ApiResult<T>>;
  groups: PolicyGroup[];
  /** Read-only facts of the loaded policy, shown above the form. */
  facts?: (policy: T) => ReactNode;
  footer?: ReactNode;
  /** The PUT body within the view; by default the view without its read-only fields. */
  editable?: (policy: T) => Json;
  /** Shown in an empty optional field, e.g. the default a cluster follows. */
  placeholder?: (policy: T, path: string) => string | undefined;
}) {
  const t = useT();
  const query = useQuery({ queryKey, queryFn: load, staleTime: 0 });
  const policy = query.data?.data;
  const managed = (policy as { managed?: ManagedSource | null } | undefined)?.managed ?? null;
  const body = (view: T) =>
    editable ? editable(view) : Object.fromEntries(Object.entries(view as Json).filter(([key]) => !READ_ONLY.has(key)));
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {title}
          {managed ? <LockIcon className="size-4 text-muted-foreground" aria-label={t("st.managed.badge")} /> : null}
        </CardTitle>
        {description ? <CardDescription>{description}</CardDescription> : null}
        {policy ? (
          <CardAction>
            <span className="text-xs text-muted-foreground tabular-nums">{t("st.version", { version: policy.version })}</span>
          </CardAction>
        ) : null}
      </CardHeader>
      <CardContent className="grid gap-4">
        {managed ? (
          <Alert>
            <LockIcon />
            <AlertTitle>{t("st.managed.title")}</AlertTitle>
            <AlertDescription>
              {t("st.managed.body", {
                digest: managed.file_sha256.slice(0, 12),
                at: formatDateTime(managed.imported_at),
              })}
            </AlertDescription>
          </Alert>
        ) : null}
        {policy && facts ? facts(policy) : null}
        {query.error && !policy ? (
          <PcErrorAlert error={query.error} onRetry={() => void query.refetch()} />
        ) : policy ? (
          <PolicyForm
            key={policy.version}
            body={body(policy)}
            etag={query.data?.etag ?? `"${policy.version}"`}
            groups={groups}
            queryKey={queryKey}
            save={save}
            readOnly={managed !== null}
            reloading={query.isFetching}
            onReload={() => void query.refetch()}
            placeholder={placeholder ? (path) => placeholder(policy, path) : undefined}
          />
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            <Skeleton className="h-16" />
            <Skeleton className="h-16" />
          </div>
        )}
        {footer}
      </CardContent>
    </Card>
  );
}

function PolicyForm<T>({
  body,
  etag,
  groups,
  queryKey,
  save,
  readOnly,
  reloading,
  onReload,
  placeholder,
}: {
  body: Json;
  etag: string;
  groups: PolicyGroup[];
  queryKey: QueryKey;
  save: (body: Json, etag: string) => Promise<ApiResult<T>>;
  readOnly: boolean;
  reloading: boolean;
  onReload: () => void;
  placeholder?: (path: string) => string | undefined;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const fields = groups.flatMap((group) => group.fields);
  const [values, setValues] = useState(() => initial(body, fields));
  const [touched, setTouched] = useState(false);
  const mutation = useMutation({
    mutationFn: async (next: Json) => save(next, etag),
    onSuccess: () => {
      toast.success(t("st.saved"));
      void queryClient.invalidateQueries({ queryKey });
    },
  });

  const results = Object.fromEntries(fields.map((field) => [field.path, parsed(field, values[field.path])]));
  const valid = fields.every((field) => results[field.path].valid);
  const original = initial(body, fields);
  const changed = fields.some((field) => values[field.path] !== original[field.path]);
  const conflict = isConflict(mutation.error);

  const submit = () => {
    setTouched(true);
    if (!valid) return;
    const next = structuredClone(body);
    for (const field of fields) write(next, field.path, results[field.path].value);
    mutation.mutate(next);
  };
  const set = (path: string, value: string | boolean) => setValues((current) => ({ ...current, [path]: value }));

  return (
    <div className="grid gap-5">
      {conflict ? (
        <ConflictAlert
          onReload={() => {
            mutation.reset();
            onReload();
          }}
        />
      ) : mutation.error ? (
        <PcErrorAlert error={mutation.error} />
      ) : null}
      {groups.map((group, index) => (
        <fieldset key={group.title ?? index} className="grid gap-3" disabled={readOnly}>
          {group.title ? <legend className="mb-1 text-sm font-medium text-muted-foreground">{t(group.title)}</legend> : null}
          <div className="grid gap-x-4 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
            {group.fields.map((field) => {
              const id = `policy-${field.path.replaceAll(".", "-")}`;
              const invalid = touched && !results[field.path].valid;
              const error = invalid
                ? field.kind === "currency"
                  ? t("st.invalidCurrency")
                  : t("st.invalid", { min: field.min ?? 0, max: (field.max ?? 0).toLocaleString() })
                : undefined;
              if (field.kind === "bool") {
                return (
                  <div key={field.path} className="sm:col-span-2 lg:col-span-3">
                    <SwitchRow
                      id={id}
                      label={t(field.label)}
                      hint={field.hint ? t(field.hint) : undefined}
                      checked={Boolean(values[field.path])}
                      disabled={readOnly}
                      onChange={(next) => set(field.path, next)}
                    />
                  </div>
                );
              }
              if (field.kind === "lines") {
                return (
                  <Field
                    key={field.path}
                    id={id}
                    label={t(field.label)}
                    hint={field.hint ? t(field.hint) : undefined}
                    className="sm:col-span-2 lg:col-span-3"
                  >
                    <Textarea
                      id={id}
                      rows={5}
                      value={String(values[field.path])}
                      onChange={(event) => set(field.path, event.target.value)}
                      className="font-mono text-xs"
                    />
                  </Field>
                );
              }
              if (field.kind === "select") {
                return (
                  <Field key={field.path} id={id} label={t(field.label)} hint={field.hint ? t(field.hint) : undefined}>
                    <Select value={String(values[field.path])} onValueChange={(next) => set(field.path, next)} disabled={readOnly}>
                      <SelectTrigger id={id} className="w-full">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {(field.options ?? []).map((option) => (
                          <SelectItem key={option.value} value={option.value}>
                            {t(option.label)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </Field>
                );
              }
              return (
                <Field key={field.path} id={id} label={t(field.label)} hint={field.hint ? t(field.hint) : undefined} error={error}>
                  <Input
                    id={id}
                    inputMode={field.kind === "int" ? "numeric" : field.kind === "float" ? "decimal" : "text"}
                    value={String(values[field.path])}
                    placeholder={placeholder?.(field.path)}
                    onChange={(event) =>
                      set(field.path, field.kind === "currency" ? event.target.value.toUpperCase() : event.target.value)
                    }
                    aria-invalid={invalid || undefined}
                    className={field.kind === "currency" ? "font-mono uppercase" : "tabular-nums"}
                  />
                </Field>
              );
            })}
          </div>
        </fieldset>
      ))}
      {readOnly ? null : (
        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={!changed || mutation.isPending}
            onClick={() => {
              setValues(original);
              setTouched(false);
            }}
          >
            {t("st.reset")}
          </Button>
          <Button type="button" disabled={!changed || mutation.isPending || conflict || reloading} onClick={submit}>
            {mutation.isPending ? <Spinner /> : null}
            {t("st.save")}
          </Button>
        </div>
      )}
    </div>
  );
}
