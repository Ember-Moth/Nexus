"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { PlusIcon, Trash2Icon } from "lucide-react";
import { Controller, useFieldArray, useForm, useWatch } from "react-hook-form";
import { z } from "zod";

import { ConflictAlert, ErrorAlert, Spinner } from "@/components/feedback";
import { DetailList, Field, useFieldErrorText } from "@/components/platform-finance/field";
import { nonblank, positiveMoney, UUID_PATTERN } from "@/components/platform-finance/validation";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import { useIdempotentMutation } from "@/lib/api/hooks";
import { orgKey } from "@/lib/api/queries";
import type { components } from "@/lib/api/schema";
import { formatDateTime, shortId } from "@/lib/format";
import { formatMoney } from "@/lib/money";
import { cn } from "@/lib/utils";

type CaseDetail = components["schemas"]["ReviewCaseDetail"];
type Resolution = components["schemas"]["Resolution"];
type CaseResolution = components["schemas"]["CaseResolution"];
type Action = Resolution["action"];
type SourceType = CaseDetail["source_type"];

/** Actions the service accepts for each case source (see billing operations). */
export const ACTIONS_BY_SOURCE: Record<SourceType, Action[]> = {
  request: ["confirm_no_charge", "supplement_usage", "waive", "retry"],
  attempt: ["confirm_cost", "supplement_usage", "retry"],
  payment: ["verify_payment", "retry"],
  account: ["retry"],
};

/** Metering dimensions accepted in `quantities` (billing DIMENSIONS). */
const DIMENSIONS = [
  "input_uncached_tokens",
  "input_cache_read_tokens",
  "input_cache_write_5m_tokens",
  "input_cache_write_1h_tokens",
  "input_cache_write_30m_tokens",
  "input_cache_write_12h_tokens",
  "output_tokens",
  "web_search_requests",
  "file_search_calls",
  "code_execution_seconds",
  "container_1g_minutes",
  "container_4g_minutes",
  "container_16g_minutes",
  "container_64g_minutes",
  "vector_storage_byte_seconds",
  "guardrail_content_units",
  "guardrail_image_units",
  "guardrail_topic_units",
  "guardrail_sensitive_units",
  "guardrail_grounding_units",
  "guardrail_reasoning_units",
] as const;

const usageActions = new Set<Action>(["supplement_usage", "waive"]);

const schema = z
  .object({
    action: z.string().min(1, "required"),
    reason: nonblank(2000),
    evidence: nonblank(2000),
    attempt_id: z.string(),
    quantities: z.array(z.object({ dimension: z.string(), value: z.string() })),
    normalization_version: z.string(),
    waiver: z.string(),
    observation_id: z.string(),
    statement_id: z.string(),
  })
  .superRefine((value, ctx) => {
    const action = value.action as Action;
    const uuid = (field: "attempt_id" | "observation_id" | "statement_id") => {
      if (!UUID_PATTERN.test(value[field].trim())) ctx.addIssue({ code: "custom", path: [field], message: "uuid" });
    };
    if (usageActions.has(action)) {
      uuid("attempt_id");
      const version = value.normalization_version.trim();
      if (!version) ctx.addIssue({ code: "custom", path: ["normalization_version"], message: "required" });
      else if (version.length > 80) ctx.addIssue({ code: "custom", path: ["normalization_version"], message: "tooLong" });
      if (value.quantities.length === 0) ctx.addIssue({ code: "custom", path: ["quantities"], message: "quantitiesRequired" });
      const seen = new Set<string>();
      value.quantities.forEach((row, index) => {
        if (!row.dimension) ctx.addIssue({ code: "custom", path: ["quantities", index, "dimension"], message: "required" });
        else if (seen.has(row.dimension))
          ctx.addIssue({ code: "custom", path: ["quantities", index, "dimension"], message: "duplicate" });
        seen.add(row.dimension);
        if (!/^\d+(\.\d+)?$/.test(row.value.trim()))
          ctx.addIssue({ code: "custom", path: ["quantities", index, "value"], message: "format" });
      });
    }
    if (action === "waive") {
      const problem = positiveMoney().safeParse(value.waiver);
      if (!problem.success) ctx.addIssue({ code: "custom", path: ["waiver"], message: problem.error.issues[0]?.message ?? "format" });
    }
    if (action === "confirm_cost") uuid("statement_id");
    if (action === "verify_payment") uuid("observation_id");
  });
type Values = z.input<typeof schema>;

/** Whole numbers are sent as JSON integers (token counters); anything else stays a string. */
function quantityValue(raw: string): number | string {
  const value = raw.trim();
  if (/^\d+$/.test(value)) {
    const number = Number(value);
    if (Number.isSafeInteger(number)) return number;
  }
  return value;
}

/** The service rejects any field not required by the action, so only those are sent. */
function toResolution(values: Values): Resolution {
  const action = values.action as Action;
  const body: Resolution = { action, reason: values.reason.trim(), evidence: values.evidence.trim() };
  if (usageActions.has(action)) {
    body.attempt_id = values.attempt_id.trim();
    body.normalization_version = values.normalization_version.trim();
    body.quantities = Object.fromEntries(values.quantities.map((row) => [row.dimension, quantityValue(row.value)]));
  }
  if (action === "waive") body.waiver = values.waiver.trim();
  if (action === "confirm_cost") body.statement_id = values.statement_id.trim();
  if (action === "verify_payment") body.observation_id = values.observation_id.trim();
  return body;
}

const stringAt = (value: unknown, key: string): string =>
  typeof value === "object" && value !== null && !Array.isArray(value) && typeof (value as Record<string, unknown>)[key] === "string"
    ? ((value as Record<string, unknown>)[key] as string)
    : "";

function defaultsFor(detail: CaseDetail): Values {
  const usage = detail.usage_evidence?.[0];
  const quantities = Object.entries(usage?.quantities ?? {})
    .filter(([dimension]) => (DIMENSIONS as readonly string[]).includes(dimension))
    .map(([dimension, value]) => ({ dimension, value: String(value ?? "") }));
  return {
    action: ACTIONS_BY_SOURCE[detail.source_type][0],
    reason: "",
    evidence: "",
    attempt_id: detail.source_type === "attempt" ? detail.source_id : stringAt(detail.evidence, "attempt_id"),
    quantities: quantities.length ? quantities : [{ dimension: "", value: "" }],
    normalization_version: usage?.rule_version ?? "",
    waiver: "",
    observation_id: stringAt(detail.evidence, "observation_id"),
    statement_id: stringAt(detail.evidence, "statement_id"),
  };
}

/** Observations of the case's payment order, to pick the one being verified. */
function ObservationSelect({
  orgId,
  orderId,
  value,
  onChange,
  invalid,
}: {
  orgId: string;
  orderId: string;
  value: string;
  onChange: (value: string) => void;
  invalid?: boolean;
}) {
  const { t, maybeT } = useI18n();
  const observations = useQuery({
    queryKey: orgKey(orgId, "payment-orders", orderId, "observations", "options"),
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/payment-orders/{order_id}/observations", {
            params: { path: { organization_id: orgId, order_id: orderId }, query: { limit: 100 } },
          }),
        )
      ).data.items,
  });
  if (observations.error || (observations.data && observations.data.length === 0)) {
    return (
      <div className="grid gap-1.5">
        <Input
          id="resolve-observation"
          className="font-mono text-xs"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder="00000000-0000-0000-0000-000000000000"
          aria-invalid={invalid || undefined}
        />
        {observations.error ? <ErrorAlert error={observations.error} onRetry={() => void observations.refetch()} /> : null}
      </div>
    );
  }
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger id="resolve-observation" className="w-full" aria-invalid={invalid || undefined}>
        <SelectValue placeholder={observations.isPending ? t("common.loading") : t("pf.reconciliation.pickObservation")} />
      </SelectTrigger>
      <SelectContent>
        {(observations.data ?? []).map((observation) => (
          <SelectItem key={observation.id} value={observation.id}>
            <span className="tabular-nums">{formatDateTime(observation.observed_at)}</span>
            <span className="text-xs text-muted-foreground">
              {maybeT(`pf.paymentCases.observationSources.${observation.source}`) ?? observation.source} ·{" "}
              {maybeT(`pf.paymentCases.dispositions.${observation.disposition}`) ?? observation.disposition} ·{" "}
              {shortId(observation.id)}
            </span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function ResolutionResult({ result }: { result: CaseResolution }) {
  const { t, maybeT } = useI18n();
  const rows = Object.entries(result).filter(
    ([key, value]) => value !== null && value !== undefined && key !== "case_id" && key !== "action",
  );
  return (
    <Alert className="border-emerald-200 bg-emerald-50 text-emerald-900">
      <AlertTitle>{t("pf.reconciliation.resolvedTitle", { action: t(`pf.reconciliation.actions.${result.action}`) })}</AlertTitle>
      <AlertDescription className="text-emerald-900">
        {result.processing_state === "queued" ? <p>{t("pf.reconciliation.queuedNote")}</p> : null}
        <DetailList
          className="mt-1"
          items={rows.map(([key, value]) => [
            maybeT(`pf.reconciliation.resultFields.${key}`) ?? key,
            /amount$/.test(key) && typeof value === "string" ? (
              <span key={key} className="tabular-nums">
                {formatMoney(value)}
              </span>
            ) : (
              <code key={key} className="font-mono text-xs break-all">
                {String(value)}
              </code>
            ),
          ])}
        />
      </AlertDescription>
    </Alert>
  );
}

export function ResolveForm({
  detail,
  ifMatch,
  onReload,
  onResolved,
}: {
  detail: CaseDetail;
  ifMatch: string;
  onReload: () => void;
  onResolved: (result: CaseResolution) => void;
}) {
  const { t } = useI18n();
  const describe = useFieldErrorText();
  const queryClient = useQueryClient();
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: defaultsFor(detail) });
  const quantities = useFieldArray({ control: form.control, name: "quantities" });
  const action = useWatch({ control: form.control, name: "action" }) as Action;
  const actions = ACTIONS_BY_SOURCE[detail.source_type];

  // Idempotency-Key per decision; the If-Match version is part of the fingerprint, so a reload
  // after 412 starts a new operation.
  const resolve = useIdempotentMutation<CaseResolution, { ifMatch: string; body: Resolution }>({
    keyPrefix: "resolve-case",
    mutationFn: async ({ ifMatch: version, body }, idempotencyKey) =>
      (
        await call(() =>
          nexus.POST("/api/v1/reconciliation-cases/{case_id}/resolve", {
            params: {
              path: { case_id: detail.id },
              header: { "If-Match": version, "Idempotency-Key": idempotencyKey },
            },
            body,
          }),
        )
      ).data,
    onSuccess: (result) => {
      onResolved(result);
      void queryClient.invalidateQueries({ queryKey: ["reconciliation-cases"] });
      void queryClient.invalidateQueries({ queryKey: orgKey(detail.organization_id, "account") });
    },
  });

  const errors = form.formState.errors;
  const conflict = isConflict(resolve.error);
  const usage = usageActions.has(action);

  return (
    <form
      noValidate
      className="flex flex-col gap-4 rounded-xl border p-4"
      onSubmit={form.handleSubmit((values) => resolve.mutate({ ifMatch, body: toResolution(values) }))}
      aria-labelledby="resolve-title"
    >
      <div className="flex flex-col gap-1">
        <h3 id="resolve-title" className="text-[15px] font-semibold">
          {t("pf.reconciliation.resolveTitle")}
        </h3>
        <p className="text-xs text-muted-foreground">{t("pf.reconciliation.resolveHint")}</p>
      </div>
      {conflict ? (
        <ConflictAlert
          onReload={() => {
            resolve.resetOperation();
            onReload();
          }}
        />
      ) : resolve.error ? (
        <ErrorAlert error={resolve.error} />
      ) : null}
      <fieldset className="grid gap-2">
        <legend className="mb-1.5 text-[13px] font-medium">{t("pf.reconciliation.action")}</legend>
        <Controller
          control={form.control}
          name="action"
          render={({ field }) => (
            <RadioGroup value={field.value} onValueChange={field.onChange} className="grid gap-2 sm:grid-cols-2">
              {actions.map((value) => (
                <Label
                  key={value}
                  className={cn(
                    "items-start rounded-lg border px-3 py-2.5 font-normal",
                    field.value === value && "border-foreground bg-muted/40",
                  )}
                >
                  <RadioGroupItem value={value} className="mt-0.5" />
                  <span className="flex flex-col gap-0.5">
                    <span className="font-medium">{t(`pf.reconciliation.actions.${value}`)}</span>
                    <span className="text-xs leading-relaxed text-muted-foreground">
                      {t(`pf.reconciliation.actionHelp.${value}`)}
                    </span>
                  </span>
                </Label>
              ))}
            </RadioGroup>
          )}
        />
      </fieldset>

      {usage ? (
        <div className="grid gap-3 rounded-lg border bg-muted/20 p-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field
              id="resolve-attempt"
              label={t("pf.reconciliation.attemptId")}
              hint={detail.source_type === "attempt" ? t("pf.reconciliation.attemptIsSource") : undefined}
              error={errors.attempt_id?.message}
            >
              <Input
                id="resolve-attempt"
                className="font-mono text-xs"
                autoComplete="off"
                {...form.register("attempt_id")}
                aria-invalid={Boolean(errors.attempt_id) || undefined}
              />
            </Field>
            <Field
              id="resolve-normalization"
              label={t("pf.reconciliation.normalizationVersion")}
              hint={t("pf.reconciliation.normalizationHint")}
              error={errors.normalization_version?.message}
            >
              <Input
                id="resolve-normalization"
                className="font-mono text-xs"
                maxLength={80}
                autoComplete="off"
                {...form.register("normalization_version")}
                aria-invalid={Boolean(errors.normalization_version) || undefined}
              />
            </Field>
          </div>
          <fieldset className="grid gap-2">
            <legend className="mb-1 text-[13px] font-medium">{t("pf.reconciliation.quantities")}</legend>
            <p className="text-xs text-muted-foreground">{t("pf.reconciliation.quantitiesHint")}</p>
            {quantities.fields.map((row, index) => {
              const rowErrors = errors.quantities?.[index];
              return (
                <div key={row.id} className="grid grid-cols-[1fr_140px_32px] items-start gap-2">
                  <Controller
                    control={form.control}
                    name={`quantities.${index}.dimension`}
                    render={({ field, fieldState }) => (
                      <Select value={field.value} onValueChange={field.onChange}>
                        <SelectTrigger
                          className="w-full font-mono text-xs"
                          aria-label={`${t("pf.reconciliation.dimension")} ${index + 1}`}
                          aria-invalid={fieldState.invalid || undefined}
                        >
                          <SelectValue placeholder={t("pf.reconciliation.dimension")} />
                        </SelectTrigger>
                        <SelectContent>
                          {DIMENSIONS.map((dimension) => (
                            <SelectItem key={dimension} value={dimension} className="font-mono text-xs">
                              {dimension}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  />
                  <Input
                    inputMode="decimal"
                    className="tabular-nums"
                    aria-label={`${t("pf.reconciliation.quantity")} ${index + 1}`}
                    {...form.register(`quantities.${index}.value`)}
                    aria-invalid={Boolean(rowErrors?.value) || undefined}
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label={t("pf.reconciliation.removeQuantity")}
                    onClick={() => quantities.remove(index)}
                    disabled={quantities.fields.length === 1}
                  >
                    <Trash2Icon />
                  </Button>
                  {rowErrors?.dimension || rowErrors?.value ? (
                    <span role="alert" className="col-span-3 text-xs text-destructive">
                      {describe(rowErrors.dimension?.message ?? rowErrors.value?.message)}
                    </span>
                  ) : null}
                </div>
              );
            })}
            {errors.quantities?.message ? (
              <span role="alert" className="text-xs text-destructive">
                {describe(errors.quantities.message)}
              </span>
            ) : null}
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="justify-self-start"
              onClick={() => quantities.append({ dimension: "", value: "" })}
              disabled={quantities.fields.length >= DIMENSIONS.length}
            >
              <PlusIcon />
              {t("pf.reconciliation.addQuantity")}
            </Button>
          </fieldset>
          {action === "waive" ? (
            <Field id="resolve-waiver" label={t("pf.reconciliation.waiver")} hint={t("pf.reconciliation.waiverHint")} error={errors.waiver?.message}>
              <Input
                id="resolve-waiver"
                inputMode="decimal"
                autoComplete="off"
                className="tabular-nums"
                {...form.register("waiver")}
                aria-invalid={Boolean(errors.waiver) || undefined}
              />
            </Field>
          ) : null}
        </div>
      ) : null}

      {action === "confirm_cost" ? (
        <Field
          id="resolve-statement"
          label={t("pf.reconciliation.statementId")}
          hint={t("pf.reconciliation.statementHint")}
          error={errors.statement_id?.message}
        >
          <Input
            id="resolve-statement"
            className="font-mono text-xs"
            autoComplete="off"
            {...form.register("statement_id")}
            aria-invalid={Boolean(errors.statement_id) || undefined}
          />
        </Field>
      ) : null}

      {action === "verify_payment" ? (
        <Field
          id="resolve-observation"
          label={t("pf.reconciliation.observationId")}
          hint={t("pf.reconciliation.observationHint")}
          error={errors.observation_id?.message}
        >
          <Controller
            control={form.control}
            name="observation_id"
            render={({ field, fieldState }) => (
              <ObservationSelect
                orgId={detail.organization_id}
                orderId={detail.source_id}
                value={field.value}
                onChange={field.onChange}
                invalid={fieldState.invalid}
              />
            )}
          />
        </Field>
      ) : null}

      <Field id="resolve-reason" label={t("pf.common.reason")} error={errors.reason?.message}>
        <Input id="resolve-reason" maxLength={2000} {...form.register("reason")} aria-invalid={Boolean(errors.reason) || undefined} />
      </Field>
      <Field id="resolve-evidence" label={t("pf.common.evidence")} hint={t("pf.reconciliation.evidenceHint")} error={errors.evidence?.message}>
        <Textarea
          id="resolve-evidence"
          rows={3}
          maxLength={2000}
          {...form.register("evidence")}
          aria-invalid={Boolean(errors.evidence) || undefined}
        />
      </Field>
      <div className="flex flex-wrap items-center justify-end gap-3">
        <span className="mr-auto text-xs text-muted-foreground">
          {t("pf.reconciliation.versionNote", { version: detail.version })}
        </span>
        <Button type="submit" disabled={resolve.isPending || conflict}>
          {resolve.isPending ? <Spinner /> : null}
          {t("pf.reconciliation.submit")}
        </Button>
      </div>
    </form>
  );
}

export { ResolutionResult };
