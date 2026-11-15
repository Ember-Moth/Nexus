"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { KeyRoundIcon, PencilIcon, PlusIcon, ServerIcon } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { DataTable } from "@/components/data-table";
import { EmptyState } from "@/components/feedback";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { CatalogStatusBadge, ProtocolBadge } from "@/components/platform-catalog/badges";
import { useUpstreams } from "@/components/platform-catalog/queries";
import type { Upstream } from "@/components/platform-catalog/types";
import { Button } from "@/components/ui/button";
import { useT } from "@/i18n/provider";
import { formatDateTime } from "@/lib/format";

import { UpstreamDialog } from "./upstream-dialog";

export default function UpstreamsPage() {
  const t = useT();
  const router = useRouter();
  const [editing, setEditing] = useState<Upstream | "new" | null>(null);
  const upstreams = useUpstreams(50, "page");

  const columns = useMemo<ColumnDef<Upstream, unknown>[]>(
    () => [
      {
        header: t("pc.upstreams.columns.name"),
        accessorKey: "name",
        cell: ({ row }) => (
          <Link href={`/platform/upstreams/${row.original.id}`} className="font-medium hover:underline">
            {row.original.name}
          </Link>
        ),
      },
      {
        header: t("pc.upstreams.columns.protocol"),
        accessorKey: "protocol_family",
        cell: ({ row }) => <ProtocolBadge protocol={row.original.protocol_family} />,
      },
      {
        header: t("pc.upstreams.columns.baseUrl"),
        accessorKey: "base_url",
        cell: ({ row }) => (
          <code className="block max-w-[320px] truncate font-mono text-[12.5px]" title={row.original.base_url}>
            {row.original.base_url}
          </code>
        ),
      },
      {
        header: t("pc.upstreams.columns.status"),
        accessorKey: "status",
        cell: ({ row }) => <CatalogStatusBadge status={row.original.status} />,
      },
      {
        header: t("pc.upstreams.columns.created"),
        accessorKey: "created_at",
        cell: ({ row }) => (
          <span className="text-muted-foreground tabular-nums">{formatDateTime(row.original.created_at, { seconds: false })}</span>
        ),
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("pc.upstreams.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <span className="inline-flex gap-2">
            <Button variant="outline" size="sm" asChild>
              <Link href={`/platform/upstreams/${row.original.id}`}>
                <KeyRoundIcon />
                {t("pc.upstreams.manageCredentials")}
              </Link>
            </Button>
            <Button variant="outline" size="sm" onClick={() => setEditing(row.original)}>
              <PencilIcon />
              {t("pc.common.edit")}
            </Button>
          </span>
        ),
      },
    ],
    [t],
  );

  return (
    <PageContainer>
      <PageHeader
        title={t("pc.upstreams.title")}
        description={t("pc.upstreams.subtitle")}
        actions={
          <Button onClick={() => setEditing("new")}>
            <PlusIcon />
            {t("pc.upstreams.create")}
          </Button>
        }
      />
      <DataTable
        columns={columns}
        data={upstreams.items}
        loading={upstreams.isPending}
        error={upstreams.error}
        onRetry={() => void upstreams.refetch()}
        getRowId={(row) => row.id}
        minWidth={900}
        rowClassName={(row) => (row.status === "archived" ? "text-muted-foreground" : undefined)}
        empty={<EmptyState icon={<ServerIcon className="size-5" />} title={t("pc.upstreams.emptyTitle")} />}
        footer={
          upstreams.items.length > 0 ? (
            <LoadMoreFooter
              count={upstreams.items.length}
              hasNext={Boolean(upstreams.hasNextPage)}
              loading={upstreams.isFetchingNextPage}
              onLoadMore={() => void upstreams.fetchNextPage()}
            />
          ) : null
        }
      />
      {editing !== null ? (
        <UpstreamDialog
          key={editing === "new" ? "new" : editing.id}
          upstream={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          // A new upstream needs a credential before deployments can use it.
          onCreated={(created) => router.push(`/platform/upstreams/${created.id}`)}
        />
      ) : null}
    </PageContainer>
  );
}
