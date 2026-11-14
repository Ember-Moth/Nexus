"use client";

import type { ColumnDef } from "@tanstack/react-table";
import Link from "next/link";
import { useMemo, useState } from "react";

import { DataTable } from "@/components/data-table";
import { EmptyState } from "@/components/feedback";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { ALL_PROJECTS } from "@/components/report-filters";
import { useOrgWorkspace } from "@/components/shell/workspace";
import { StatusBadge, toneFor } from "@/components/status-badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useCursorList } from "@/lib/api/hooks";
import { orgKey, useProjectOptions } from "@/lib/api/queries";
import type { ServiceAccount } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";

export default function ServiceAccountsPage() {
  const { t, maybeT } = useI18n();
  const { orgId } = useOrgWorkspace();
  const [projectId, setProjectId] = useState(ALL_PROJECTS);
  const projectFilter = projectId === ALL_PROJECTS ? undefined : projectId;
  const projects = useProjectOptions(orgId);
  const projectName = useMemo(() => new Map((projects.data ?? []).map((p) => [p.id, p.name])), [projects.data]);

  const accounts = useCursorList<ServiceAccount>({
    queryKey: orgKey(orgId, "service-accounts", "list", projectFilter ?? null),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/service-accounts", {
            params: { path: { organization_id: orgId }, query: { project_id: projectFilter, limit: 50, cursor } },
          }),
        )
      ).data,
  });

  const columns = useMemo<ColumnDef<ServiceAccount, unknown>[]>(
    () => [
      {
        header: t("projects.columns.name"),
        accessorKey: "name",
        cell: ({ row }) => <span className="font-mono text-[12.5px] font-medium">{row.original.name}</span>,
      },
      {
        header: t("serviceAccounts.project"),
        accessorKey: "project_id",
        cell: ({ row }) => (
          <Link className="underline-offset-3 hover:underline" href={`/o/${orgId}/projects?project=${row.original.project_id}`}>
            {projectName.get(row.original.project_id) ?? "—"}
          </Link>
        ),
      },
      {
        header: t("projects.columns.status"),
        accessorKey: "status",
        cell: ({ row }) => (
          <StatusBadge tone={toneFor(row.original.status)}>{maybeT(`status.${row.original.status}`) ?? row.original.status}</StatusBadge>
        ),
      },
      {
        header: t("projects.columns.created"),
        accessorKey: "created_at",
        cell: ({ row }) => <span className="text-muted-foreground tabular-nums">{formatDateTime(row.original.created_at, { seconds: false })}</span>,
      },
    ],
    [t, maybeT, orgId, projectName],
  );

  return (
    <PageContainer>
      <PageHeader title={t("serviceAccounts.title")} description={t("serviceAccounts.subtitle")} />
      <Select value={projectId} onValueChange={setProjectId}>
        <SelectTrigger aria-label={t("serviceAccounts.project")} className="w-fit min-w-48">
          <span className="text-muted-foreground">{t("serviceAccounts.project")}</span>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL_PROJECTS}>{t("keys.allProjects")}</SelectItem>
          {(projects.data ?? []).map((project) => (
            <SelectItem key={project.id} value={project.id}>
              {project.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <DataTable
        columns={columns}
        data={accounts.items}
        loading={accounts.isPending}
        error={accounts.error}
        onRetry={() => void accounts.refetch()}
        getRowId={(row) => row.id}
        empty={<EmptyState title={t("projects.saEmpty")} />}
        footer={
          accounts.items.length > 0 ? (
            <LoadMoreFooter
              count={accounts.items.length}
              hasNext={Boolean(accounts.hasNextPage)}
              loading={accounts.isFetchingNextPage}
              onLoadMore={() => void accounts.fetchNextPage()}
            />
          ) : null
        }
      />
    </PageContainer>
  );
}
