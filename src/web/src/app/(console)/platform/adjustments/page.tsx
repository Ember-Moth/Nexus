"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CheckCheckIcon, FileTextIcon, PlusIcon, SlidersHorizontalIcon } from "lucide-react";
import { Suspense, useMemo, useState } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { CopyButton } from "@/components/copy-button";
import { DataTable } from "@/components/data-table";
import { EmptyState, ErrorAlert, Spinner } from "@/components/feedback";
import { Money } from "@/components/money";
import { OrgPicker, useOrgParam } from "@/components/org-picker";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { AccountStrip } from "@/components/platform-finance/account-strip";
import { DetailList, Field, ReadOnlyValue } from "@/components/platform-finance/field";
import { Actor, PickOrganization, useUserLabels } from "@/components/platform-finance/scope";
import { absMoney, nonblank, positiveMoney } from "@/components/platform-finance/validation";
import { ConfirmWriteDialog } from "@/components/platform-finance/write-dialog";
import { StatusBadge, type Tone } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useCursorList, useIdempotentMutation } from "@/lib/api/hooks";
import { orgKey } from "@/lib/api/queries";
import type { components } from "@/lib/api/schema";
import { formatDateTime, shortId } from "@/lib/format";
import { compareMoney, formatMoney } from "@/lib/money";
import { cn } from "@/lib/utils";

type Adjustment = components["schemas"]["AdjustmentView"];
type AdjustmentCreate = components["schemas"]["AdjustmentCreate"];
type LedgerRecord = components["schemas"]["LedgerRecord"];
type LedgerEntry = components["schemas"]["LedgerView"];

/** Adjustments may only reverse an original top-up or refund an original usage charge. */
const ADJUSTABLE = new Set(["funding_credit", "usage_debit"]);
const stateTone: Record<string, Tone> = { draft: "info", posted: "success" };

function useLedgerEntry(orgId: string, entryId: string | undefined) {
  return useQuery({
    queryKey: orgKey(orgId, "ledger", "entry", entryId),
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/ledger/{ledger_entry_id}", {
            params: { path: { organization_id: orgId, ledger_entry_id: entryId! } },
          }),
        )
      ).data,
    enabled: Boolean(entryId),
    staleTime: 60_000,
  });
}

function KindLabel({ kind }: { kind: string }) {
  const { maybeT } = useI18n();
  return <>{maybeT(`wallet.kinds.${kind}`) ?? kind}</>;
}

/** What posting does to the account, by the kind of the referenced entry. */
function EffectNote({ entry }: { entry: Pick<LedgerEntry, "kind"> | undefined }) {
  const { t } = useI18n();
  if (!entry) return null;
  return (
    <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-900">
      {entry.kind === "funding_credit" ? t("pf.adjustments.effectFunding") : t("pf.adjustments.effectUsage")}
    </p>
  );
}

function SourcePicker({
  orgId,
  value,
  onChange,
  invalid,
}: {
  orgId: string;
  value: string;
  onChange: (entry: LedgerRecord) => void;
  invalid?: boolean;
}) {
  const { t } = useI18n();
  const ledger = useCursorList<LedgerRecord>({
    queryKey: orgKey(orgId, "ledger", "adjustable"),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/ledger", {
            params: { path: { organization_id: orgId }, query: { limit: 100, cursor, time_basis: "posted_at" } },
          }),
        )
      ).data,
  });
  // The ledger cannot be filtered by kind on the server; non-adjustable rows are skipped here.
  const entries = ledger.items.filter((entry) => ADJUSTABLE.has(entry.kind));
  return (
    <div className={cn("overflow-hidden rounded-md border", invalid && "border-destructive")}>
      {ledger.error ? <ErrorAlert error={ledger.error} onRetry={() => void ledger.refetch()} className="m-2 w-auto" /> : null}
      {ledger.isPending ? (
        <Skeleton className="m-2 h-24" />
      ) : entries.length === 0 ? (
        <p className="px-3 py-4 text-sm text-muted-foreground">{t("pf.adjustments.noSources")}</p>
      ) : (
        <RadioGroup
          value={value}
          onValueChange={(id) => {
            const entry = entries.find((item) => item.id === id);
            if (entry) onChange(entry);
          }}
          aria-label={t("pf.adjustments.source")}
          className="max-h-60 gap-0 overflow-y-auto"
        >
          {entries.map((entry) => (
            <Label
              key={entry.id}
              className={cn(
                "grid grid-cols-[16px_1fr_auto] items-center gap-x-3 border-b px-3 py-2 font-normal last:border-b-0",
                entry.id === value && "bg-muted/60",
              )}
            >
              <RadioGroupItem value={entry.id} />
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="text-[13px] font-medium">
                  <KindLabel kind={entry.kind} />
                  <span className="ml-2 font-mono text-xs font-normal text-muted-foreground">#{entry.sequence}</span>
                </span>
                <span className="truncate text-xs text-muted-foreground tabular-nums">
                  {formatDateTime(entry.posted_at, { seconds: false })} · {entry.source_type} {shortId(entry.source_id)}
                </span>
              </span>
              <Money value={entry.signed_amount} currency={entry.currency} signed className="text-[13px] font-medium" />
            </Label>
          ))}
        </RadioGroup>
      )}
      {ledger.hasNextPage ? (
        <div className="border-t px-3 py-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={ledger.isFetchingNextPage}
            onClick={() => void ledger.fetchNextPage()}
          >
            {ledger.isFetchingNextPage ? t("common.loading") : t("pf.adjustments.moreSources")}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

const schema = z.object({
  source_entry_id: z.string().min(1, "required"),
  amount: positiveMoney(),
  reason: nonblank(2000),
  evidence: nonblank(2000),
});
type Values = z.input<typeof schema>;

function CreateAdjustmentDialog({ orgId, open, onOpenChange }: { orgId: string; open: boolean; onOpenChange: (open: boolean) => void }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [source, setSource] = useState<LedgerRecord | null>(null);
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { source_entry_id: "", amount: "", reason: "", evidence: "" },
  });
  const amount = useWatch({ control: form.control, name: "amount" });

  const create = useIdempotentMutation<Adjustment, AdjustmentCreate>({
    keyPrefix: "adjustment",
    mutationFn: async (body, idempotencyKey) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/adjustments", {
            params: { path: { organization_id: orgId }, header: { "Idempotency-Key": idempotencyKey } },
            body,
          }),
        )
      ).data,
    onSuccess: () => {
      toast.success(t("pf.adjustments.draftCreated"));
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "adjustments") });
      close();
    },
  });

  function close() {
    onOpenChange(false);
    form.reset();
    setSource(null);
    create.resetOperation();
  }

  const maximum = source ? absMoney(source.signed_amount) : null;
  const errors = form.formState.errors;
  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent className="sm:max-w-[560px]">
        <form
          noValidate
          className="grid gap-4"
          onSubmit={form.handleSubmit((values) => {
            if (!source) return;
            // The server also bounds concurrent adjustments; this catches the obvious case early.
            if (maximum && compareMoney(values.amount.trim(), maximum) > 0) {
              form.setError("amount", { message: "exceedsSource" });
              return;
            }
            create.mutate({
              source_entry_id: source.id,
              amount: values.amount.trim(),
              currency: source.currency,
              reason: values.reason.trim(),
              evidence: values.evidence.trim(),
            });
          })}
        >
          <DialogHeader>
            <DialogTitle>{t("pf.adjustments.createTitle")}</DialogTitle>
            <DialogDescription>{t("pf.adjustments.createDescription")}</DialogDescription>
          </DialogHeader>
          {create.error ? <ErrorAlert error={create.error} /> : null}
          <Field label={t("pf.adjustments.source")} error={errors.source_entry_id?.message} hint={t("pf.adjustments.sourceHint")}>
            {open ? (
              <Controller
                control={form.control}
                name="source_entry_id"
                render={({ field, fieldState }) => (
                  <SourcePicker
                    orgId={orgId}
                    value={field.value}
                    invalid={fieldState.invalid}
                    onChange={(entry) => {
                      setSource(entry);
                      field.onChange(entry.id);
                    }}
                  />
                )}
              />
            ) : null}
          </Field>
          <EffectNote entry={source ?? undefined} />
          <div className="grid grid-cols-[1fr_96px] gap-3">
            <Field
              id="adj-amount"
              label={t("pf.common.amount")}
              error={errors.amount?.message}
              errorVars={{ max: maximum ? formatMoney(maximum) : "—" }}
              hint={maximum ? t("pf.adjustments.amountHint", { max: formatMoney(maximum) }) : t("pf.common.amountHint")}
            >
              <Input
                id="adj-amount"
                inputMode="decimal"
                autoComplete="off"
                className="font-medium tabular-nums"
                {...form.register("amount")}
                aria-invalid={Boolean(errors.amount) || undefined}
              />
            </Field>
            <ReadOnlyValue label={t("pf.common.currency")} mono>
              {source?.currency ?? "—"}
            </ReadOnlyValue>
          </div>
          {maximum && amount.trim() && compareMoneySafe(amount.trim(), maximum) === 0 ? (
            <p className="text-xs text-muted-foreground">{t("pf.adjustments.fullAmount")}</p>
          ) : null}
          <Field id="adj-reason" label={t("pf.common.reason")} error={errors.reason?.message}>
            <Input id="adj-reason" maxLength={2000} {...form.register("reason")} aria-invalid={Boolean(errors.reason) || undefined} />
          </Field>
          <Field id="adj-evidence" label={t("pf.common.evidence")} hint={t("pf.adjustments.evidenceHint")} error={errors.evidence?.message}>
            <Textarea id="adj-evidence" rows={3} maxLength={2000} {...form.register("evidence")} aria-invalid={Boolean(errors.evidence) || undefined} />
          </Field>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={close}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? <Spinner /> : null}
              {t("pf.adjustments.saveDraft")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** compareMoney throws on malformed input; while typing, treat that as "not equal". */
function compareMoneySafe(a: string, b: string) {
  try {
    return compareMoney(a, b);
  } catch {
    return null;
  }
}

function AdjustmentSummary({ orgId, adjustment }: { orgId: string; adjustment: Adjustment }) {
  const { t } = useI18n();
  const source = useLedgerEntry(orgId, adjustment.source_entry_id);
  return (
    <div className="flex flex-col gap-3">
      <DetailList
        className="rounded-md border px-3 py-2.5"
        items={[
          [t("pf.common.amount"), <Money key="a" value={adjustment.amount} currency={adjustment.currency} className="font-semibold" />],
          [
            t("pf.adjustments.source"),
            source.data ? (
              <span key="s" className="flex flex-col">
                <span>
                  <KindLabel kind={source.data.kind} /> #{source.data.sequence} ·{" "}
                  <Money value={source.data.signed_amount} currency={source.data.currency} signed />
                </span>
                <span className="text-xs text-muted-foreground tabular-nums">
                  {formatDateTime(source.data.posted_at)} {t("common.utc")}
                </span>
              </span>
            ) : (
              <code key="s" className="font-mono text-xs" title={adjustment.source_entry_id}>
                {shortId(adjustment.source_entry_id)}
              </code>
            ),
          ],
          [t("pf.common.reason"), <span key="r" className="whitespace-pre-wrap">{adjustment.reason}</span>],
          [t("pf.common.evidence"), <span key="e" className="whitespace-pre-wrap">{adjustment.evidence}</span>],
        ]}
      />
      <EffectNote entry={source.data} />
    </div>
  );
}

function PostAdjustmentDialog({ orgId, adjustmentId, onClose }: { orgId: string; adjustmentId: string; onClose: () => void }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const detail = useQuery({
    queryKey: orgKey(orgId, "adjustments", "detail", adjustmentId),
    queryFn: async () =>
      call(() =>
        nexus.GET("/api/v1/organizations/{organization_id}/adjustments/{adjustment_id}", {
          params: { path: { organization_id: orgId, adjustment_id: adjustmentId } },
        }),
      ),
    staleTime: 0,
  });
  const adjustment = detail.data?.data;
  const ifMatch = detail.data ? (detail.data.etag ?? `"${detail.data.data.version}"`) : null;
  const post = useIdempotentMutation<Adjustment, { ifMatch: string }>({
    keyPrefix: "post-adjustment",
    mutationFn: async ({ ifMatch: version }, idempotencyKey) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/adjustments/{adjustment_id}/post", {
            params: {
              path: { organization_id: orgId, adjustment_id: adjustmentId },
              header: { "If-Match": version, "Idempotency-Key": idempotencyKey },
            },
          }),
        )
      ).data,
    onSuccess: () => {
      toast.success(t("pf.adjustments.postedToast"));
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "adjustments") });
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "account") });
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "ledger") });
      close();
    },
  });
  function close() {
    post.resetOperation();
    onClose();
  }
  return (
    <ConfirmWriteDialog
      open
      onClose={close}
      title={t("pf.adjustments.postTitle")}
      description={t("pf.adjustments.postDescription")}
      submitLabel={t("pf.adjustments.post")}
      loading={detail.isPending}
      loadError={detail.error}
      onRetryLoad={() => void detail.refetch()}
      pending={post.isPending}
      error={post.error}
      onReload={() => {
        post.resetOperation();
        void detail.refetch();
      }}
      disabled={!adjustment || adjustment.status !== "draft" || !ifMatch}
      onSubmit={() => ifMatch && post.mutate({ ifMatch })}
    >
      {adjustment ? <AdjustmentSummary orgId={orgId} adjustment={adjustment} /> : null}
    </ConfirmWriteDialog>
  );
}

function AdjustmentSheet({
  orgId,
  adjustment,
  onClose,
  userLabel,
}: {
  orgId: string;
  adjustment: Adjustment;
  onClose: () => void;
  userLabel: (id: string | null | undefined) => string | undefined;
}) {
  const { t, maybeT } = useI18n();
  return (
    <Sheet open onOpenChange={(next) => !next && onClose()}>
      <SheetContent className="w-full gap-0 overflow-y-auto sm:max-w-[520px]" closeLabel={t("common.close")}>
        <SheetHeader className="border-b">
          <SheetTitle>{t("pf.adjustments.detailTitle")}</SheetTitle>
          <SheetDescription>
            <StatusBadge tone={stateTone[adjustment.status] ?? "neutral"}>
              {maybeT(`pf.common.states.${adjustment.status}`) ?? adjustment.status}
            </StatusBadge>
          </SheetDescription>
        </SheetHeader>
        <div className="flex flex-col gap-4 p-4">
          <AdjustmentSummary orgId={orgId} adjustment={adjustment} />
          <DetailList
            items={[
              [
                t("pf.common.id"),
                <span key="id" className="inline-flex items-center gap-1">
                  <code className="font-mono text-xs break-all">{adjustment.id}</code>
                  <CopyButton value={adjustment.id} />
                </span>,
              ],
              [
                t("pf.adjustments.sourceId"),
                <span key="src" className="inline-flex items-center gap-1">
                  <code className="font-mono text-xs break-all">{adjustment.source_entry_id}</code>
                  <CopyButton value={adjustment.source_entry_id} />
                </span>,
              ],
              [t("pf.common.version"), `v${adjustment.version}`],
              [t("pf.common.createdAt"), `${formatDateTime(adjustment.created_at)} ${t("common.utc")}`],
              [t("pf.common.createdBy"), <Actor key="c" id={adjustment.created_by} label={userLabel} />],
              [t("pf.common.postedAt"), adjustment.posted_at ? `${formatDateTime(adjustment.posted_at)} ${t("common.utc")}` : "—"],
              [t("pf.common.postedBy"), <Actor key="p" id={adjustment.posted_by} label={userLabel} />],
            ]}
          />
        </div>
      </SheetContent>
    </Sheet>
  );
}

function AdjustmentsContent() {
  const { t, maybeT } = useI18n();
  const [orgId, setOrgId] = useOrgParam();
  const [creating, setCreating] = useState(false);
  const [posting, setPosting] = useState<string | null>(null);
  const [viewing, setViewing] = useState<Adjustment | null>(null);
  const userLabel = useUserLabels();

  const adjustments = useCursorList<Adjustment>({
    queryKey: orgKey(orgId ?? "", "adjustments", "list"),
    enabled: Boolean(orgId),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/adjustments", {
            params: { path: { organization_id: orgId! }, query: { limit: 50, cursor } },
          }),
        )
      ).data,
  });

  const columns = useMemo<ColumnDef<Adjustment, unknown>[]>(
    () => [
      {
        header: t("pf.common.createdAt"),
        accessorKey: "created_at",
        cell: ({ row }) => <span className="tabular-nums">{formatDateTime(row.original.created_at, { seconds: false })}</span>,
      },
      {
        header: t("pf.adjustments.source"),
        accessorKey: "source_entry_id",
        cell: ({ row }) => (
          <code className="font-mono text-xs" title={row.original.source_entry_id}>
            {shortId(row.original.source_entry_id)}
          </code>
        ),
      },
      {
        header: t("pf.common.amount"),
        accessorKey: "amount",
        meta: { align: "right" },
        cell: ({ row }) => <Money value={row.original.amount} currency={row.original.currency} className="font-medium" />,
      },
      {
        header: t("pf.common.reason"),
        accessorKey: "reason",
        meta: { className: "max-w-[280px]" },
        cell: ({ row }) => (
          <span className="block truncate" title={row.original.reason}>
            {row.original.reason}
          </span>
        ),
      },
      {
        header: t("pf.common.status"),
        accessorKey: "status",
        cell: ({ row }) => (
          <StatusBadge tone={stateTone[row.original.status] ?? "neutral"}>
            {maybeT(`pf.common.states.${row.original.status}`) ?? row.original.status}
          </StatusBadge>
        ),
      },
      {
        id: "actors",
        header: t("pf.funding.actors"),
        cell: ({ row }) => (
          <span className="flex flex-col gap-0.5 text-xs">
            <span>
              <span className="text-muted-foreground">{t("pf.funding.createdShort")} </span>
              <Actor id={row.original.created_by} label={userLabel} />
            </span>
            {row.original.posted_by ? (
              <span>
                <span className="text-muted-foreground">{t("pf.funding.postedShort")} </span>
                <Actor id={row.original.posted_by} label={userLabel} />
              </span>
            ) : null}
          </span>
        ),
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("pf.common.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <span className="inline-flex items-center gap-1">
            {row.original.status === "draft" ? (
              <Button size="sm" variant="outline" onClick={() => setPosting(row.original.id)}>
                <CheckCheckIcon />
                {t("pf.adjustments.post")}
              </Button>
            ) : null}
            <Button size="icon-sm" variant="ghost" aria-label={t("pf.common.details")} onClick={() => setViewing(row.original)}>
              <FileTextIcon />
            </Button>
          </span>
        ),
      },
    ],
    [t, maybeT, userLabel],
  );

  return (
    <PageContainer>
      <PageHeader
        title={t("pf.adjustments.title")}
        description={t("pf.adjustments.subtitle")}
        actions={
          <>
            <OrgPicker value={orgId} onChange={setOrgId} />
            <Button onClick={() => setCreating(true)} disabled={!orgId}>
              <PlusIcon />
              {t("pf.adjustments.create")}
            </Button>
          </>
        }
      />
      {!orgId ? (
        <PickOrganization />
      ) : (
        <>
          <AccountStrip orgId={orgId} />
          <DataTable
            columns={columns}
            data={adjustments.items}
            loading={adjustments.isPending}
            error={adjustments.error}
            onRetry={() => void adjustments.refetch()}
            getRowId={(row) => row.id}
            minWidth={940}
            empty={
              <EmptyState
                icon={<SlidersHorizontalIcon className="size-5" />}
                title={t("pf.adjustments.empty")}
                description={t("pf.adjustments.emptyBody")}
              />
            }
            footer={
              adjustments.items.length > 0 ? (
                <LoadMoreFooter
                  count={adjustments.items.length}
                  hasNext={Boolean(adjustments.hasNextPage)}
                  loading={adjustments.isFetchingNextPage}
                  onLoadMore={() => void adjustments.fetchNextPage()}
                />
              ) : null
            }
          />
          <CreateAdjustmentDialog orgId={orgId} open={creating} onOpenChange={setCreating} />
          {posting ? <PostAdjustmentDialog orgId={orgId} adjustmentId={posting} onClose={() => setPosting(null)} /> : null}
          {viewing ? (
            <AdjustmentSheet orgId={orgId} adjustment={viewing} onClose={() => setViewing(null)} userLabel={userLabel} />
          ) : null}
        </>
      )}
    </PageContainer>
  );
}

export default function AdjustmentsPage() {
  return (
    <Suspense>
      <AdjustmentsContent />
    </Suspense>
  );
}
