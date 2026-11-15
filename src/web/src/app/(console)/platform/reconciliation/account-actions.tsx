"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { LockOpenIcon } from "lucide-react";
import { useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { CopyButton } from "@/components/copy-button";
import { ConflictAlert, ErrorAlert, Spinner } from "@/components/feedback";
import { Money } from "@/components/money";
import { useOrganizationOptions } from "@/components/org-picker";
import { Field } from "@/components/platform-finance/field";
import {
  currencyString,
  nonblank,
  nonnegativeMoney,
  uuidString,
} from "@/components/platform-finance/validation";
import { StatusBadge, toneFor } from "@/components/status-badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import { useIdempotentMutation } from "@/lib/api/hooks";
import { orgKey } from "@/lib/api/queries";
import type { components } from "@/lib/api/schema";

type AccountUnfrozen = components["schemas"]["AccountUnfrozen"];
type ReviewBasis = components["schemas"]["ReviewBasis"];
type Statement = components["schemas"]["Statement"];
type StatementRecorded = components["schemas"]["StatementRecorded"];

const basisSchema = z.object({ reason: nonblank(2000), evidence: nonblank(2000) });
type BasisValues = z.input<typeof basisSchema>;

/** GET …/account with its ETag; the unfreeze command is conditional on that version. */
function useAccountWithEtag(orgId: string) {
  return useQuery({
    queryKey: orgKey(orgId, "account", "with-etag"),
    queryFn: async () =>
      call(() =>
        nexus.GET("/api/v1/organizations/{organization_id}/account", { params: { path: { organization_id: orgId } } }),
      ),
    staleTime: 0,
  });
}

function UnfreezeDialog({ orgId, orgName, onClose }: { orgId: string; orgName: string; onClose: () => void }) {
  const { t, maybeT } = useI18n();
  const queryClient = useQueryClient();
  const account = useAccountWithEtag(orgId);
  const form = useForm<BasisValues>({ resolver: zodResolver(basisSchema), defaultValues: { reason: "", evidence: "" } });
  const ifMatch = account.data ? (account.data.etag ?? `"${account.data.data.version}"`) : null;

  const unfreeze = useIdempotentMutation<AccountUnfrozen, { ifMatch: string; body: ReviewBasis }>({
    keyPrefix: "unfreeze-account",
    mutationFn: async ({ ifMatch: version, body }, idempotencyKey) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/account/unfreeze", {
            params: {
              path: { organization_id: orgId },
              header: { "If-Match": version, "Idempotency-Key": idempotencyKey },
            },
            body,
          }),
        )
      ).data,
    onSuccess: () => {
      toast.success(t("pf.reconciliation.unfrozenToast", { name: orgName }));
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "account") });
      close();
    },
  });
  function close() {
    unfreeze.resetOperation();
    onClose();
  }

  const errors = form.formState.errors;
  const conflict = isConflict(unfreeze.error);
  const data = account.data?.data;
  return (
    <Dialog open onOpenChange={(next) => !next && close()}>
      <DialogContent className="sm:max-w-[480px]">
        <form
          noValidate
          className="grid gap-4"
          onSubmit={form.handleSubmit((values) => {
            if (!ifMatch) return;
            unfreeze.mutate({ ifMatch, body: { reason: values.reason.trim(), evidence: values.evidence.trim() } });
          })}
        >
          <DialogHeader>
            <DialogTitle>{t("pf.reconciliation.unfreezeTitle", { name: orgName })}</DialogTitle>
            <DialogDescription>{t("pf.reconciliation.unfreezeDescription")}</DialogDescription>
          </DialogHeader>
          {conflict ? (
            <ConflictAlert
              onReload={() => {
                unfreeze.resetOperation();
                void account.refetch();
              }}
            />
          ) : unfreeze.error ? (
            <ErrorAlert error={unfreeze.error} />
          ) : null}
          {account.error ? <ErrorAlert error={account.error} onRetry={() => void account.refetch()} /> : null}
          {data ? (
            <div className="flex flex-wrap items-center gap-3 rounded-md border px-3 py-2.5 text-[13px]">
              <StatusBadge tone={data.status === "frozen" ? "danger" : toneFor(data.status)}>
                {maybeT(`pf.common.accountStates.${data.status}`) ?? data.status}
              </StatusBadge>
              <span className="text-muted-foreground">{t("pf.common.available")}</span>
              <Money value={data.available_balance} currency={data.currency} className="font-medium" />
              <span className="ml-auto text-xs text-muted-foreground">v{data.version}</span>
            </div>
          ) : (
            <Skeleton className="h-11 w-full" />
          )}
          {data && data.status !== "frozen" ? (
            <p className="text-sm text-muted-foreground">{t("pf.reconciliation.notFrozen")}</p>
          ) : null}
          <Field id="unfreeze-reason" label={t("pf.common.reason")} error={errors.reason?.message}>
            <Input id="unfreeze-reason" maxLength={2000} {...form.register("reason")} aria-invalid={Boolean(errors.reason) || undefined} />
          </Field>
          <Field
            id="unfreeze-evidence"
            label={t("pf.common.evidence")}
            hint={t("pf.reconciliation.unfreezeEvidenceHint")}
            error={errors.evidence?.message}
          >
            <Textarea
              id="unfreeze-evidence"
              rows={3}
              maxLength={2000}
              {...form.register("evidence")}
              aria-invalid={Boolean(errors.evidence) || undefined}
            />
          </Field>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={close}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={unfreeze.isPending || conflict || !ifMatch || data?.status !== "frozen"}>
              {unfreeze.isPending ? <Spinner /> : null}
              {t("pf.reconciliation.unfreeze")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Account status of the case's organization, with "解除账户限制" when it is frozen. */
export function AccountPanel({ orgId, orgName }: { orgId: string; orgName: string }) {
  const { t, maybeT } = useI18n();
  const account = useAccountWithEtag(orgId);
  const [unfreezing, setUnfreezing] = useState(false);
  const data = account.data?.data;
  if (account.error) return <ErrorAlert error={account.error} onRetry={() => void account.refetch()} />;
  if (!data) return <Skeleton className="h-14 w-full rounded-xl" />;
  const frozen = data.status === "frozen";
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border px-4 py-3">
      <span className="flex flex-col gap-0.5">
        <span className="text-xs text-muted-foreground">{t("pf.common.accountStatus")}</span>
        <StatusBadge tone={frozen ? "danger" : toneFor(data.status)}>
          {maybeT(`pf.common.accountStates.${data.status}`) ?? data.status}
        </StatusBadge>
      </span>
      <span className="flex flex-col gap-0.5">
        <span className="text-xs text-muted-foreground">{t("pf.common.balance")}</span>
        <Money value={data.balance} currency={data.currency} className="font-medium" />
      </span>
      <span className="flex flex-col gap-0.5">
        <span className="text-xs text-muted-foreground">{t("pf.common.available")}</span>
        <Money value={data.available_balance} currency={data.currency} className="font-medium" />
      </span>
      {frozen ? (
        <Button variant="outline" size="sm" className="ml-auto" onClick={() => setUnfreezing(true)}>
          <LockOpenIcon />
          {t("pf.reconciliation.unfreeze")}
        </Button>
      ) : null}
      {unfreezing ? <UnfreezeDialog orgId={orgId} orgName={orgName} onClose={() => setUnfreezing(false)} /> : null}
    </div>
  );
}

const statementSchema = z.object({
  organization_id: z.string().min(1, "required"),
  attempt_id: uuidString(),
  external_reference: nonblank(255),
  amount: nonnegativeMoney(),
  currency: currencyString(),
  reason: nonblank(2000),
  evidence: nonblank(2000),
});
type StatementValues = z.input<typeof statementSchema>;

/** POST /reconciliation-cases/upstream-statements: register an upstream bill for one attempt. */
export function StatementDialog({ defaultOrgId, onClose }: { defaultOrgId: string | undefined; onClose: () => void }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const organizations = useOrganizationOptions();
  const [recorded, setRecorded] = useState<StatementRecorded | null>(null);
  const form = useForm<StatementValues>({
    resolver: zodResolver(statementSchema),
    defaultValues: {
      organization_id: defaultOrgId ?? "",
      attempt_id: "",
      external_reference: "",
      amount: "",
      currency: "USD",
      reason: "",
      evidence: "",
    },
  });
  const record = useIdempotentMutation<StatementRecorded, Statement>({
    keyPrefix: "upstream-statement",
    mutationFn: async (body, idempotencyKey) =>
      (
        await call(() =>
          nexus.POST("/api/v1/reconciliation-cases/upstream-statements", {
            params: { header: { "Idempotency-Key": idempotencyKey } },
            body,
          }),
        )
      ).data,
    onSuccess: (result) => {
      setRecorded(result);
      void queryClient.invalidateQueries({ queryKey: ["reconciliation-cases"] });
    },
  });
  const errors = form.formState.errors;
  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-[540px]">
        {recorded ? (
          <div className="grid gap-4">
            <DialogHeader>
              <DialogTitle>{t("pf.reconciliation.statementRecorded")}</DialogTitle>
              <DialogDescription>{t("pf.reconciliation.statementRecordedBody")}</DialogDescription>
            </DialogHeader>
            <Alert className="border-emerald-200 bg-emerald-50 text-emerald-900">
              <AlertTitle>{t("pf.reconciliation.statementId")}</AlertTitle>
              <AlertDescription className="flex items-center gap-1 text-emerald-900">
                <code className="font-mono text-xs break-all">{recorded.statement_id}</code>
                <CopyButton value={recorded.statement_id} />
              </AlertDescription>
            </Alert>
            <DialogFooter>
              <Button type="button" onClick={onClose}>
                {t("common.done")}
              </Button>
            </DialogFooter>
          </div>
        ) : (
          <form
            noValidate
            className="grid gap-4"
            onSubmit={form.handleSubmit((values) =>
              record.mutate({
                organization_id: values.organization_id,
                attempt_id: values.attempt_id.trim(),
                external_reference: values.external_reference.trim(),
                amount: values.amount.trim(),
                currency: values.currency.trim(),
                reason: values.reason.trim(),
                evidence: values.evidence.trim(),
              }),
            )}
          >
            <DialogHeader>
              <DialogTitle>{t("pf.reconciliation.statementTitle")}</DialogTitle>
              <DialogDescription>{t("pf.reconciliation.statementDescription")}</DialogDescription>
            </DialogHeader>
            {record.error ? <ErrorAlert error={record.error} /> : null}
            <Field id="st-org" label={t("pf.common.organization")} error={errors.organization_id?.message}>
              <Controller
                control={form.control}
                name="organization_id"
                render={({ field, fieldState }) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger id="st-org" className="w-full" aria-invalid={fieldState.invalid || undefined}>
                      <SelectValue placeholder={t("pf.common.selectOrganization")} />
                    </SelectTrigger>
                    <SelectContent>
                      {(organizations.data ?? []).map((org) => (
                        <SelectItem key={org.id} value={org.id}>
                          {org.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
            </Field>
            <Field id="st-attempt" label={t("pf.reconciliation.attemptId")} error={errors.attempt_id?.message}>
              <Input id="st-attempt" className="font-mono text-xs" autoComplete="off" {...form.register("attempt_id")} aria-invalid={Boolean(errors.attempt_id) || undefined} />
            </Field>
            <Field id="st-ref" label={t("pf.reconciliation.statementReference")} error={errors.external_reference?.message}>
              <Input id="st-ref" maxLength={255} className="font-mono" autoComplete="off" {...form.register("external_reference")} aria-invalid={Boolean(errors.external_reference) || undefined} />
            </Field>
            <div className="grid grid-cols-[1fr_96px] gap-3">
              <Field id="st-amount" label={t("pf.reconciliation.statementAmount")} hint={t("pf.reconciliation.statementAmountHint")} error={errors.amount?.message}>
                <Input id="st-amount" inputMode="decimal" autoComplete="off" className="tabular-nums" {...form.register("amount")} aria-invalid={Boolean(errors.amount) || undefined} />
              </Field>
              <Field id="st-currency" label={t("pf.common.currency")} error={errors.currency?.message}>
                <Input
                  id="st-currency"
                  maxLength={3}
                  className="font-mono uppercase"
                  {...form.register("currency", { setValueAs: (value: string) => value.toUpperCase() })}
                  aria-invalid={Boolean(errors.currency) || undefined}
                />
              </Field>
            </div>
            <Field id="st-reason" label={t("pf.common.reason")} error={errors.reason?.message}>
              <Input id="st-reason" maxLength={2000} {...form.register("reason")} aria-invalid={Boolean(errors.reason) || undefined} />
            </Field>
            <Field id="st-evidence" label={t("pf.common.evidence")} error={errors.evidence?.message}>
              <Textarea id="st-evidence" rows={3} maxLength={2000} {...form.register("evidence")} aria-invalid={Boolean(errors.evidence) || undefined} />
            </Field>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={onClose}>
                {t("common.cancel")}
              </Button>
              <Button type="submit" disabled={record.isPending}>
                {record.isPending ? <Spinner /> : null}
                {t("pf.reconciliation.statementSubmit")}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
