"use client";

import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { BarChart3Icon } from "lucide-react";
import { useMemo, useState } from "react";

import { DataTable } from "@/components/data-table";
import { EmptyState } from "@/components/feedback";
import { Money } from "@/components/money";
import { PageContainer, PageHeader } from "@/components/page";
import { defaultReportFilters, reportQuery, ReportFilters, type ReportFilterState } from "@/components/report-filters";
import { hasAnyRole, useOrgWorkspace } from "@/components/shell/workspace";
import { StatusBadge } from "@/components/status-badge";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { formatCount, useKeyNames, useModelNames } from "@/components/workspace/lookups";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { orgKey, useProjectOptions } from "@/lib/api/queries";
import type { UsageBucket, UsageReport } from "@/lib/api/types";
import { formatDateTime, shortId } from "@/lib/format";
import { addMoney } from "@/lib/money";

type GroupBy = UsageReport["group_by"];
const GROUPS: GroupBy[] = ["organization", "project", "key", "model", "day"];
const ALL_MODELS = "__all";

/** Canonical order of metering dimensions; unknown ones are appended alphabetically. */
const DIMENSION_ORDER = [
  "input_uncached_tokens",
  "input_cache_read_tokens",
  "input_cache_write_5m_tokens",
  "input_cache_write_30m_tokens",
  "input_cache_write_1h_tokens",
  "input_cache_write_12h_tokens",
  "output_tokens",
];

function sortDimensions(dimensions: Iterable<string>) {
  return [...dimensions].sort((a, b) => {
    const ia = DIMENSION_ORDER.indexOf(a);
    const ib = DIMENSION_ORDER.indexOf(b);
    if (ia !== -1 || ib !== -1) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    return a.localeCompare(b);
  });
}

function sum(items: UsageBucket[], pick: (bucket: UsageBucket) => string | null | undefined) {
  return items.reduce((total, bucket) => addMoney(total, pick(bucket) ?? "0"), "0");
}

export default function UsagePage() {
  const { t, maybeT } = useI18n();
  const workspace = useOrgWorkspace();
  const { orgId } = workspace;
  const canSeeCost = hasAnyRole(workspace, "platform_finance");
  const [groupBy, setGroupBy] = useState<GroupBy>("project");
  const [includeCost, setIncludeCost] = useState(false);
  const [filters, setFilters] = useState<ReportFilterState>(defaultReportFilters);
  const [modelId, setModelId] = useState(ALL_MODELS);
  const projects = useProjectOptions(orgId);
  const modelNames = useModelNames(workspace);
  const keyNames = useKeyNames(workspace);
  const withCost = canSeeCost && includeCost;

  const query = {
    ...reportQuery(filters),
    model_id: modelId === ALL_MODELS ? undefined : modelId,
    group_by: groupBy,
    include_cost: withCost || undefined,
  };

  const report = useQuery({
    queryKey: orgKey(orgId, "usage", "report", query),
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/usage", {
            params: { path: { organization_id: orgId }, query },
          }),
        )
      ).data,
    placeholderData: (previous) => previous,
  });

  // After a failed request the previous report stays only as placeholder; don't show it as current.
  const data = report.error ? undefined : report.data;
  const items = useMemo(() => data?.items ?? [], [data]);
  const currency = data?.currency;
  const shownGroup = data?.group_by ?? groupBy;
  const costShown = Boolean(withCost && items.some((bucket) => bucket.confirmed_cost != null));

  const projectName = useMemo(() => new Map((projects.data ?? []).map((p) => [p.id, p.name])), [projects.data]);
  const dimensions = useMemo(
    () => sortDimensions(new Set(items.flatMap((bucket) => Object.keys(bucket.quantities)))),
    [items],
  );

  const columns = useMemo<ColumnDef<UsageBucket, unknown>[]>(() => {
    const groupLabel = (group: string) => {
      switch (shownGroup) {
        case "organization":
          return { name: workspace.organization?.name ?? shortId(group), id: group };
        case "project":
          return { name: projectName.get(group) ?? shortId(group), id: group };
        case "key": {
          const key = keyNames.get(group);
          return { name: key ? key.name : shortId(group), id: group, extra: key ? `${key.prefix}…` : undefined };
        }
        case "model": {
          const model = modelNames.get(group);
          return { name: model ? model.public_name : shortId(group), id: group, mono: true };
        }
        default:
          // "2026-11-14 00:00:00+00:00" → "2026-11-14"
          return { name: /^\d{4}-\d{2}-\d{2}/.test(group) ? group.slice(0, 10) : group, id: undefined };
      }
    };
    const list: ColumnDef<UsageBucket, unknown>[] = [
      {
        id: "group",
        header: t(`ws.usage.groupColumn.${shownGroup}`),
        cell: ({ row }) => {
          const label = groupLabel(row.original.group);
          return (
            <span className="flex flex-col">
              <span className={label.mono ? "font-mono text-[12.5px] font-medium" : "font-medium"} title={label.id}>
                {label.name}
              </span>
              {label.extra ? <span className="font-mono text-xs text-muted-foreground">{label.extra}</span> : null}
            </span>
          );
        },
      },
      {
        id: "requests",
        header: t("ws.usage.columns.requests"),
        meta: { align: "right" },
        cell: ({ row }) => (
          <span className="flex flex-col items-end tabular-nums">
            <span>{formatCount(row.original.requests)}</span>
            {row.original.held_requests > 0 ? (
              <span className="text-xs text-muted-foreground">
                {t("ws.usage.heldRequests", { count: formatCount(row.original.held_requests) })}
              </span>
            ) : null}
          </span>
        ),
      },
      ...dimensions.map<ColumnDef<UsageBucket, unknown>>((dimension) => ({
        id: `q:${dimension}`,
        header: () => (
          <span title={dimension}>{maybeT(`ws.dimensions.${dimension}`) ?? dimension}</span>
        ),
        meta: { align: "right" },
        cell: ({ row }) => (
          <span className="tabular-nums">{formatCount(row.original.quantities[dimension] ?? 0)}</span>
        ),
      })),
      {
        id: "held",
        header: t("ws.usage.columns.held"),
        meta: { align: "right" },
        cell: ({ row }) => <Money value={row.original.held_amount} className="text-muted-foreground" />,
      },
      {
        id: "charged",
        header: t("ws.usage.columns.charged"),
        meta: { align: "right" },
        cell: ({ row }) => <Money value={row.original.charged_amount} />,
      },
      {
        id: "refunds",
        header: t("ws.usage.columns.refunds"),
        meta: { align: "right" },
        cell: ({ row }) => <Money value={row.original.refund_amount} className="text-muted-foreground" />,
      },
      {
        id: "net",
        header: t("ws.usage.columns.net"),
        meta: { align: "right" },
        cell: ({ row }) => <Money value={row.original.net_charge} className="font-semibold" />,
      },
    ];
    if (costShown) {
      list.push(
        {
          id: "cost",
          header: t("ws.usage.columns.cost"),
          meta: { align: "right" },
          cell: ({ row }) => <Money value={row.original.confirmed_cost} />,
        },
        {
          id: "margin",
          header: t("ws.usage.columns.margin"),
          meta: { align: "right" },
          cell: ({ row }) => <Money value={row.original.margin} signed className="font-medium" />,
        },
        {
          id: "provisional",
          header: t("ws.usage.columns.provisional"),
          cell: ({ row }) =>
            row.original.provisional ? (
              <StatusBadge tone="warning">{t("ws.usage.provisional")}</StatusBadge>
            ) : (
              <StatusBadge tone="success">{t("ws.usage.settled")}</StatusBadge>
            ),
        },
      );
    }
    return list;
  }, [t, maybeT, shownGroup, dimensions, costShown, projectName, keyNames, modelNames, workspace.organization?.name]);

  const totals = useMemo(
    () => ({
      requests: items.reduce((total, bucket) => total + bucket.requests, 0),
      held: sum(items, (b) => b.held_amount),
      charged: sum(items, (b) => b.charged_amount),
      refunds: sum(items, (b) => b.refund_amount),
      net: sum(items, (b) => b.net_charge),
      cost: costShown ? sum(items, (b) => b.confirmed_cost) : undefined,
      margin: costShown ? sum(items, (b) => b.margin) : undefined,
    }),
    [items, costShown],
  );

  const summary: { label: string; value: string | undefined; signed?: boolean; money: boolean }[] = [
    { label: t("ws.usage.summary.requests"), value: String(totals.requests), money: false },
    { label: t("ws.usage.summary.charged"), value: totals.charged, money: true },
    { label: t("ws.usage.summary.refunds"), value: totals.refunds, money: true },
    { label: t("ws.usage.summary.net"), value: totals.net, money: true },
    { label: t("ws.usage.summary.held"), value: totals.held, money: true },
    ...(costShown
      ? [
          { label: t("ws.usage.summary.cost"), value: totals.cost, money: true },
          { label: t("ws.usage.summary.margin"), value: totals.margin, money: true, signed: true },
        ]
      : []),
  ];

  const watermark = data?.watermark;
  const models = [...modelNames.values()].sort((a, b) => a.public_name.localeCompare(b.public_name));

  return (
    <PageContainer>
      <PageHeader title={t("ws.usage.title")} description={t("ws.usage.subtitle")} />
      <div className="flex flex-wrap items-center gap-3">
        <span id="usage-group" className="text-sm font-medium">
          {t("ws.usage.groupBy")}
        </span>
        <ToggleGroup
          type="single"
          variant="outline"
          aria-labelledby="usage-group"
          value={groupBy}
          onValueChange={(value) => value && setGroupBy(value as GroupBy)}
        >
          {GROUPS.map((group) => (
            <ToggleGroupItem key={group} value={group}>
              {t(`ws.usage.groups.${group}`)}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        {canSeeCost ? (
          <Label className="ml-auto flex items-center gap-2 text-sm">
            <Switch checked={includeCost} onCheckedChange={setIncludeCost} />
            {t("ws.usage.includeCost")}
          </Label>
        ) : null}
      </div>
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
      {report.error ? null : (
        <div className="flex flex-wrap overflow-hidden rounded-xl border">
          {summary.map((item) => (
            <div key={item.label} className="flex flex-[1_1_150px] flex-col gap-1 border-r px-5 py-3.5 last:border-r-0">
              <span className="text-xs text-muted-foreground">
                {item.label}
                {item.money && currency ? ` · ${currency}` : ""}
              </span>
              {!data ? (
                <Skeleton className="h-6 w-24" />
              ) : item.money ? (
                <Money value={item.value} signed={item.signed} className="text-lg font-semibold" extraClassName="text-sm" />
              ) : (
                <span className="text-lg font-semibold tabular-nums">{formatCount(item.value)}</span>
              )}
            </div>
          ))}
        </div>
      )}
      {watermark ? (
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 rounded-lg border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          <span>{t("ws.usage.watermark.title")}</span>
          <span className="tabular-nums">
            {t("ws.usage.watermark.source", { time: formatDateTime(watermark.latest_source_at) })}
          </span>
          <span className="tabular-nums">
            {t("ws.usage.watermark.projected", { time: formatDateTime(watermark.projected_at) })}
          </span>
          <span className={watermark.pending_requests > 0 ? "font-medium text-amber-800" : undefined}>
            {t("ws.usage.watermark.pending", { count: formatCount(watermark.pending_requests) })}
          </span>
          <span className="basis-full">{t("ws.usage.watermark.hint")}</span>
        </div>
      ) : null}
      <DataTable
        columns={columns}
        data={items}
        loading={report.isPending}
        error={report.error}
        onRetry={() => void report.refetch()}
        getRowId={(row) => row.group}
        minWidth={Math.max(860, 640 + dimensions.length * 120 + (costShown ? 320 : 0))}
        empty={<EmptyState icon={<BarChart3Icon className="size-5" />} title={t("ws.usage.emptyTitle")} description={t("ws.usage.emptyBody")} />}
        footer={
          items.length > 0 ? (
            <div className="border-t px-4 py-3 text-xs text-muted-foreground">
              {costShown ? t("ws.usage.costHint") : t("ws.usage.footerHint")}
            </div>
          ) : null
        }
      />
    </PageContainer>
  );
}
