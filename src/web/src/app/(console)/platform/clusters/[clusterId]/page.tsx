"use client";

import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ArrowLeftIcon, CheckIcon, CircleXIcon, NetworkIcon } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo, useState } from "react";

import { DataTable } from "@/components/data-table";
import { EmptyState, ErrorAlert, LoadingRows } from "@/components/feedback";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { useUserLabels } from "@/components/platform-finance/scope";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useCursorList } from "@/lib/api/hooks";
import type { components } from "@/lib/api/schema";
import { formatDateTime, shortId } from "@/lib/format";

import { AutoPublishCard, useScopeSummary } from "../auto-publish";
import { ClusterDialog, NodeDialog, ReleaseDialog } from "../cluster-dialogs";
import { ReleaseSheet } from "../release-sheet";

type PrincipalView = components["schemas"]["PrincipalView"];
type ReleaseRecord = components["schemas"]["ReleaseRecord"];

export default function ClusterDetailPage() {
  const t = useT();
  const userLabel = useUserLabels();
  const scopeSummary = useScopeSummary();
  const { clusterId } = useParams<{ clusterId: string }>();
  const [dialog, setDialog] = useState<"edit" | "node" | "release" | null>(null);
  const [editingNode, setEditingNode] = useState<PrincipalView | null>(null);
  const [viewing, setViewing] = useState<string | null>(null);

  // GET /gateway-clusters/{cluster_id} carries the ETag used as If-Match by the edit dialog.
  const cluster = useQuery({
    queryKey: ["gateway-clusters", clusterId],
    queryFn: async () =>
      call(() => nexus.GET("/api/v1/gateway-clusters/{cluster_id}", { params: { path: { cluster_id: clusterId } } })),
  });
  const nodes = useCursorList<PrincipalView>({
    queryKey: ["gateway-clusters", clusterId, "principals"],
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/gateway-clusters/{cluster_id}/principals", {
            params: { path: { cluster_id: clusterId }, query: { limit: 50, cursor } },
          }),
        )
      ).data,
  });
  const releases = useCursorList<ReleaseRecord>({
    queryKey: ["gateway-clusters", clusterId, "releases"],
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/gateway-clusters/{cluster_id}/releases", {
            params: { path: { cluster_id: clusterId }, query: { limit: 20, cursor } },
          }),
        )
      ).data,
  });

  const nodeColumns = useMemo<ColumnDef<PrincipalView, unknown>[]>(
    () => [
      {
        header: t("clusters.nodeColumns.node"),
        accessorKey: "node_id",
        cell: ({ row }) => (
          <code className="font-mono text-xs" title={row.original.node_id}>
            {shortId(row.original.node_id)}
          </code>
        ),
      },
      {
        header: t("clusters.nodeColumns2.role"),
        accessorKey: "database_role",
        cell: ({ row }) => <code className="font-mono text-xs">{row.original.database_role}</code>,
      },
      {
        header: t("clusters.nodeColumns2.capabilities"),
        accessorKey: "capabilities",
        cell: ({ row }) => (
          <span className="flex max-w-[360px] flex-wrap gap-1">
            {row.original.capabilities.map((capability) => (
              <span key={capability} className="rounded border px-1.5 font-mono text-[11px]">
                {capability}
              </span>
            ))}
          </span>
        ),
      },
      {
        header: t("clusters.nodeColumns.active"),
        accessorKey: "active",
        cell: ({ row }) => (
          <StatusBadge tone={row.original.active ? "success" : "neutral"}>
            {row.original.active ? t("clusters.active") : t("status.disabled")}
          </StatusBadge>
        ),
      },
      {
        header: t("clusters.nodeColumns.applied"),
        accessorKey: "applied_revision",
        cell: ({ row }) => <span className="font-mono">r{row.original.applied_revision}</span>,
      },
      {
        id: "ack",
        header: t("clusters.nodeColumns.ack"),
        cell: ({ row }) => {
          const ack = row.original.last_ack;
          if (!ack) return <span className="text-muted-foreground">{t("clusters.noAck")}</span>;
          return (
            <span className={ack.applied ? "flex items-center gap-1 text-emerald-700" : "flex items-center gap-1 text-red-700"}>
              {ack.applied ? <CheckIcon className="size-3.5" /> : <CircleXIcon className="size-3.5" />}
              {ack.applied
                ? t("clusters.ackApplied", { revision: ack.revision })
                : t("clusters.ackFailed", { revision: ack.revision, code: ack.error_code ?? "—" })}
            </span>
          );
        },
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("clusters.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <Button variant="outline" size="sm" onClick={() => setEditingNode(row.original)}>
            {t("clusters.edit")}
          </Button>
        ),
      },
    ],
    [t],
  );

  const releaseColumns = useMemo<ColumnDef<ReleaseRecord, unknown>[]>(
    () => [
      {
        header: t("clusters.historyColumns.revision"),
        accessorKey: "revision",
        cell: ({ row }) => <span className="font-mono font-medium">r{row.original.revision}</span>,
      },
      {
        header: t("clusters.historyColumns.created"),
        accessorKey: "created_at",
        cell: ({ row }) => <span className="tabular-nums">{formatDateTime(row.original.created_at)}</span>,
      },
      {
        header: t("clusters.historyColumns.actor"),
        accessorKey: "initiator",
        // Releases by the auto-publisher have no person behind them.
        cell: ({ row }) =>
          row.original.initiator === "auto_publisher" || !row.original.actor_id ? (
            <StatusBadge tone="info">{t("clusters.systemActor")}</StatusBadge>
          ) : (
            (userLabel(row.original.actor_id) ?? (
              <code className="font-mono text-xs" title={row.original.actor_id}>
                {shortId(row.original.actor_id)}
              </code>
            ))
          ),
      },
      {
        header: t("clusters.historyColumns.rollback"),
        accessorKey: "rollback_revision",
        cell: ({ row }) => (row.original.rollback_revision ? <span className="font-mono">r{row.original.rollback_revision}</span> : "—"),
      },
      {
        id: "delivered",
        header: t("clusters.historyColumns.delivered"),
        cell: ({ row }) =>
          row.original.distribution.delivered_at ? (
            <span className="tabular-nums">{formatDateTime(row.original.distribution.delivered_at)}</span>
          ) : (
            <span className="text-amber-700">{t("clusters.notDelivered")}</span>
          ),
      },
      {
        header: t("clusters.historyColumns.sha"),
        accessorKey: "sha256",
        cell: ({ row }) => (
          <code className="font-mono text-xs" title={row.original.sha256}>
            {row.original.sha256.slice(0, 12)}…
          </code>
        ),
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("clusters.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <Button variant="outline" size="sm" onClick={() => setViewing(row.original.revision)}>
            {t("clusters.view")}
          </Button>
        ),
      },
    ],
    [t, userLabel],
  );

  if (cluster.error) {
    return (
      <PageContainer>
        <ErrorAlert error={cluster.error} onRetry={() => void cluster.refetch()} />
      </PageContainer>
    );
  }
  const data = cluster.data?.data;

  return (
    <PageContainer>
      <Link href="/platform/clusters" className="flex w-fit items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeftIcon className="size-4" aria-hidden />
        {t("clusters.back")}
      </Link>
      {!data ? (
        <LoadingRows rows={3} />
      ) : (
        <>
          <PageHeader
            title={data.name}
            badge={
              <StatusBadge tone={data.active ? "success" : "warning"}>
                {data.active ? t("clusters.active") : t("clusters.paused")}
              </StatusBadge>
            }
            description={`${data.region} · ${scopeSummary(data)} · ${t("clusters.columns.key")} v${data.key_version}`}
            actions={
              <>
                <Button variant="outline" onClick={() => setDialog("edit")}>
                  {t("clusters.edit")}
                </Button>
                <Button variant="outline" onClick={() => setDialog("node")}>
                  {t("clusters.registerNode")}
                </Button>
                <Button onClick={() => setDialog("release")}>{t("clusters.release")}</Button>
              </>
            }
          />
          <section aria-label={t("clusters.title")} className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3">
            <div className="flex flex-col gap-1.5 rounded-xl border p-4">
              <span className="text-sm text-muted-foreground">{t("clusters.columns.desired")}</span>
              <span className="font-mono text-2xl font-semibold">{data.desired_revision === "0" ? "—" : `r${data.desired_revision}`}</span>
            </div>
            <div className="flex flex-col gap-1.5 rounded-xl border p-4">
              <span className="text-sm text-muted-foreground">{t("clusters.columns.distributed")}</span>
              <span className="font-mono text-2xl font-semibold">{data.distributed_revision === "0" ? "—" : `r${data.distributed_revision}`}</span>
            </div>
            <div className="flex flex-col gap-1.5 rounded-xl border p-4">
              <span className="text-sm text-muted-foreground">{t("clusters.nodes")}</span>
              <span className="text-2xl font-semibold tabular-nums">
                {nodes.items.filter((n) => n.active && n.applied_revision === data.desired_revision).length} /{" "}
                {nodes.items.filter((n) => n.active).length}
              </span>
            </div>
          </section>
          <AutoPublishCard cluster={data} etag={cluster.data?.etag} />
        </>
      )}
      <Tabs defaultValue="nodes">
        <TabsList variant="line">
          <TabsTrigger value="nodes">{t("clusters.nodesTab")}</TabsTrigger>
          <TabsTrigger value="history">{t("clusters.historyTab")}</TabsTrigger>
        </TabsList>
        <TabsContent value="nodes" className="pt-4">
          <DataTable
            columns={nodeColumns}
            data={nodes.items}
            loading={nodes.isPending}
            error={nodes.error}
            onRetry={() => void nodes.refetch()}
            getRowId={(row) => row.node_id}
            minWidth={900}
            empty={<EmptyState icon={<NetworkIcon className="size-5" />} title={t("clusters.nodesEmpty")} />}
            footer={
              nodes.hasNextPage ? (
                <LoadMoreFooter count={nodes.items.length} hasNext loading={nodes.isFetchingNextPage} onLoadMore={() => void nodes.fetchNextPage()} />
              ) : null
            }
          />
        </TabsContent>
        <TabsContent value="history" className="pt-4">
          <DataTable
            columns={releaseColumns}
            data={releases.items}
            loading={releases.isPending}
            error={releases.error}
            onRetry={() => void releases.refetch()}
            getRowId={(row) => row.revision}
            minWidth={900}
            empty={<EmptyState title={t("clusters.historyEmpty")} />}
            footer={
              releases.items.length > 0 ? (
                <LoadMoreFooter
                  count={releases.items.length}
                  hasNext={Boolean(releases.hasNextPage)}
                  loading={releases.isFetchingNextPage}
                  onLoadMore={() => void releases.fetchNextPage()}
                />
              ) : null
            }
          />
        </TabsContent>
      </Tabs>
      {data && dialog === "edit" ? <ClusterDialog cluster={data} etag={cluster.data?.etag} onClose={() => setDialog(null)} /> : null}
      {dialog === "node" ? <NodeDialog clusterId={clusterId} onClose={() => setDialog(null)} /> : null}
      {editingNode ? <NodeDialog clusterId={clusterId} node={editingNode} onClose={() => setEditingNode(null)} /> : null}
      {data && dialog === "release" ? (
        <ReleaseDialog
          cluster={data}
          onClose={() => setDialog(null)}
          onAccepted={(revision) => {
            setDialog(null);
            setViewing(revision);
          }}
        />
      ) : null}
      {data && viewing ? <ReleaseSheet key={viewing} cluster={data} revision={viewing} onClose={() => setViewing(null)} /> : null}
    </PageContainer>
  );
}
