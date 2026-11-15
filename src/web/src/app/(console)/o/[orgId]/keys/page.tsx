"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { BanIcon, CloudIcon, EllipsisIcon, KeyRoundIcon, PlusIcon, RefreshCwIcon } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { DataTable } from "@/components/data-table";
import { EmptyState } from "@/components/feedback";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { useOrgWorkspace } from "@/components/shell/workspace";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useCursorList } from "@/lib/api/hooks";
import { orgKey, useProjectOptions, useServiceAccountOptions } from "@/lib/api/queries";
import type { ApiKey } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";

import { IssueKeyDialog } from "./issue-key-dialog";
import { RevokeKeyDialog, RotateKeyDialog } from "./key-actions";
import { canRevoke, canRotate, isUsable, keyStatus } from "./key-status";

const ALL = "__all";

export default function KeysPage() {
  const t = useT();
  const { orgId } = useOrgWorkspace();
  const [projectId, setProjectId] = useState<string>(ALL);
  const [serviceAccountId, setServiceAccountId] = useState<string>(ALL);
  const [hideInactive, setHideInactive] = useState(false);
  const [issueOpen, setIssueOpen] = useState(false);
  const [rotating, setRotating] = useState<ApiKey | null>(null);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);

  const projects = useProjectOptions(orgId);
  const accounts = useServiceAccountOptions(orgId);
  const projectFilter = projectId === ALL ? undefined : projectId;
  const accountFilter = serviceAccountId === ALL ? undefined : serviceAccountId;

  const keys = useCursorList<ApiKey>({
    queryKey: orgKey(orgId, "api-keys", "list", projectFilter ?? null, accountFilter ?? null),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/api-keys", {
            params: {
              path: { organization_id: orgId },
              query: { project_id: projectFilter, service_account_id: accountFilter, limit: 50, cursor },
            },
          }),
        )
      ).data,
  });

  const projectName = useMemo(() => new Map((projects.data ?? []).map((p) => [p.id, p.name])), [projects.data]);
  const accountName = useMemo(() => new Map((accounts.data ?? []).map((a) => [a.id, a.name])), [accounts.data]);
  const visibleAccounts = (accounts.data ?? []).filter((a) => !projectFilter || a.project_id === projectFilter);
  // The API cannot filter by status; hiding inactive keys is a client-side view option.
  const rows = hideInactive ? keys.items.filter((key) => isUsable(keyStatus(key))) : keys.items;

  const columns = useMemo<ColumnDef<ApiKey, unknown>[]>(
    () => [
      {
        header: t("keys.columns.name"),
        accessorKey: "name",
        cell: ({ row }) => <span className="font-medium">{row.original.name}</span>,
      },
      {
        header: t("keys.columns.prefix"),
        accessorKey: "prefix",
        cell: ({ row }) => <code className="font-mono text-[12.5px]">{row.original.prefix}…</code>,
      },
      {
        id: "owner",
        header: t("keys.columns.owner"),
        cell: ({ row }) => (
          <span className="flex flex-col">
            <span>{projectName.get(row.original.project_id) ?? "—"}</span>
            <span className="text-xs text-muted-foreground">{accountName.get(row.original.service_account_id) ?? "—"}</span>
          </span>
        ),
      },
      {
        id: "status",
        header: t("keys.columns.status"),
        cell: ({ row }) => {
          const status = keyStatus(row.original);
          const label =
            status.state === "expiring"
              ? t("keys.statusExpiring", { days: status.days ?? 0 })
              : status.state === "grace"
                ? t("keys.statusGrace", { time: `${formatDateTime(status.until, { seconds: false })} UTC` })
                : t(`keys.status${status.state[0].toUpperCase()}${status.state.slice(1)}` as "keys.statusActive");
          return <StatusBadge tone={status.tone}>{label}</StatusBadge>;
        },
      },
      {
        id: "expires",
        header: t("keys.columns.expires"),
        cell: ({ row }) => (
          <span className="tabular-nums">
            {row.original.expires_at ? formatDateTime(row.original.expires_at, { seconds: false }) : t("common.neverExpires")}
          </span>
        ),
      },
      {
        id: "created",
        header: t("keys.columns.created"),
        cell: ({ row }) => (
          <span className="text-muted-foreground tabular-nums">{formatDateTime(row.original.created_at, { seconds: false })}</span>
        ),
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("keys.columns.actions")}</span>,
        meta: { className: "w-12" },
        cell: ({ row }) => {
          const status = keyStatus(row.original);
          return (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="icon-sm" aria-label={t("keys.menuLabel", { name: row.original.name })}>
                  <EllipsisIcon />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-44">
                {/* Revoked keys keep their upstream copies until the purge, so the link stays. */}
                <DropdownMenuItem asChild>
                  <Link href={`/o/${orgId}/upstream-resources?api_key_id=${row.original.id}`}>
                    <CloudIcon />
                    {t("keys.upstreamResources")}
                  </Link>
                </DropdownMenuItem>
                {canRotate(status) ? (
                  <DropdownMenuItem onSelect={() => setRotating(row.original)}>
                    <RefreshCwIcon />
                    {t("keys.rotate")}
                  </DropdownMenuItem>
                ) : null}
                {canRevoke(status) ? (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem variant="destructive" onSelect={() => setRevoking(row.original)}>
                      <BanIcon />
                      {t("keys.revoke")}
                    </DropdownMenuItem>
                  </>
                ) : null}
              </DropdownMenuContent>
            </DropdownMenu>
          );
        },
      },
    ],
    [t, orgId, projectName, accountName],
  );

  const noAccounts = accounts.data !== undefined && accounts.data.length === 0;

  return (
    <PageContainer>
      <PageHeader
        title={t("keys.title")}
        description={t("keys.subtitle")}
        actions={
          <Button onClick={() => setIssueOpen(true)} disabled={noAccounts}>
            <PlusIcon />
            {t("keys.issue")}
          </Button>
        }
      />
      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={projectId}
          onValueChange={(value) => {
            setProjectId(value);
            setServiceAccountId(ALL);
          }}
        >
          <SelectTrigger aria-label={t("keys.filterProject")} className="min-w-44">
            <span className="text-muted-foreground">{t("keys.filterProject")}</span>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>{t("keys.allProjects")}</SelectItem>
            {(projects.data ?? []).map((project) => (
              <SelectItem key={project.id} value={project.id}>
                {project.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={serviceAccountId} onValueChange={setServiceAccountId}>
          <SelectTrigger aria-label={t("keys.filterServiceAccount")} className="min-w-44">
            <span className="text-muted-foreground">{t("keys.filterServiceAccount")}</span>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>{t("keys.allServiceAccounts")}</SelectItem>
            {visibleAccounts.map((account) => (
              <SelectItem key={account.id} value={account.id}>
                {account.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <div className="ml-1 flex items-center gap-2">
          <Checkbox id="hide-inactive" checked={hideInactive} onCheckedChange={(value) => setHideInactive(value === true)} />
          <Label htmlFor="hide-inactive" className="font-normal">
            {t("keys.hideInactive")}
          </Label>
        </div>
      </div>
      <DataTable
        columns={columns}
        data={rows}
        loading={keys.isPending}
        error={keys.error}
        onRetry={() => void keys.refetch()}
        getRowId={(key) => key.id}
        minWidth={920}
        rowClassName={(key) => (isUsable(keyStatus(key)) ? undefined : "text-muted-foreground")}
        empty={
          <EmptyState
            icon={<KeyRoundIcon className="size-5" />}
            title={t("keys.emptyTitle")}
            description={t("keys.emptyBody")}
            action={
              noAccounts ? (
                <Button variant="outline" asChild>
                  <Link href={`/o/${orgId}/projects`}>{t("keys.emptyNoAccounts")}</Link>
                </Button>
              ) : (
                <Button onClick={() => setIssueOpen(true)}>{t("keys.issue")}</Button>
              )
            }
          />
        }
        footer={
          keys.items.length > 0 ? (
            <LoadMoreFooter
              count={keys.items.length}
              hasNext={Boolean(keys.hasNextPage)}
              loading={keys.isFetchingNextPage}
              onLoadMore={() => void keys.fetchNextPage()}
            />
          ) : null
        }
      />
      <IssueKeyDialog
        key={accountFilter ?? "all"}
        orgId={orgId}
        open={issueOpen}
        onOpenChange={setIssueOpen}
        defaultServiceAccountId={accountFilter}
        onRotate={setRotating}
      />
      <RotateKeyDialog orgId={orgId} apiKey={rotating} onOpenChange={(open) => !open && setRotating(null)} />
      <RevokeKeyDialog orgId={orgId} apiKey={revoking} onOpenChange={(open) => !open && setRevoking(null)} />
    </PageContainer>
  );
}
