"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { DownloadIcon, FileTextIcon, ListTreeIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { CopyButton } from "@/components/copy-button";
import { DataTable } from "@/components/data-table";
import { EmptyState, useErrorToast } from "@/components/feedback";
import { Money } from "@/components/money";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { defaultReportFilters, reportQuery, ReportFilters, type ReportFilterState } from "@/components/report-filters";
import { hasAnyRole, useOrgWorkspace } from "@/components/shell/workspace";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatCount, trimDecimal, useKeyNames, useModelNames } from "@/components/workspace/lookups";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useCursorList } from "@/lib/api/hooks";
import { orgKey, useProjectOptions } from "@/lib/api/queries";
import type { components } from "@/lib/api/schema";
import type { ApiKey, Model } from "@/lib/api/types";
import { formatDateTime, shortId } from "@/lib/format";


type ChargeRecord = components["schemas"]["ChargeRecord"];
type ChargeEntry = components["schemas"]["ChargeEntry"];

const ALL_MODELS = "__all";

function IdCell({ id }: { id: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <code className="font-mono text-xs" title={id}>
        {shortId(id)}
      </code>
      <CopyButton value={id} />
    </span>
  );
}

function ChargeSheet({
  record,
  onClose,
  names,
}: {
  record: ChargeRecord | null;
  onClose: () => void;
  names: { project: (id: string) => string; key: (id: string) => string; model: (id: string) => string };
}) {
  const { t, maybeT } = useI18n();
  const charge: ChargeEntry | null = record && record.kind === "charge" ? record : null;
  const lines = charge?.charge_lines ?? [];
  return (
    <Sheet open={record !== null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full gap-0 sm:max-w-[680px]">
        {record ? (
          <>
            <SheetHeader className="border-b">
              <SheetTitle>
                {record.kind === "charge" ? t("ws.charges.sheetChargeTitle") : t("ws.charges.sheetRefundTitle")}
              </SheetTitle>
              <SheetDescription className="tabular-nums">
                {formatDateTime(record.occurred_at)} UTC · <Money value={record.amount} signed currency={record.currency} />
              </SheetDescription>
            </SheetHeader>
            <div className="flex flex-col gap-4 overflow-y-auto p-4">
              <dl className="grid grid-cols-[132px_1fr] gap-x-3 gap-y-2 rounded-md border px-3 py-2.5 text-[13px]">
                <dt className="text-muted-foreground">{t("ws.charges.fields.request")}</dt>
                <dd>
                  <IdCell id={record.request_id} />
                </dd>
                <dt className="text-muted-foreground">{t("ws.charges.fields.project")}</dt>
                <dd>{names.project(record.project_id)}</dd>
                <dt className="text-muted-foreground">{t("ws.charges.fields.key")}</dt>
                <dd>{names.key(record.api_key_id)}</dd>
                <dt className="text-muted-foreground">{t("ws.charges.fields.model")}</dt>
                <dd className="font-mono">{names.model(record.model_id)}</dd>
                <dt className="text-muted-foreground">{t("ws.charges.fields.occurred")}</dt>
                <dd className="tabular-nums">{formatDateTime(record.occurred_at)} UTC</dd>
                <dt className="text-muted-foreground">{t("ws.charges.fields.posted")}</dt>
                <dd className="tabular-nums">{formatDateTime(record.posted_at)} UTC</dd>
                {charge ? (
                  <>
                    <dt className="text-muted-foreground">{t("ws.charges.fields.saleVersion")}</dt>
                    <dd>
                      <IdCell id={charge.sale_price_version_id} />
                    </dd>
                    <dt className="text-muted-foreground">{t("ws.charges.fields.discountVersion")}</dt>
                    <dd>
                      <IdCell id={charge.discount_version_id} />
                    </dd>
                    <dt className="text-muted-foreground">{t("ws.charges.fields.gross")}</dt>
                    <dd>
                      <Money value={charge.gross_amount} />
                    </dd>
                    <dt className="text-muted-foreground">{t("ws.charges.fields.waived")}</dt>
                    <dd>
                      <Money value={charge.waived_amount} />
                    </dd>
                    <dt className="text-muted-foreground">{t("ws.charges.fields.amount")}</dt>
                    <dd>
                      <Money value={charge.amount} className="font-semibold" />
                    </dd>
                  </>
                ) : (
                  <>
                    <dt className="text-muted-foreground">{t("ws.charges.fields.ledgerEntry")}</dt>
                    <dd>{record.kind === "refund" ? <IdCell id={record.ledger_entry_id} /> : "—"}</dd>
                    <dt className="text-muted-foreground">{t("ws.charges.fields.amount")}</dt>
                    <dd>
                      <Money value={record.amount} signed className="font-semibold" />
                    </dd>
                  </>
                )}
              </dl>
              {charge ? (
                lines.length > 0 ? (
                  <div className="overflow-hidden rounded-xl border">
                    <div className="overflow-x-auto">
                      <Table style={{ minWidth: 560 }}>
                        <TableHeader className="bg-muted/50">
                          <TableRow className="hover:bg-transparent">
                            <TableHead className="h-9 px-3 text-xs">{t("ws.charges.lineColumns.dimension")}</TableHead>
                            <TableHead className="h-9 px-3 text-right text-xs">{t("ws.charges.lineColumns.quantity")}</TableHead>
                            <TableHead className="h-9 px-3 text-right text-xs">{t("ws.charges.lineColumns.unitPrice")}</TableHead>
                            <TableHead className="h-9 px-3 text-right text-xs">{t("ws.charges.lineColumns.factor")}</TableHead>
                            <TableHead className="h-9 px-3 text-right text-xs">{t("ws.charges.lineColumns.amount")}</TableHead>
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {lines.map((line) => (
                            <TableRow key={line.dimension}>
                              <TableCell className="px-3 py-2">{maybeT(`ws.dimensions.${line.dimension}`) ?? line.dimension}</TableCell>
                              <TableCell className="px-3 py-2 text-right tabular-nums">{formatCount(line.quantity)}</TableCell>
                              <TableCell className="px-3 py-2 text-right">
                                <span className="flex flex-col items-end">
                                  <Money value={line.unit_price} />
                                  <span className="text-xs text-muted-foreground">
                                    {t("ws.models.perUnit", { quantity: formatCount(line.unit_quantity) })}
                                  </span>
                                </span>
                              </TableCell>
                              <TableCell className="px-3 py-2 text-right tabular-nums">× {trimDecimal(line.discount_factor)}</TableCell>
                              <TableCell className="px-3 py-2 text-right">
                                <Money value={line.unrounded_amount} />
                              </TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </div>
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">{t("ws.charges.noLines")}</p>
                )
              ) : (
                <p className="text-sm text-muted-foreground">{t("ws.charges.refundHint")}</p>
              )}
              {charge ? <p className="text-xs text-muted-foreground">{t("ws.charges.roundingHint")}</p> : null}
            </div>
          </>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

export default function ChargesPage() {
  const { t } = useI18n();
  const workspace = useOrgWorkspace();
  const { orgId } = workspace;
  const showError = useErrorToast();
  const router = useRouter();
  const [filters, setFilters] = useState<ReportFilterState>(defaultReportFilters);
  const [modelId, setModelId] = useState(ALL_MODELS);
  const [selected, setSelected] = useState<ChargeRecord | null>(null);
  const query = { ...reportQuery(filters), model_id: modelId === ALL_MODELS ? undefined : modelId };
  const projects = useProjectOptions(orgId);
  const modelNames = useModelNames(workspace);
  const keyNames = useKeyNames(workspace);
  // Exports and their downloads are for organization finance roles (operations.md); developers
  // see their own projects' charges but cannot export them.
  const canExport = hasAnyRole(workspace, "org_owner", "org_finance", "platform_admin", "platform_finance");

  const charges = useCursorList<ChargeRecord>({
    queryKey: orgKey(orgId, "charges", query),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/charges", {
            params: { path: { organization_id: orgId }, query: { ...query, limit: 50, cursor } },
          }),
        )
      ).data,
  });

  const queryClient = useQueryClient();

  const exportCharges = useMutation({
    mutationFn: async () =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/exports", {
            params: { path: { organization_id: orgId } },
            body: { kind: "charges", filters: query },
          }),
        )
      ).data,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "exports", "list") });
      toast.success(t("wallet.exportQueued"), {
        action: { label: t("nav.items.audit"), onClick: () => router.push(`/o/${orgId}/audit`) },
      });
    },
    onError: (error) => showError(error),
  });

  const names = useMemo(() => {
    const projectName = new Map((projects.data ?? []).map((p) => [p.id, p.name]));
    return {
      project: (id: string) => projectName.get(id) ?? shortId(id),
      key: (id: string) => {
        const key: ApiKey | undefined = keyNames.get(id);
        return key ? `${key.name} · ${key.prefix}…` : shortId(id);
      },
      model: (id: string) => {
        const model: Model | undefined = modelNames.get(id);
        return model ? model.public_name : shortId(id);
      },
    };
  }, [projects.data, keyNames, modelNames]);

  const basis = filters.basis;
  const columns = useMemo<ColumnDef<ChargeRecord, unknown>[]>(
    () => [
      {
        id: "time",
        header: basis === "posted_at" ? t("ws.charges.columns.posted") : t("ws.charges.columns.occurred"),
        cell: ({ row }) => (
          <span className="tabular-nums">
            {formatDateTime(basis === "posted_at" ? row.original.posted_at : row.original.occurred_at)}
          </span>
        ),
      },
      {
        id: "kind",
        header: t("ws.charges.columns.kind"),
        cell: ({ row }) =>
          row.original.kind === "refund" ? (
            <StatusBadge tone="info">{t("ws.charges.kinds.refund")}</StatusBadge>
          ) : (
            <StatusBadge tone="neutral">{t("ws.charges.kinds.charge")}</StatusBadge>
          ),
      },
      {
        id: "request",
        header: t("ws.charges.columns.request"),
        cell: ({ row }) => <IdCell id={row.original.request_id} />,
      },
      {
        id: "project",
        header: t("ws.charges.columns.projectKey"),
        cell: ({ row }) => (
          <span className="flex flex-col">
            <span>{names.project(row.original.project_id)}</span>
            <span className="text-xs text-muted-foreground" title={row.original.api_key_id}>
              {names.key(row.original.api_key_id)}
            </span>
          </span>
        ),
      },
      {
        id: "model",
        header: t("ws.charges.columns.model"),
        cell: ({ row }) => (
          <code className="font-mono text-xs" title={row.original.model_id}>
            {names.model(row.original.model_id)}
          </code>
        ),
      },
      {
        id: "versions",
        header: t("ws.charges.columns.versions"),
        cell: ({ row }) =>
          row.original.kind === "charge" ? (
            <span className="flex flex-col font-mono text-[11.5px] text-muted-foreground">
              <span title={row.original.sale_price_version_id}>
                {t("ws.charges.saleShort")} {shortId(row.original.sale_price_version_id)}
              </span>
              <span title={row.original.discount_version_id}>
                {t("ws.charges.discountShort")} {shortId(row.original.discount_version_id)}
              </span>
            </span>
          ) : (
            <span className="text-muted-foreground">—</span>
          ),
      },
      {
        id: "amount",
        header: t("ws.charges.columns.amount"),
        meta: { align: "right" },
        cell: ({ row }) => (
          <Money
            value={row.original.amount}
            signed={row.original.kind === "refund"}
            className={row.original.kind === "refund" ? "font-medium text-blue-800" : "font-medium"}
          />
        ),
      },
      {
        id: "detail",
        header: () => <span className="sr-only">{t("ws.charges.columns.detail")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <Button variant="ghost" size="sm" onClick={() => setSelected(row.original)}>
            <ListTreeIcon />
            {t("ws.charges.detail")}
          </Button>
        ),
      },
    ],
    [t, basis, names],
  );

  const models = [...modelNames.values()].sort((a, b) => a.public_name.localeCompare(b.public_name));

  return (
    <PageContainer>
      <PageHeader
        title={t("ws.charges.title")}
        description={t("ws.charges.subtitle")}
        actions={
          canExport ? (
            <Button variant="outline" onClick={() => exportCharges.mutate()} disabled={exportCharges.isPending}>
              <DownloadIcon />
              {t("wallet.export")}
            </Button>
          ) : null
        }
      />
      <div className="flex flex-wrap items-end gap-3">
        <ReportFilters
          value={filters}
          onChange={setFilters}
          projects={projects.data ?? []}
          allProjectsLabel={t("keys.allProjects")}
        />
        {models.length > 0 ? (
          <Select value={modelId} onValueChange={setModelId}>
            <SelectTrigger aria-label={t("ws.common.model")} className="min-w-48">
              <span className="text-muted-foreground">{t("ws.common.model")}</span>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_MODELS}>{t("ws.common.allModels")}</SelectItem>
              {models.map((model) => (
                <SelectItem key={model.id} value={model.id}>
                  <span className="font-mono">{model.public_name}</span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : null}
      </div>
      <DataTable
        columns={columns}
        data={charges.items}
        loading={charges.isPending}
        error={charges.error}
        onRetry={() => void charges.refetch()}
        getRowId={(row) => `${row.kind}:${row.id}`}
        minWidth={1040}
        empty={<EmptyState icon={<FileTextIcon className="size-5" />} title={t("ws.charges.emptyTitle")} description={t("ws.charges.emptyBody")} />}
        footer={
          charges.items.length > 0 ? (
            <LoadMoreFooter
              count={charges.items.length}
              hasNext={Boolean(charges.hasNextPage)}
              loading={charges.isFetchingNextPage}
              onLoadMore={() => void charges.fetchNextPage()}
              note={t("ws.charges.footerNote")}
            />
          ) : null
        }
      />
      <ChargeSheet record={selected} onClose={() => setSelected(null)} names={names} />
    </PageContainer>
  );
}
