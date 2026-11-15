"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { GitBranchIcon, PencilIcon, PlusIcon } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useMemo, useState } from "react";

import { DataTable } from "@/components/data-table";
import { EmptyState } from "@/components/feedback";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { CatalogStatusBadge, ConfiguredBadge } from "@/components/platform-catalog/badges";
import { PcErrorAlert } from "@/components/platform-catalog/errors";
import { useDeployments, useModels, useUpstreams } from "@/components/platform-catalog/queries";
import type { CatalogDeployment } from "@/components/platform-catalog/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useT } from "@/i18n/provider";
import { shortId } from "@/lib/format";

import { DeploymentDialog } from "./deployment-dialog";

const ALL = "__all";

/** Filters live in ?model=&upstream= so upstream detail pages can link here. */
function useFilters() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const modelId = params.get("model") ?? undefined;
  const upstreamId = params.get("upstream") ?? undefined;
  const set = useCallback(
    (key: "model" | "upstream", value: string) => {
      const search = new URLSearchParams(params.toString());
      if (value === ALL) search.delete(key);
      else search.set(key, value);
      const query = search.toString();
      router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
    },
    [params, pathname, router],
  );
  const clear = useCallback(() => router.replace(pathname, { scroll: false }), [pathname, router]);
  return { modelId, upstreamId, set, clear };
}

function DeploymentsView() {
  const t = useT();
  const { modelId, upstreamId, set, clear } = useFilters();
  const [editing, setEditing] = useState<CatalogDeployment | "new" | null>(null);
  const models = useModels();
  const upstreams = useUpstreams();
  const deployments = useDeployments({ modelId, upstreamId }, { limit: 50, scope: "page" });

  const names = useMemo(
    () => ({
      model: new Map(models.items.map((m) => [m.id, m.public_name])),
      upstream: new Map(upstreams.items.map((u) => [u.id, u.name])),
    }),
    [models.items, upstreams.items],
  );

  const columns = useMemo<ColumnDef<CatalogDeployment, unknown>[]>(
    () => [
      {
        header: t("pc.deployments.columns.name"),
        accessorKey: "name",
        cell: ({ row }) => <span className="font-medium">{row.original.name}</span>,
      },
      {
        id: "model",
        header: t("pc.deployments.columns.model"),
        cell: ({ row }) => (
          <span className="font-mono text-[12.5px]">{names.model.get(row.original.model_id) ?? shortId(row.original.model_id)}</span>
        ),
      },
      {
        id: "upstream",
        header: t("pc.deployments.columns.upstream"),
        cell: ({ row }) => (
          <Link href={`/platform/upstreams/${row.original.upstream_id}`} className="hover:underline">
            {names.upstream.get(row.original.upstream_id) ?? shortId(row.original.upstream_id)}
          </Link>
        ),
      },
      {
        id: "upstreamModel",
        header: t("pc.deployments.columns.upstreamModel"),
        cell: ({ row }) => (
          <span className="flex flex-col">
            <code className="max-w-[240px] truncate font-mono text-[12.5px]" title={row.original.upstream_model}>
              {row.original.upstream_model}
            </code>
            <span className="text-xs text-muted-foreground">{row.original.region}</span>
          </span>
        ),
      },
      {
        id: "relay",
        header: t("pc.deployments.columns.relay"),
        cell: ({ row }) => {
          const profile = row.original.relay_profile;
          if (!profile) return <span className="text-muted-foreground">{t("pc.deployments.noRelay")}</span>;
          return (
            <span className="flex flex-wrap items-center gap-1">
              <Badge variant="outline" className="font-normal">
                {t(`pc.relay.options.${profile.endpoint}`)}
              </Badge>
              {profile.transports.map((transport) => (
                <Badge key={transport} variant="secondary" className="font-normal">
                  {t(`pc.relay.options.${transport}`)}
                </Badge>
              ))}
              {profile.convert_from ? (
                <span className="text-xs text-muted-foreground">← {t(`pc.relay.options.${profile.convert_from}`)}</span>
              ) : null}
            </span>
          );
        },
      },
      {
        header: t("pc.deployments.columns.status"),
        accessorKey: "status",
        cell: ({ row }) => <CatalogStatusBadge status={row.original.status} />,
      },
      {
        id: "configured",
        header: t("pc.deployments.columns.configured"),
        cell: ({ row }) => <ConfiguredBadge configured={row.original.configured} />,
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("pc.deployments.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <Button variant="outline" size="sm" onClick={() => setEditing(row.original)}>
            <PencilIcon />
            {t("pc.common.edit")}
          </Button>
        ),
      },
    ],
    [t, names],
  );

  const filtered = Boolean(modelId || upstreamId);

  return (
    <PageContainer>
      <PageHeader
        title={t("pc.deployments.title")}
        description={t("pc.deployments.subtitle")}
        actions={
          <Button onClick={() => setEditing("new")}>
            <PlusIcon />
            {t("pc.deployments.create")}
          </Button>
        }
      />
      <div className="flex flex-wrap gap-2">
        <Select value={modelId ?? ALL} onValueChange={(value) => set("model", value)}>
          <SelectTrigger aria-label={t("pc.deployments.filters.model")} className="min-w-56">
            <span className="text-muted-foreground">{t("pc.deployments.filters.model")}</span>
            <SelectValue>
              {modelId ? (names.model.get(modelId) ?? shortId(modelId)) : t("pc.deployments.filters.all")}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>{t("pc.deployments.filters.all")}</SelectItem>
            {models.items.map((m) => (
              <SelectItem key={m.id} value={m.id}>
                <span className="font-mono">{m.public_name}</span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={upstreamId ?? ALL} onValueChange={(value) => set("upstream", value)}>
          <SelectTrigger aria-label={t("pc.deployments.filters.upstream")} className="min-w-56">
            <span className="text-muted-foreground">{t("pc.deployments.filters.upstream")}</span>
            <SelectValue>
              {upstreamId ? (names.upstream.get(upstreamId) ?? shortId(upstreamId)) : t("pc.deployments.filters.all")}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>{t("pc.deployments.filters.all")}</SelectItem>
            {upstreams.items.map((u) => (
              <SelectItem key={u.id} value={u.id}>
                {u.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {filtered ? (
          <Button variant="ghost" onClick={clear}>
            {t("common.clearFilters")}
          </Button>
        ) : null}
      </div>
      {models.error || upstreams.error ? <PcErrorAlert error={models.error ?? upstreams.error} /> : null}
      <DataTable
        columns={columns}
        data={deployments.items}
        loading={deployments.isPending}
        error={deployments.error}
        onRetry={() => void deployments.refetch()}
        getRowId={(row) => row.id}
        minWidth={1100}
        rowClassName={(row) => (row.status === "archived" ? "text-muted-foreground" : undefined)}
        empty={
          <EmptyState
            icon={<GitBranchIcon className="size-5" />}
            title={filtered ? t("pc.deployments.filteredEmpty") : t("pc.deployments.emptyTitle")}
          />
        }
        footer={
          deployments.items.length > 0 ? (
            <LoadMoreFooter
              count={deployments.items.length}
              hasNext={Boolean(deployments.hasNextPage)}
              loading={deployments.isFetchingNextPage}
              onLoadMore={() => void deployments.fetchNextPage()}
            />
          ) : null
        }
      />
      {editing !== null ? (
        <DeploymentDialog
          key={editing === "new" ? "new" : editing.id}
          deployment={editing === "new" ? null : editing}
          filters={{ modelId, upstreamId }}
          onClose={() => setEditing(null)}
        />
      ) : null}
    </PageContainer>
  );
}

export default function DeploymentsPage() {
  return (
    <Suspense>
      <DeploymentsView />
    </Suspense>
  );
}
