"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { useMemo, useState } from "react";

import { CopyButton } from "@/components/copy-button";
import { DataTable } from "@/components/data-table";
import { EmptyState } from "@/components/feedback";
import { Money } from "@/components/money";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { defaultReportFilters, reportQuery, ReportFilters, type ReportFilterState } from "@/components/report-filters";
import { useOrgWorkspace } from "@/components/shell/workspace";
import { StatusBadge, toneFor } from "@/components/status-badge";
import { useI18n } from "@/i18n/provider";
import { callAs, nexus } from "@/lib/api/client";
import { useCursorList } from "@/lib/api/hooks";
import { orgKey, useProjectOptions } from "@/lib/api/queries";
import type { ReportPage, RequestRow } from "@/lib/api/types";
import { formatDateTime, shortId } from "@/lib/format";

export default function RequestsPage() {
  const { t, maybeT } = useI18n();
  const { orgId } = useOrgWorkspace();
  const [filters, setFilters] = useState<ReportFilterState>(defaultReportFilters);
  const query = reportQuery(filters);
  const projects = useProjectOptions(orgId);

  const requests = useCursorList<RequestRow>({
    queryKey: orgKey(orgId, "requests", query),
    fetchPage: async (cursor) =>
      (
        await callAs<ReportPage<RequestRow>>(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/requests", {
            params: { path: { organization_id: orgId }, query: { ...query, limit: 50, cursor } },
          }),
        )
      ).data,
  });

  const columns = useMemo<ColumnDef<RequestRow, unknown>[]>(
    () => [
      {
        header: t("requests.columns.time"),
        accessorKey: "occurred_at",
        cell: ({ row }) => <span className="tabular-nums">{formatDateTime(row.original.occurred_at)}</span>,
      },
      {
        header: t("requests.columns.id"),
        accessorKey: "id",
        cell: ({ row }) => (
          <span className="inline-flex items-center gap-1">
            <code className="font-mono text-[12.5px]" title={row.original.id}>
              {shortId(row.original.id)}
            </code>
            <CopyButton value={row.original.id} />
          </span>
        ),
      },
      {
        header: t("requests.columns.key"),
        accessorKey: "api_key_id",
        cell: ({ row }) => <code className="font-mono text-xs" title={row.original.api_key_id}>{shortId(row.original.api_key_id)}</code>,
      },
      {
        header: t("requests.columns.model"),
        accessorKey: "model_id",
        cell: ({ row }) => <code className="font-mono text-xs" title={row.original.model_id}>{shortId(row.original.model_id)}</code>,
      },
      {
        header: t("requests.columns.state"),
        accessorKey: "state",
        cell: ({ row }) => (
          <StatusBadge tone={toneFor(row.original.state)}>
            {maybeT(`status.${row.original.state}`) ?? row.original.state}
            {row.original.reservation_state ? ` · ${row.original.reservation_state}` : ""}
          </StatusBadge>
        ),
      },
      {
        header: t("requests.columns.reserved"),
        accessorKey: "reserved_amount",
        meta: { align: "right" },
        cell: ({ row }) => <Money value={row.original.reserved_amount} className="text-muted-foreground" />,
      },
      {
        header: t("requests.columns.charged"),
        accessorKey: "charged_amount",
        meta: { align: "right" },
        cell: ({ row }) => <Money value={row.original.charged_amount} className="font-medium" />,
      },
    ],
    [t, maybeT],
  );

  return (
    <PageContainer>
      <PageHeader title={t("requests.title")} description={t("requests.subtitle")} />
      <ReportFilters
        value={filters}
        onChange={setFilters}
        projects={projects.data ?? []}
        allProjectsLabel={t("keys.allProjects")}
      />
      <DataTable
        columns={columns}
        data={requests.items}
        loading={requests.isPending}
        error={requests.error}
        onRetry={() => void requests.refetch()}
        getRowId={(row) => row.id}
        minWidth={940}
        empty={<EmptyState title={t("requests.emptyTitle")} />}
        footer={
          requests.items.length > 0 ? (
            <LoadMoreFooter
              count={requests.items.length}
              hasNext={Boolean(requests.hasNextPage)}
              loading={requests.isFetchingNextPage}
              onLoadMore={() => void requests.fetchNextPage()}
            />
          ) : null
        }
      />
    </PageContainer>
  );
}
