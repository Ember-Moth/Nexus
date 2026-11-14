"use client";

import { useMutation } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { DownloadIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { EmptyState, ErrorAlert, useErrorToast } from "@/components/feedback";
import { Money } from "@/components/money";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { defaultReportFilters, reportQuery, ReportFilters, type ReportFilterState } from "@/components/report-filters";
import { useOrgWorkspace } from "@/components/shell/workspace";
import { StatusBadge, toneFor } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useCursorList } from "@/lib/api/hooks";
import { orgKey, useAccount, useProjectOptions } from "@/lib/api/queries";
import type { LedgerRecord } from "@/lib/api/types";
import { formatDateTime, shortId } from "@/lib/format";
import { rememberExport } from "../audit/exports";

const KIND_DOTS: Record<string, string> = {
  funding_credit: "bg-emerald-600",
  usage_debit: "bg-zinc-500",
  usage_refund: "bg-blue-600",
  funding_reversal: "bg-red-600",
};

export default function WalletPage() {
  const { t, maybeT } = useI18n();
  const workspace = useOrgWorkspace();
  const { orgId } = workspace;
  const showError = useErrorToast();
  const router = useRouter();
  const [filters, setFilters] = useState<ReportFilterState>(defaultReportFilters);
  const query = reportQuery(filters);
  const account = useAccount(orgId);
  const projects = useProjectOptions(orgId);

  const ledger = useCursorList<LedgerRecord>({
    queryKey: orgKey(orgId, "ledger", query),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/ledger", {
            params: { path: { organization_id: orgId }, query: { ...query, limit: 50, cursor } },
          }),
        )
      ).data,
  });

  const exportLedger = useMutation({
    mutationFn: async () =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/exports", {
            params: { path: { organization_id: orgId } },
            body: { kind: "ledger", filters: query },
          }),
        )
      ).data,
    onSuccess: (job) => {
      rememberExport(orgId, job);
      toast.success(t("wallet.exportQueued"), {
        action: { label: t("nav.items.audit"), onClick: () => router.push(`/o/${orgId}/audit`) },
      });
    },
    onError: (error) => showError(error),
  });

  const actorLabel = (entry: LedgerRecord) =>
    entry.actor_id === workspace.me.user.id ? workspace.me.user.login : entry.actor_id ? shortId(entry.actor_id) : t("wallet.system");

  const columns = useMemo<ColumnDef<LedgerRecord, unknown>[]>(
    () => [
      {
        header: t("wallet.columns.sequence"),
        accessorKey: "sequence",
        cell: ({ row }) => <span className="text-muted-foreground tabular-nums">#{row.original.sequence}</span>,
      },
      {
        id: "time",
        header: t("wallet.columns.time"),
        cell: ({ row }) => (
          <span className="tabular-nums">{formatDateTime(filters.basis === "posted_at" ? row.original.posted_at : row.original.occurred_at)}</span>
        ),
      },
      {
        header: t("wallet.columns.kind"),
        accessorKey: "kind",
        cell: ({ row }) => (
          <span className="inline-flex h-[22px] items-center gap-1.5 rounded-md border px-2 text-xs font-medium">
            <span aria-hidden className={`size-1.5 rounded-full ${KIND_DOTS[row.original.kind] ?? "bg-zinc-400"}`} />
            {maybeT(`wallet.kinds.${row.original.kind}`) ?? row.original.kind}
          </span>
        ),
      },
      {
        id: "source",
        header: t("wallet.columns.source"),
        cell: ({ row }) => (
          <span className="flex flex-col">
            <span>{maybeT(`wallet.sources.${row.original.source_type}`) ?? row.original.source_type}</span>
            <code className="font-mono text-[11.5px] text-muted-foreground" title={row.original.source_id}>
              {shortId(row.original.source_id)}
            </code>
          </span>
        ),
      },
      {
        id: "amount",
        header: t("wallet.columns.amount"),
        meta: { align: "right" },
        cell: ({ row }) => <Money value={row.original.signed_amount} signed className="font-medium" />,
      },
      {
        id: "balance",
        header: t("wallet.columns.balance"),
        meta: { align: "right" },
        cell: ({ row }) => <Money value={row.original.balance_after} />,
      },
      {
        id: "actor",
        header: t("wallet.columns.actor"),
        cell: ({ row }) => <span className="text-foreground/80">{actorLabel(row.original)}</span>,
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [t, maybeT, filters.basis, workspace.me.user.id],
  );

  const currency = account.data?.currency ?? "USD";
  const summary = [
    { label: t("wallet.balance", { currency }), value: account.data?.balance },
    { label: t("wallet.held"), value: account.data?.held_amount },
    { label: t("wallet.available"), value: account.data?.available_balance },
  ];

  return (
    <PageContainer>
      <PageHeader
        title={t("wallet.title")}
        description={t("wallet.subtitle")}
        actions={
          <Button variant="outline" onClick={() => exportLedger.mutate()} disabled={exportLedger.isPending}>
            <DownloadIcon />
            {t("wallet.export")}
          </Button>
        }
      />
      {account.error ? (
        <ErrorAlert error={account.error} onRetry={() => void account.refetch()} />
      ) : (
        <div className="flex flex-wrap overflow-hidden rounded-xl border">
          {summary.map((item) => (
            <div key={item.label} className="flex flex-[1_1_180px] flex-col gap-1 border-r px-5 py-3.5 last:border-r-0">
              <span className="text-xs text-muted-foreground">{item.label}</span>
              {account.isPending ? (
                <Skeleton className="h-6 w-28" />
              ) : (
                <Money value={item.value} className="text-lg font-semibold" extraClassName="text-sm" />
              )}
            </div>
          ))}
          <div className="flex flex-[1_1_180px] flex-col gap-1 px-5 py-3.5">
            <span className="text-xs text-muted-foreground">{t("wallet.state")}</span>
            {account.data ? (
              <span className="flex items-center gap-2">
                <StatusBadge tone={toneFor(account.data.status)}>
                  {maybeT(`status.${account.data.status}`) ?? account.data.status}
                </StatusBadge>
                <span className="text-xs text-muted-foreground tabular-nums">{formatDateTime(account.data.as_of)}</span>
              </span>
            ) : (
              <Skeleton className="h-6 w-28" />
            )}
          </div>
        </div>
      )}
      <ReportFilters
        value={filters}
        onChange={setFilters}
        projects={projects.data ?? []}
        allProjectsLabel={t("wallet.allProjects")}
      />
      <DataTable
        columns={columns}
        data={ledger.items}
        loading={ledger.isPending}
        error={ledger.error}
        onRetry={() => void ledger.refetch()}
        getRowId={(entry) => entry.id}
        minWidth={940}
        empty={<EmptyState title={t("wallet.emptyTitle")} />}
        footer={
          ledger.items.length > 0 ? (
            <LoadMoreFooter
              count={ledger.items.length}
              hasNext={Boolean(ledger.hasNextPage)}
              loading={ledger.isFetchingNextPage}
              onLoadMore={() => void ledger.fetchNextPage()}
              note={t("wallet.precisionHint")}
            />
          ) : null
        }
      />
    </PageContainer>
  );
}
