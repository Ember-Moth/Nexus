"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { BanIcon, CheckCheckIcon, EllipsisIcon, FileTextIcon, PencilIcon, PlusIcon, ReceiptIcon } from "lucide-react";
import { Suspense, useMemo, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { CopyButton } from "@/components/copy-button";
import { DataTable } from "@/components/data-table";
import { ConflictAlert, EmptyState, ErrorAlert, Spinner } from "@/components/feedback";
import { Money } from "@/components/money";
import { OrgPicker, useOrgParam } from "@/components/org-picker";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { AccountStrip } from "@/components/platform-finance/account-strip";
import { DetailList, Field, ReadOnlyValue } from "@/components/platform-finance/field";
import { Actor, PickOrganization, useUserLabels } from "@/components/platform-finance/scope";
import {
  currencyString,
  isoToUtcInput,
  nonblank,
  pastUtcInput,
  positiveMoney,
  utcInputToIsoStrict,
} from "@/components/platform-finance/validation";
import { ConfirmWriteDialog } from "@/components/platform-finance/write-dialog";
import { StatusBadge, type Tone } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import { useCursorList, useIdempotentMutation } from "@/lib/api/hooks";
import { orgKey } from "@/lib/api/queries";
import type { components } from "@/lib/api/schema";
import { formatDateTime, shortId } from "@/lib/format";

type Funding = components["schemas"]["FundingView"];
type FundingCreate = components["schemas"]["FundingCreate"];
type ReceivingAccount = components["schemas"]["ReceivingView"];

const stateTone: Record<string, Tone> = { draft: "info", posted: "success", cancelled: "neutral" };
const isEditable = (record: Funding) => record.status === "draft" && record.source_type === "manual";

function useReceivingOptions() {
  return useQuery({
    queryKey: ["receiving-accounts", "options"],
    queryFn: async () =>
      (await call(() => nexus.GET("/api/v1/receiving-accounts", { params: { query: { limit: 100 } } }))).data.items,
  });
}

/** Single read with ETag for conditional writes (PATCH, post, cancel). */
function useFundingDetail(orgId: string, id: string | undefined) {
  return useQuery({
    queryKey: orgKey(orgId, "funding-records", "detail", id),
    queryFn: async () =>
      call(() =>
        nexus.GET("/api/v1/organizations/{organization_id}/funding-records/{funding_record_id}", {
          params: { path: { organization_id: orgId, funding_record_id: id! } },
        }),
      ),
    enabled: Boolean(id),
    staleTime: 0,
  });
}

const etagOf = (result: { data: Funding; etag: string | null } | undefined) =>
  result ? (result.etag ?? `"${result.data.version}"`) : null;

const fundingSchema = z.object({
  receiving_account_id: z.string().min(1, "required"),
  external_reference: nonblank(255),
  amount: positiveMoney(),
  currency: currencyString(),
  received_at: pastUtcInput(),
  evidence: nonblank(2000),
});
type FundingValues = z.input<typeof fundingSchema>;

function toBody(values: FundingValues): FundingCreate {
  return {
    receiving_account_id: values.receiving_account_id,
    external_reference: values.external_reference.trim(),
    amount: values.amount.trim(),
    currency: values.currency.trim(),
    received_at: utcInputToIsoStrict(values.received_at) ?? values.received_at,
    evidence: values.evidence.trim(),
  };
}

/** Full editable content of a manual funding draft; shared by create and PATCH. */
function FundingForm({
  defaultValues,
  originalReceivedAt,
  accounts,
  submitLabel,
  pending,
  error,
  conflictReload,
  onSubmit,
  onCancel,
}: {
  defaultValues: FundingValues;
  /** The draft's stored timestamp; kept as is (with sub-second precision) when not edited. */
  originalReceivedAt?: string;
  accounts: ReceivingAccount[];
  submitLabel: string;
  pending: boolean;
  error: unknown;
  conflictReload?: () => void;
  onSubmit: (body: FundingCreate) => void;
  onCancel: () => void;
}) {
  const { t } = useI18n();
  const form = useForm<FundingValues>({ resolver: zodResolver(fundingSchema), defaultValues });
  const errors = form.formState.errors;
  const conflict = isConflict(error);
  // Disabled accounts take no new registrations; keep the draft's current one selectable.
  const selectable = accounts.filter(
    (account) => account.status === "active" || account.id === defaultValues.receiving_account_id,
  );

  return (
    <form
      noValidate
      className="grid gap-4"
      onSubmit={form.handleSubmit((values) => {
        const body = toBody(values);
        if (originalReceivedAt && values.received_at === isoToUtcInput(originalReceivedAt)) body.received_at = originalReceivedAt;
        onSubmit(body);
      })}
    >
      {conflict && conflictReload ? (
        <ConflictAlert onReload={conflictReload} />
      ) : error ? (
        <ErrorAlert error={error} />
      ) : null}
      <Field id="fr-account" label={t("pf.funding.receivingAccount")} error={errors.receiving_account_id?.message}>
        <Controller
          control={form.control}
          name="receiving_account_id"
          render={({ field, fieldState }) => (
            <Select
              value={field.value}
              onValueChange={(value) => {
                field.onChange(value);
                const account = accounts.find((item) => item.id === value);
                if (account) form.setValue("currency", account.currency, { shouldValidate: true });
              }}
            >
              <SelectTrigger id="fr-account" className="w-full" aria-invalid={fieldState.invalid || undefined}>
                <SelectValue placeholder={selectable.length ? t("pf.funding.selectAccount") : t("pf.funding.noAccounts")} />
              </SelectTrigger>
              <SelectContent>
                {selectable.map((account) => (
                  <SelectItem key={account.id} value={account.id}>
                    {account.name}
                    <span className="font-mono text-xs text-muted-foreground">
                      {account.reference} · {account.currency}
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        />
      </Field>
      <Field
        id="fr-ref"
        label={t("pf.funding.externalReference")}
        hint={t("pf.funding.externalReferenceHint")}
        error={errors.external_reference?.message}
      >
        <Input
          id="fr-ref"
          maxLength={255}
          autoComplete="off"
          className="font-mono"
          {...form.register("external_reference")}
          aria-invalid={Boolean(errors.external_reference) || undefined}
        />
      </Field>
      <div className="grid grid-cols-[1fr_96px] gap-3">
        <Field id="fr-amount" label={t("pf.common.amount")} hint={t("pf.common.amountHint")} error={errors.amount?.message}>
          <Input
            id="fr-amount"
            inputMode="decimal"
            autoComplete="off"
            className="font-medium tabular-nums"
            {...form.register("amount")}
            aria-invalid={Boolean(errors.amount) || undefined}
          />
        </Field>
        <ReadOnlyValue label={t("pf.common.currency")} mono>
          {/* Follows the receiving account; the API rejects a different currency. */}
          <Controller control={form.control} name="currency" render={({ field }) => <>{field.value || "—"}</>} />
        </ReadOnlyValue>
      </div>
      <Field id="fr-received" label={t("pf.funding.receivedAt")} hint={t("pf.funding.receivedAtHint")} error={errors.received_at?.message}>
        <div className="flex gap-2">
          <Input
            id="fr-received"
            type="datetime-local"
            step={1}
            className="tabular-nums"
            {...form.register("received_at")}
            aria-invalid={Boolean(errors.received_at) || undefined}
          />
          <Button
            type="button"
            variant="outline"
            onClick={() => form.setValue("received_at", isoToUtcInput(new Date().toISOString()), { shouldValidate: true })}
          >
            {t("pf.funding.now")}
          </Button>
        </div>
      </Field>
      <Field id="fr-evidence" label={t("pf.common.evidence")} hint={t("pf.funding.evidenceHint")} error={errors.evidence?.message}>
        <Textarea
          id="fr-evidence"
          rows={3}
          maxLength={2000}
          {...form.register("evidence")}
          aria-invalid={Boolean(errors.evidence) || undefined}
        />
      </Field>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" disabled={pending || conflict}>
          {pending ? <Spinner /> : null}
          {submitLabel}
        </Button>
      </DialogFooter>
    </form>
  );
}

function CreateFundingDialog({
  orgId,
  open,
  onOpenChange,
  accounts,
}: {
  orgId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  accounts: ReceivingAccount[];
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const create = useIdempotentMutation<Funding, FundingCreate>({
    keyPrefix: "funding-record",
    mutationFn: async (body, idempotencyKey) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/funding-records", {
            params: { path: { organization_id: orgId }, header: { "Idempotency-Key": idempotencyKey } },
            body,
          }),
        )
      ).data,
    onSuccess: () => {
      toast.success(t("pf.funding.draftCreated"));
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "funding-records") });
      close();
    },
  });
  function close() {
    onOpenChange(false);
    create.resetOperation();
  }
  const firstActive = accounts.find((account) => account.status === "active");
  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent className="sm:max-w-[520px]">
        <DialogHeader>
          <DialogTitle>{t("pf.funding.createTitle")}</DialogTitle>
          <DialogDescription>{t("pf.funding.createDescription")}</DialogDescription>
        </DialogHeader>
        {open ? (
          <FundingForm
            defaultValues={{
              receiving_account_id: firstActive?.id ?? "",
              external_reference: "",
              amount: "",
              currency: firstActive?.currency ?? "",
              received_at: "",
              evidence: "",
            }}
            accounts={accounts}
            submitLabel={t("pf.funding.saveDraft")}
            pending={create.isPending}
            error={create.error}
            onSubmit={(body) => create.mutate(body)}
            onCancel={close}
          />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function EditFundingDialog({
  orgId,
  recordId,
  onClose,
  accounts,
}: {
  orgId: string;
  recordId: string;
  onClose: () => void;
  accounts: ReceivingAccount[];
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const detail = useFundingDetail(orgId, recordId);
  const record = detail.data?.data;
  const save = useMutation({
    mutationFn: async ({ body, ifMatch }: { body: FundingCreate; ifMatch: string }) =>
      (
        await call(() =>
          nexus.PATCH("/api/v1/organizations/{organization_id}/funding-records/{funding_record_id}", {
            params: { path: { organization_id: orgId, funding_record_id: recordId }, header: { "If-Match": ifMatch } },
            body,
          }),
        )
      ).data,
    onSuccess: () => {
      toast.success(t("pf.funding.draftSaved"));
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "funding-records") });
      onClose();
    },
  });
  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-[520px]">
        <DialogHeader>
          <DialogTitle>{t("pf.funding.editTitle")}</DialogTitle>
          <DialogDescription>{t("pf.funding.editDescription")}</DialogDescription>
        </DialogHeader>
        {detail.error ? <ErrorAlert error={detail.error} onRetry={() => void detail.refetch()} /> : null}
        {record && !isEditable(record) ? (
          <p className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">{t("pf.funding.notDraft")}</p>
        ) : record ? (
          <FundingForm
            // A reload after 412 re-initializes the form with the latest content.
            key={record.version}
            defaultValues={{
              receiving_account_id: record.receiving_account_id ?? "",
              external_reference: record.external_reference,
              amount: record.amount,
              currency: record.currency,
              received_at: isoToUtcInput(record.received_at),
              evidence: record.evidence,
            }}
            originalReceivedAt={record.received_at}
            accounts={accounts}
            submitLabel={t("common.save")}
            pending={save.isPending}
            error={save.error}
            conflictReload={() => {
              save.reset();
              void detail.refetch();
            }}
            onSubmit={(body) => {
              const ifMatch = etagOf(detail.data);
              if (ifMatch) save.mutate({ body, ifMatch });
            }}
            onCancel={onClose}
          />
        ) : detail.isPending ? (
          <Spinner />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function FundingSummary({ record, accountName }: { record: Funding; accountName: string }) {
  const { t } = useI18n();
  return (
    <DetailList
      className="rounded-md border px-3 py-2.5"
      items={[
        [t("pf.common.amount"), <Money key="a" value={record.amount} currency={record.currency} className="font-semibold" />],
        [t("pf.funding.receivingAccount"), accountName],
        [t("pf.funding.externalReference"), <code key="r" className="font-mono text-xs">{record.external_reference}</code>],
        [t("pf.funding.receivedAt"), `${formatDateTime(record.received_at)} ${t("common.utc")}`],
        [t("pf.common.evidence"), <span key="e" className="whitespace-pre-wrap">{record.evidence}</span>],
      ]}
    />
  );
}

function PostFundingDialog({
  orgId,
  recordId,
  onClose,
  accountName,
}: {
  orgId: string;
  recordId: string;
  onClose: () => void;
  accountName: (id: string | null) => string;
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const detail = useFundingDetail(orgId, recordId);
  const record = detail.data?.data;
  // Posting moves money: If-Match pins the reviewed draft, Idempotency-Key makes retries safe.
  const post = useIdempotentMutation<Funding, { ifMatch: string }>({
    keyPrefix: "post-funding",
    mutationFn: async ({ ifMatch }, idempotencyKey) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/funding-records/{funding_record_id}/post", {
            params: {
              path: { organization_id: orgId, funding_record_id: recordId },
              header: { "If-Match": ifMatch, "Idempotency-Key": idempotencyKey },
            },
          }),
        )
      ).data,
    onSuccess: (posted) => {
      toast.success(t("pf.funding.postedToast"), {
        description: `${posted.currency} ${posted.amount} · ${posted.external_reference}`,
      });
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "funding-records") });
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "account") });
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "ledger") });
      close();
    },
  });
  function close() {
    post.resetOperation();
    onClose();
  }
  const ifMatch = etagOf(detail.data);
  return (
    <ConfirmWriteDialog
      open
      onClose={close}
      title={t("pf.funding.postTitle")}
      description={t("pf.funding.postDescription")}
      submitLabel={t("pf.funding.post")}
      loading={detail.isPending}
      loadError={detail.error}
      onRetryLoad={() => void detail.refetch()}
      pending={post.isPending}
      error={post.error}
      onReload={() => {
        post.resetOperation();
        void detail.refetch();
      }}
      disabled={!record || record.status !== "draft" || !ifMatch}
      onSubmit={() => ifMatch && post.mutate({ ifMatch })}
    >
      {record ? (
        <>
          <FundingSummary record={record} accountName={accountName(record.receiving_account_id)} />
          {record.status !== "draft" ? (
            <p className="text-sm text-muted-foreground">{t("pf.funding.notDraft")}</p>
          ) : null}
        </>
      ) : null}
    </ConfirmWriteDialog>
  );
}

function CancelFundingDialog({
  orgId,
  recordId,
  onClose,
  accountName,
}: {
  orgId: string;
  recordId: string;
  onClose: () => void;
  accountName: (id: string | null) => string;
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const detail = useFundingDetail(orgId, recordId);
  const record = detail.data?.data;
  const cancel = useMutation({
    mutationFn: async ({ ifMatch }: { ifMatch: string }) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/funding-records/{funding_record_id}/cancel", {
            params: { path: { organization_id: orgId, funding_record_id: recordId }, header: { "If-Match": ifMatch } },
          }),
        )
      ).data,
    onSuccess: () => {
      toast.success(t("pf.funding.cancelledToast"));
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "funding-records") });
      onClose();
    },
  });
  const ifMatch = etagOf(detail.data);
  return (
    <ConfirmWriteDialog
      open
      onClose={onClose}
      title={t("pf.funding.cancelTitle")}
      description={t("pf.funding.cancelDescription")}
      submitLabel={t("pf.funding.cancelDraft")}
      destructive
      loading={detail.isPending}
      loadError={detail.error}
      onRetryLoad={() => void detail.refetch()}
      pending={cancel.isPending}
      error={cancel.error}
      onReload={() => {
        cancel.reset();
        void detail.refetch();
      }}
      disabled={!record || record.status !== "draft" || !ifMatch}
      onSubmit={() => ifMatch && cancel.mutate({ ifMatch })}
    >
      {record ? <FundingSummary record={record} accountName={accountName(record.receiving_account_id)} /> : null}
    </ConfirmWriteDialog>
  );
}

function FundingSheet({
  record,
  onClose,
  accountName,
  userLabel,
}: {
  record: Funding;
  onClose: () => void;
  accountName: (id: string | null) => string;
  userLabel: (id: string | null | undefined) => string | undefined;
}) {
  const { t, maybeT } = useI18n();
  const idCell = (value: string | null) =>
    value ? (
      <span className="inline-flex items-center gap-1">
        <code className="font-mono text-xs break-all">{value}</code>
        <CopyButton value={value} />
      </span>
    ) : (
      "—"
    );
  return (
    <Sheet open onOpenChange={(next) => !next && onClose()}>
      <SheetContent className="w-full gap-0 overflow-y-auto sm:max-w-[520px]" closeLabel={t("common.close")}>
        <SheetHeader className="border-b">
          <SheetTitle>{t("pf.funding.detailTitle")}</SheetTitle>
          <SheetDescription className="flex flex-wrap items-center gap-2">
            <StatusBadge tone={stateTone[record.status] ?? "neutral"}>
              {maybeT(`pf.common.states.${record.status}`) ?? record.status}
            </StatusBadge>
            <SourceBadge source={record.source_type} />
          </SheetDescription>
        </SheetHeader>
        <div className="flex flex-col gap-4 p-4">
          <FundingSummary record={record} accountName={accountName(record.receiving_account_id)} />
          <DetailList
            items={[
              [t("pf.common.id"), idCell(record.id)],
              [t("pf.common.version"), `v${record.version}`],
              [t("pf.common.createdAt"), `${formatDateTime(record.created_at)} ${t("common.utc")}`],
              [t("pf.common.createdBy"), <Actor key="c" id={record.created_by} systemActor={record.system_actor} label={userLabel} />],
              [t("pf.common.postedAt"), record.posted_at ? `${formatDateTime(record.posted_at)} ${t("common.utc")}` : "—"],
              [
                t("pf.common.postedBy"),
                record.status === "posted" ? (
                  <Actor key="p" id={record.posted_by} systemActor={record.system_actor} label={userLabel} />
                ) : (
                  "—"
                ),
              ],
              [t("pf.funding.paymentOrder"), idCell(record.payment_order_id)],
              [t("pf.funding.paymentReceipt"), idCell(record.payment_receipt_id)],
            ]}
          />
          {record.source_type === "gateway" ? (
            <p className="rounded-md border bg-muted/40 px-3 py-2 text-xs leading-relaxed text-muted-foreground">
              {t("pf.funding.gatewayNote")}
            </p>
          ) : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function SourceBadge({ source }: { source: string }) {
  const { maybeT } = useI18n();
  return (
    <span className="inline-flex h-[22px] items-center rounded-md border px-2 text-xs whitespace-nowrap text-muted-foreground">
      {maybeT(`pf.funding.sources.${source}`) ?? source}
    </span>
  );
}

type Dialogs =
  | { kind: "create" }
  | { kind: "edit" | "post" | "cancel"; id: string }
  | { kind: "detail"; record: Funding }
  | null;

function FundingContent() {
  const { t, maybeT } = useI18n();
  const [orgId, setOrgId] = useOrgParam();
  const [dialog, setDialog] = useState<Dialogs>(null);
  const receiving = useReceivingOptions();
  const userLabel = useUserLabels();
  const accounts = useMemo(() => receiving.data ?? [], [receiving.data]);

  const accountName = useMemo(() => {
    const names = new Map(accounts.map((account) => [account.id, account.name] as const));
    return (id: string | null) => (id ? (names.get(id) ?? shortId(id)) : "—");
  }, [accounts]);

  const records = useCursorList<Funding>({
    queryKey: orgKey(orgId ?? "", "funding-records", "list"),
    enabled: Boolean(orgId),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/funding-records", {
            params: { path: { organization_id: orgId! }, query: { limit: 50, cursor } },
          }),
        )
      ).data,
  });

  const columns = useMemo<ColumnDef<Funding, unknown>[]>(
    () => [
      {
        header: t("pf.funding.receivedAt"),
        accessorKey: "received_at",
        cell: ({ row }) => <span className="tabular-nums">{formatDateTime(row.original.received_at, { seconds: false })}</span>,
      },
      {
        header: t("pf.funding.externalReference"),
        accessorKey: "external_reference",
        cell: ({ row }) => (
          <span className="flex min-w-0 flex-col">
            <code className="truncate font-mono text-[12.5px]" title={row.original.external_reference}>
              {row.original.external_reference}
            </code>
            <span className="text-xs text-muted-foreground">
              {row.original.source_type === "gateway"
                ? t("pf.funding.sources.gateway")
                : accountName(row.original.receiving_account_id)}
            </span>
          </span>
        ),
        meta: { className: "max-w-[260px]" },
      },
      {
        header: t("pf.common.amount"),
        accessorKey: "amount",
        meta: { align: "right" },
        cell: ({ row }) => <Money value={row.original.amount} currency={row.original.currency} className="font-medium" />,
      },
      {
        header: t("pf.funding.source"),
        accessorKey: "source_type",
        cell: ({ row }) => <SourceBadge source={row.original.source_type} />,
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
              <Actor id={row.original.created_by} systemActor={row.original.system_actor} label={userLabel} />
            </span>
            {row.original.status === "posted" ? (
              <span>
                <span className="text-muted-foreground">{t("pf.funding.postedShort")} </span>
                <Actor id={row.original.posted_by} systemActor={row.original.system_actor} label={userLabel} />
              </span>
            ) : null}
          </span>
        ),
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("pf.common.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => {
          const record = row.original;
          const editable = isEditable(record);
          return (
            <span className="inline-flex items-center gap-1">
              {editable ? (
                <Button size="sm" variant="outline" onClick={() => setDialog({ kind: "post", id: record.id })}>
                  <CheckCheckIcon />
                  {t("pf.funding.post")}
                </Button>
              ) : null}
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="ghost" size="icon-sm" aria-label={t("common.more")}>
                    <EllipsisIcon />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuItem onSelect={() => setDialog({ kind: "detail", record })}>
                    <FileTextIcon />
                    {t("pf.common.details")}
                  </DropdownMenuItem>
                  {editable ? (
                    <>
                      <DropdownMenuItem onSelect={() => setDialog({ kind: "edit", id: record.id })}>
                        <PencilIcon />
                        {t("pf.funding.editDraft")}
                      </DropdownMenuItem>
                      <DropdownMenuSeparator />
                      <DropdownMenuItem variant="destructive" onSelect={() => setDialog({ kind: "cancel", id: record.id })}>
                        <BanIcon />
                        {t("pf.funding.cancelDraft")}
                      </DropdownMenuItem>
                    </>
                  ) : null}
                </DropdownMenuContent>
              </DropdownMenu>
            </span>
          );
        },
      },
    ],
    [t, maybeT, accountName, userLabel],
  );

  const close = () => setDialog(null);
  return (
    <PageContainer>
      <PageHeader
        title={t("pf.funding.title")}
        description={t("pf.funding.subtitle")}
        actions={
          <>
            <OrgPicker value={orgId} onChange={setOrgId} />
            <Button onClick={() => setDialog({ kind: "create" })} disabled={!orgId}>
              <PlusIcon />
              {t("pf.funding.create")}
            </Button>
          </>
        }
      />
      {receiving.error ? <ErrorAlert error={receiving.error} onRetry={() => void receiving.refetch()} /> : null}
      {!orgId ? (
        <PickOrganization />
      ) : (
        <>
          <AccountStrip orgId={orgId} />
          <DataTable
            columns={columns}
            data={records.items}
            loading={records.isPending}
            error={records.error}
            onRetry={() => void records.refetch()}
            getRowId={(row) => row.id}
            minWidth={980}
            rowClassName={(row) => (row.status === "cancelled" ? "text-muted-foreground" : undefined)}
            empty={
              <EmptyState
                icon={<ReceiptIcon className="size-5" />}
                title={t("pf.funding.empty")}
                description={t("pf.funding.emptyBody")}
              />
            }
            footer={
              records.items.length > 0 ? (
                <LoadMoreFooter
                  count={records.items.length}
                  hasNext={Boolean(records.hasNextPage)}
                  loading={records.isFetchingNextPage}
                  onLoadMore={() => void records.fetchNextPage()}
                />
              ) : null
            }
          />
          <CreateFundingDialog
            orgId={orgId}
            open={dialog?.kind === "create"}
            onOpenChange={(open) => !open && close()}
            accounts={accounts}
          />
          {dialog?.kind === "edit" ? (
            <EditFundingDialog orgId={orgId} recordId={dialog.id} onClose={close} accounts={accounts} />
          ) : null}
          {dialog?.kind === "post" ? (
            <PostFundingDialog orgId={orgId} recordId={dialog.id} onClose={close} accountName={accountName} />
          ) : null}
          {dialog?.kind === "cancel" ? (
            <CancelFundingDialog orgId={orgId} recordId={dialog.id} onClose={close} accountName={accountName} />
          ) : null}
          {dialog?.kind === "detail" ? (
            <FundingSheet record={dialog.record} onClose={close} accountName={accountName} userLabel={userLabel} />
          ) : null}
        </>
      )}
    </PageContainer>
  );
}

export default function FundingPage() {
  return (
    <Suspense>
      <FundingContent />
    </Suspense>
  );
}
