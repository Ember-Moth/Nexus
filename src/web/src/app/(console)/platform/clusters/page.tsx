"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { EllipsisIcon, NetworkIcon, PlusIcon } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { DataTable } from "@/components/data-table";
import { EmptyState } from "@/components/feedback";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useCursorList } from "@/lib/api/hooks";
import type { GatewayCluster } from "@/lib/api/types";

import { AutoPublishBadge, useScopeSummary } from "./auto-publish";
import { ClusterDialog, NodeDialog, ReleaseDialog } from "./cluster-dialogs";
import { ReleaseSheet } from "./release-sheet";

export default function ClustersPage() {
  const t = useT();
  const scopeSummary = useScopeSummary();
  const [viewing, setViewing] = useState<{ cluster: GatewayCluster; revision: string } | null>(null);
  const [editing, setEditing] = useState<GatewayCluster | "new" | null>(null);
  const [registering, setRegistering] = useState<GatewayCluster | null>(null);
  const [releasing, setReleasing] = useState<GatewayCluster | null>(null);
  const clusters = useCursorList<GatewayCluster>({
    queryKey: ["gateway-clusters"],
    fetchPage: async (cursor) =>
      (await call(() => nexus.GET("/api/v1/gateway-clusters", { params: { query: { limit: 50, cursor } } }))).data,
  });

  const columns = useMemo<ColumnDef<GatewayCluster, unknown>[]>(
    () => [
      {
        header: t("clusters.columns.name"),
        accessorKey: "name",
        cell: ({ row }) => (
          <span className="flex flex-col">
            <Link href={`/platform/clusters/${row.original.id}`} className="font-medium underline-offset-3 hover:underline">
              {row.original.name}
            </Link>
            <span className="text-xs text-muted-foreground">{row.original.region}</span>
          </span>
        ),
      },
      {
        header: t("clusters.columns.status"),
        accessorKey: "active",
        cell: ({ row }) => (
          <StatusBadge tone={row.original.active ? "success" : "warning"}>
            {row.original.active ? t("clusters.active") : t("clusters.paused")}
          </StatusBadge>
        ),
      },
      {
        header: t("clusters.columns.desired"),
        accessorKey: "desired_revision",
        cell: ({ row }) =>
          row.original.desired_revision === "0" ? (
            <span className="text-muted-foreground">{t("clusters.noRelease")}</span>
          ) : (
            <span className="font-mono">r{row.original.desired_revision}</span>
          ),
      },
      {
        header: t("clusters.columns.distributed"),
        accessorKey: "distributed_revision",
        cell: ({ row }) => (
          <span className={row.original.distributed_revision === row.original.desired_revision ? "font-mono" : "font-mono text-amber-700"}>
            r{row.original.distributed_revision}
          </span>
        ),
      },
      {
        id: "auto",
        header: t("clusters.columns.auto"),
        cell: ({ row }) => <AutoPublishBadge cluster={row.original} />,
      },
      {
        id: "scope",
        header: t("clusters.columns.scope"),
        cell: ({ row }) => scopeSummary(row.original),
      },
      {
        header: t("clusters.columns.key"),
        accessorKey: "key_version",
        cell: ({ row }) => <span className="tabular-nums">v{row.original.key_version}</span>,
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("clusters.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <span className="flex justify-end gap-1.5">
            <Button
              variant="outline"
              size="sm"
              disabled={row.original.desired_revision === "0"}
              onClick={() => setViewing({ cluster: row.original, revision: row.original.desired_revision })}
            >
              {t("clusters.view")}
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="icon-sm" aria-label={t("clusters.actions", { name: row.original.name })}>
                  <EllipsisIcon />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-44">
                <DropdownMenuItem onSelect={() => setReleasing(row.original)}>{t("clusters.release")}</DropdownMenuItem>
                <DropdownMenuItem onSelect={() => setRegistering(row.original)}>{t("clusters.registerNode")}</DropdownMenuItem>
                <DropdownMenuItem onSelect={() => setEditing(row.original)}>{t("clusters.edit")}</DropdownMenuItem>
                <DropdownMenuItem asChild>
                  <Link href={`/platform/clusters/${row.original.id}`}>{t("clusters.detail")}</Link>
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </span>
        ),
      },
    ],
    [t, scopeSummary],
  );

  return (
    <PageContainer>
      <PageHeader
        title={t("clusters.title")}
        description={t("clusters.subtitle")}
        actions={
          <Button onClick={() => setEditing("new")}>
            <PlusIcon />
            {t("clusters.create")}
          </Button>
        }
      />
      <DataTable
        columns={columns}
        data={clusters.items}
        loading={clusters.isPending}
        error={clusters.error}
        onRetry={() => void clusters.refetch()}
        getRowId={(row) => row.id}
        minWidth={860}
        empty={<EmptyState icon={<NetworkIcon className="size-5" />} title={t("clusters.emptyTitle")} />}
        footer={
          clusters.items.length > 0 ? (
            <LoadMoreFooter
              count={clusters.items.length}
              hasNext={Boolean(clusters.hasNextPage)}
              loading={clusters.isFetchingNextPage}
              onLoadMore={() => void clusters.fetchNextPage()}
            />
          ) : null
        }
      />
      {viewing ? (
        <ReleaseSheet
          key={`${viewing.cluster.id}:${viewing.revision}`}
          cluster={viewing.cluster}
          revision={viewing.revision}
          onClose={() => setViewing(null)}
        />
      ) : null}
      {editing ? <ClusterDialog cluster={editing} onClose={() => setEditing(null)} /> : null}
      {registering ? <NodeDialog clusterId={registering.id} onClose={() => setRegistering(null)} /> : null}
      {releasing ? (
        <ReleaseDialog
          cluster={releasing}
          onClose={() => setReleasing(null)}
          onAccepted={(revision) => {
            setViewing({ cluster: releasing, revision });
            setReleasing(null);
          }}
        />
      ) : null}
    </PageContainer>
  );
}
