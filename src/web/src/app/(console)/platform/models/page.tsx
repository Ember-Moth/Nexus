"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { BoxIcon, PencilIcon, PlusIcon } from "lucide-react";
import { useMemo, useState } from "react";

import { DataTable } from "@/components/data-table";
import { EmptyState } from "@/components/feedback";
import { useOrganizationOptions } from "@/components/org-picker";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { CatalogStatusBadge, ConfiguredBadge, ProtocolBadge } from "@/components/platform-catalog/badges";
import { inEnumOrder, PROTOCOLS, TOKEN_DIMENSIONS, TOOL_DIMENSIONS } from "@/components/platform-catalog/labels";
import { catalogKey } from "@/components/platform-catalog/queries";
import type { CatalogModel } from "@/components/platform-catalog/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useCursorList } from "@/lib/api/hooks";

import { ModelDialog } from "./model-dialog";

const number = (value: number) => value.toLocaleString("en-US");
const NONE = "__none";

export default function ModelsPage() {
  const t = useT();
  const [editing, setEditing] = useState<CatalogModel | "new" | null>(null);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [previewOrg, setPreviewOrg] = useState<string>(NONE);
  const organizations = useOrganizationOptions();
  const organizationId = previewOrg === NONE ? undefined : previewOrg;

  // Without organization_id an admin reads the full catalog; with it, the API applies that
  // organization's access policies and shows only active, configured models (customer view).
  const models = useCursorList<CatalogModel>({
    queryKey: catalogKey("models", "page", organizationId ?? null),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/models", { params: { query: { limit: 50, cursor, organization_id: organizationId } } }),
        )
      ).data,
  });
  const rows = useMemo(
    () => (includeArchived ? models.items : models.items.filter((m) => m.status !== "archived")),
    [models.items, includeArchived],
  );

  const columns = useMemo<ColumnDef<CatalogModel, unknown>[]>(
    () => [
      {
        id: "model",
        header: t("pc.models.columns.model"),
        cell: ({ row }) => {
          const tokens = inEnumOrder(row.original.token_dimensions ?? [], TOKEN_DIMENSIONS);
          const tools = inEnumOrder(row.original.tool_dimensions ?? [], TOOL_DIMENSIONS);
          return (
            <span className="flex flex-col gap-0.5">
              <span className="font-mono text-[12.5px] font-medium">{row.original.public_name}</span>
              <span
                className="text-xs text-muted-foreground"
                title={[
                  ...tokens.map((d) => `${t(`pc.enums.tokenDimension.${d}`)} · ${d}`),
                  ...tools.map((d) => `${t(`pc.enums.toolDimension.${d}`)} · ${d}`),
                ].join("\n")}
              >
                {t("pc.models.nameLine", { name: row.original.display_name, count: tokens.length + tools.length })}
              </span>
            </span>
          );
        },
      },
      {
        id: "protocols",
        header: t("pc.models.columns.protocols"),
        cell: ({ row }) => (
          <span className="flex flex-wrap gap-1">
            {inEnumOrder(row.original.protocols, PROTOCOLS).map((p) => (
              <ProtocolBadge key={p} protocol={p} />
            ))}
          </span>
        ),
      },
      {
        id: "limits",
        header: t("pc.models.columns.limits"),
        meta: { align: "right" },
        cell: ({ row }) => (
          <span className="tabular-nums whitespace-nowrap">
            {number(row.original.max_input_tokens)}
            <span className="text-muted-foreground"> / </span>
            {number(row.original.max_output_tokens)}
          </span>
        ),
      },
      {
        id: "capabilities",
        header: t("pc.models.columns.capabilities"),
        cell: ({ row }) => {
          const caps = row.original.capabilities;
          if (!caps?.streaming && !caps?.tools) return <span className="text-muted-foreground">{t("pc.models.capabilities.none")}</span>;
          return (
            <span className="flex flex-wrap gap-1">
              {caps?.streaming ? <Badge variant="secondary">{t("pc.models.capabilities.streaming")}</Badge> : null}
              {caps?.tools ? <Badge variant="secondary">{t("pc.models.capabilities.tools")}</Badge> : null}
            </span>
          );
        },
      },
      {
        header: t("pc.models.columns.status"),
        accessorKey: "status",
        cell: ({ row }) => <CatalogStatusBadge status={row.original.status} />,
      },
      {
        id: "configured",
        header: t("pc.models.columns.configured"),
        cell: ({ row }) => <ConfiguredBadge configured={row.original.configured} />,
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("pc.models.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <Button variant="outline" size="sm" onClick={() => setEditing(row.original)}>
            <PencilIcon />
            {t("pc.common.edit")}
          </Button>
        ),
      },
    ],
    [t],
  );

  const previewName = organizations.data?.find((o) => o.id === organizationId)?.name;

  return (
    <PageContainer>
      <PageHeader
        title={t("pc.models.title")}
        description={t("pc.models.subtitle")}
        actions={
          <Button onClick={() => setEditing("new")}>
            <PlusIcon />
            {t("pc.models.create")}
          </Button>
        }
      />
      <div className="flex flex-wrap gap-2">
        <Select value={includeArchived ? "all" : "current"} onValueChange={(value) => setIncludeArchived(value === "all")}>
          <SelectTrigger aria-label={t("pc.models.filters.status")} className="min-w-48">
            <span className="text-muted-foreground">{t("pc.models.filters.status")}</span>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="current">{t("pc.models.filters.current")}</SelectItem>
            <SelectItem value="all">{t("pc.models.filters.withArchived")}</SelectItem>
          </SelectContent>
        </Select>
        <Select value={previewOrg} onValueChange={setPreviewOrg}>
          <SelectTrigger aria-label={t("pc.models.filters.preview")} className="min-w-56">
            <span className="text-muted-foreground">{t("pc.models.filters.preview")}</span>
            <SelectValue>{previewName ?? t("pc.models.filters.noPreview")}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={NONE}>{t("pc.models.filters.noPreview")}</SelectItem>
            {(organizations.data ?? []).map((org) => (
              <SelectItem key={org.id} value={org.id}>
                {org.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {organizationId ? (
        <p className="text-sm text-muted-foreground">{t("pc.models.previewNote", { name: previewName ?? "" })}</p>
      ) : null}
      <DataTable
        columns={columns}
        data={rows}
        loading={models.isPending}
        error={models.error}
        onRetry={() => void models.refetch()}
        getRowId={(row) => row.id}
        minWidth={980}
        rowClassName={(row) => (row.status === "archived" ? "text-muted-foreground" : undefined)}
        empty={
          <EmptyState
            icon={<BoxIcon className="size-5" />}
            title={organizationId ? t("pc.models.previewEmpty") : t("pc.models.emptyTitle")}
            description={organizationId ? undefined : t("pc.models.emptyBody")}
          />
        }
        footer={
          models.items.length > 0 ? (
            <LoadMoreFooter
              count={models.items.length}
              hasNext={Boolean(models.hasNextPage)}
              loading={models.isFetchingNextPage}
              onLoadMore={() => void models.fetchNextPage()}
            />
          ) : null
        }
      />
      <p className="text-xs text-muted-foreground">{t("pc.models.footnote")}</p>
      {editing !== null ? (
        <ModelDialog
          key={editing === "new" ? "new" : editing.id}
          model={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
        />
      ) : null}
    </PageContainer>
  );
}
