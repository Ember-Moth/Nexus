"use client";

import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { BoxIcon, CircleAlertIcon, TagIcon } from "lucide-react";
import { useMemo, useState } from "react";

import { CopyButton } from "@/components/copy-button";
import { DataTable } from "@/components/data-table";
import { EmptyState, ErrorAlert, LoadingRows } from "@/components/feedback";
import { Money } from "@/components/money";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { hasAnyRole, useOrgWorkspace } from "@/components/shell/workspace";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatCount, trimDecimal, useKeyOptions } from "@/components/workspace/lookups";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";
import { useCursorList } from "@/lib/api/hooks";
import { orgKey, useProjectOptions } from "@/lib/api/queries";
import type { Model } from "@/lib/api/types";
import { formatDateTime, shortId } from "@/lib/format";

const NONE = "__none";

interface Context {
  projectId?: string;
  keyId?: string;
}

function PricingSheet({
  orgId,
  model,
  context,
  contextLabel,
  onClose,
}: {
  orgId: string;
  model: Model | null;
  context: Context;
  contextLabel: string;
  onClose: () => void;
}) {
  const { t, maybeT } = useI18n();
  const pricing = useQuery({
    queryKey: orgKey(orgId, "pricing", model?.id, context.projectId ?? null, context.keyId ?? null),
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/pricing", {
            params: {
              path: { organization_id: orgId },
              query: { model_id: model!.id, project_id: context.projectId, key_id: context.keyId },
            },
          }),
        )
      ).data,
    enabled: model !== null,
    retry: (count, error) => count < 2 && !(isApiError(error) && error.status < 500),
  });
  const data = pricing.data;
  const factor = data ? trimDecimal(data.discount_factor) : "";
  const noDiscount = factor === "1";
  const unavailable = isApiError(pricing.error) && (pricing.error.status === 409 || pricing.error.status === 404);
  // Rates follow the model's own dimension order: token dimensions, then tool dimensions.
  const order: string[] = [...(model?.token_dimensions ?? []), ...(model?.tool_dimensions ?? [])];
  const rank = (dimension: string) => (order.includes(dimension) ? order.indexOf(dimension) : order.length);
  const rates = data ? Object.entries(data.rates).sort(([a], [b]) => rank(a) - rank(b)) : [];

  return (
    <Sheet open={model !== null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full gap-0 sm:max-w-[620px]">
        {model ? (
          <>
            <SheetHeader className="border-b">
              <SheetTitle className="font-mono">{model.public_name}</SheetTitle>
              <SheetDescription>
                {model.display_name} · {contextLabel}
              </SheetDescription>
            </SheetHeader>
            <div className="flex flex-col gap-4 overflow-y-auto p-4">
              {pricing.isPending ? (
                <LoadingRows rows={4} columns={3} />
              ) : unavailable ? (
                <div className="flex gap-2.5 rounded-md border border-amber-200 bg-amber-50 px-3 py-2.5 text-sm text-amber-900">
                  <CircleAlertIcon className="mt-0.5 size-4 shrink-0 text-amber-700" aria-hidden />
                  <span>
                    {isApiError(pricing.error) && pricing.error.status === 409
                      ? t("ws.models.priceUnavailable")
                      : t("ws.models.priceNotVisible")}
                  </span>
                </div>
              ) : pricing.error ? (
                <ErrorAlert error={pricing.error} onRetry={() => void pricing.refetch()} />
              ) : data ? (
                <>
                  <dl className="grid grid-cols-[120px_1fr] gap-x-3 gap-y-2 rounded-md border px-3 py-2.5 text-[13px]">
                    <dt className="text-muted-foreground">{t("ws.models.currency")}</dt>
                    <dd>{data.currency}</dd>
                    <dt className="text-muted-foreground">{t("ws.models.saleVersion")}</dt>
                    <dd className="flex flex-col">
                      <span className="inline-flex items-center gap-1">
                        <code className="font-mono text-xs" title={data.sale_price_version_id}>
                          {shortId(data.sale_price_version_id)}
                        </code>
                        <CopyButton value={data.sale_price_version_id} />
                      </span>
                      <span className="text-xs text-muted-foreground tabular-nums">
                        {t("ws.models.effectiveSince", { time: formatDateTime(data.sale_effective_at) })}
                      </span>
                    </dd>
                    <dt className="text-muted-foreground">{t("ws.models.discountVersion")}</dt>
                    <dd className="flex flex-col">
                      <span className="inline-flex items-center gap-1">
                        <code className="font-mono text-xs" title={data.discount_version_id}>
                          {shortId(data.discount_version_id)}
                        </code>
                        <CopyButton value={data.discount_version_id} />
                      </span>
                      <span className="text-xs text-muted-foreground tabular-nums">
                        {t("ws.models.effectiveSince", { time: formatDateTime(data.discount_effective_at) })}
                      </span>
                    </dd>
                    <dt className="text-muted-foreground">{t("ws.models.discountFactor")}</dt>
                    <dd className="flex items-center gap-2">
                      <span className="font-medium tabular-nums">× {factor}</span>
                      {noDiscount ? <Badge variant="outline">{t("ws.models.noDiscount")}</Badge> : null}
                    </dd>
                    <dt className="text-muted-foreground">{t("ws.models.asOf")}</dt>
                    <dd className="tabular-nums">{formatDateTime(data.as_of)} UTC</dd>
                  </dl>
                  <div className="overflow-hidden rounded-xl border">
                    <Table>
                      <TableHeader className="bg-muted/50">
                        <TableRow className="hover:bg-transparent">
                          <TableHead className="h-9 px-3 text-xs">{t("ws.models.rateColumns.dimension")}</TableHead>
                          <TableHead className="h-9 px-3 text-right text-xs">{t("ws.models.rateColumns.base")}</TableHead>
                          <TableHead className="h-9 px-3 text-right text-xs">{t("ws.models.rateColumns.discounted")}</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {rates.map(([dimension, rate]) => (
                          <TableRow key={dimension}>
                            <TableCell className="px-3 py-2 align-top">
                              <span className="flex flex-col">
                                <span>{maybeT(`ws.dimensions.${dimension}`) ?? dimension}</span>
                                <span className="text-xs text-muted-foreground">
                                  {t("ws.models.perUnit", { quantity: formatCount(rate.unit_quantity) })}
                                </span>
                              </span>
                            </TableCell>
                            <TableCell className="px-3 py-2 text-right align-top">
                              <Money value={rate.unit_price} />
                            </TableCell>
                            <TableCell className="px-3 py-2 text-right align-top">
                              <Money value={rate.discounted_unit_price} className="font-semibold" />
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                  <p className="text-xs text-muted-foreground">{t("ws.models.pricingHint")}</p>
                </>
              ) : null}
            </div>
          </>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

export default function ModelsPage() {
  const { t, maybeT } = useI18n();
  const workspace = useOrgWorkspace();
  const { orgId } = workspace;
  const canListKeys = hasAnyRole(workspace, "org_owner", "org_developer", "platform_admin");
  const [projectId, setProjectId] = useState(NONE);
  const [keyId, setKeyId] = useState(NONE);
  const [selected, setSelected] = useState<Model | null>(null);
  const projects = useProjectOptions(orgId);
  const keys = useKeyOptions(orgId, canListKeys);
  const context: Context = {
    projectId: projectId === NONE ? undefined : projectId,
    keyId: keyId === NONE ? undefined : keyId,
  };
  const keyOptions = (keys.data ?? []).filter(
    (key) => !key.revoked_at && (context.projectId === undefined || key.project_id === context.projectId),
  );

  const models = useCursorList<Model>({
    queryKey: orgKey(orgId, "models", "list", context.projectId ?? null, context.keyId ?? null),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/models", {
            params: {
              query: {
                organization_id: orgId,
                project_id: context.projectId,
                key_id: context.keyId,
                limit: 50,
                cursor,
              },
            },
          }),
        )
      ).data,
  });

  const projectName = projects.data?.find((p) => p.id === context.projectId)?.name;
  const keyLabel = keyOptions.find((k) => k.id === context.keyId);
  const contextLabel = keyLabel
    ? t("ws.models.contextKey", { name: keyLabel.name })
    : projectName
      ? t("ws.models.contextProject", { name: projectName })
      : t("ws.models.contextOrganization");

  const columns = useMemo<ColumnDef<Model, unknown>[]>(
    () => [
      {
        id: "name",
        header: t("ws.models.columns.name"),
        cell: ({ row }) => (
          <span className="flex flex-col">
            <span className="font-mono text-[12.5px] font-medium">{row.original.public_name}</span>
            <span className="text-xs text-muted-foreground">{row.original.display_name}</span>
          </span>
        ),
      },
      {
        id: "protocols",
        header: t("ws.models.columns.protocols"),
        cell: ({ row }) => (
          <span className="flex flex-wrap gap-1">
            {row.original.protocols.map((protocol) => (
              <Badge key={protocol} variant="outline" className="font-normal">
                {maybeT(`ws.models.protocols.${protocol}`) ?? protocol}
              </Badge>
            ))}
          </span>
        ),
      },
      {
        id: "capabilities",
        header: t("ws.models.columns.capabilities"),
        cell: ({ row }) => {
          const caps = row.original.capabilities;
          const list = [caps?.streaming ? t("ws.models.streaming") : null, caps?.tools ? t("ws.models.tools") : null].filter(Boolean);
          return <span className="text-foreground/80">{list.length ? list.join(" · ") : "—"}</span>;
        },
      },
      {
        id: "limits",
        header: t("ws.models.columns.limits"),
        meta: { align: "right" },
        cell: ({ row }) => (
          <span className="flex flex-col items-end text-xs tabular-nums">
            <span>{t("ws.models.maxInput", { count: formatCount(row.original.max_input_tokens) })}</span>
            <span className="text-muted-foreground">{t("ws.models.maxOutput", { count: formatCount(row.original.max_output_tokens) })}</span>
          </span>
        ),
      },
      {
        id: "dimensions",
        header: t("ws.models.columns.dimensions"),
        cell: ({ row }) => {
          const dims = [...(row.original.token_dimensions ?? []), ...(row.original.tool_dimensions ?? [])];
          return (
            <span className="text-xs text-muted-foreground" title={dims.join(", ")}>
              {t("ws.models.dimensionCount", { count: dims.length })}
            </span>
          );
        },
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("ws.models.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <Button variant="outline" size="sm" onClick={() => setSelected(row.original)}>
            <TagIcon />
            {t("ws.models.viewPricing")}
          </Button>
        ),
      },
    ],
    [t, maybeT],
  );

  return (
    <PageContainer>
      <PageHeader title={t("ws.models.title")} description={t("ws.models.subtitle")} />
      <div className="flex flex-wrap items-end gap-3">
        <Select
          value={projectId}
          onValueChange={(value) => {
            setProjectId(value);
            setKeyId(NONE);
          }}
        >
          <SelectTrigger aria-label={t("ws.models.contextProjectLabel")} className="min-w-56">
            <span className="text-muted-foreground">{t("ws.models.contextProjectLabel")}</span>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={NONE}>{t("ws.models.noProject")}</SelectItem>
            {(projects.data ?? []).map((project) => (
              <SelectItem key={project.id} value={project.id}>
                {project.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {canListKeys ? (
          <Select value={keyId} onValueChange={setKeyId}>
            <SelectTrigger aria-label={t("ws.models.contextKeyLabel")} className="min-w-56">
              <span className="text-muted-foreground">{t("ws.models.contextKeyLabel")}</span>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={NONE}>{t("ws.models.noKey")}</SelectItem>
              {keyOptions.map((key) => (
                <SelectItem key={key.id} value={key.id}>
                  {key.name} <span className="font-mono text-muted-foreground">{key.prefix}…</span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : null}
        <span className="pb-2 text-xs text-muted-foreground">{t("ws.models.contextHint")}</span>
      </div>
      <DataTable
        columns={columns}
        data={models.items}
        loading={models.isPending}
        error={models.error}
        onRetry={() => void models.refetch()}
        getRowId={(row) => row.id}
        minWidth={860}
        empty={<EmptyState icon={<BoxIcon className="size-5" />} title={t("ws.models.emptyTitle")} description={t("ws.models.emptyBody")} />}
        footer={
          models.items.length > 0 ? (
            <LoadMoreFooter
              count={models.items.length}
              hasNext={Boolean(models.hasNextPage)}
              loading={models.isFetchingNextPage}
              onLoadMore={() => void models.fetchNextPage()}
              note={t("ws.models.footerNote")}
            />
          ) : null
        }
      />
      <PricingSheet
        orgId={orgId}
        model={selected}
        context={context}
        contextLabel={contextLabel}
        onClose={() => setSelected(null)}
      />
    </PageContainer>
  );
}
