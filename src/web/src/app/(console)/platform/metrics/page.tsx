"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCwIcon } from "lucide-react";
import type { ReactNode } from "react";

import { ErrorAlert, Spinner } from "@/components/feedback";
import { PageContainer, PageHeader } from "@/components/page";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useI18n } from "@/i18n/provider";
import { ApiError } from "@/lib/api/errors";
import { formatTime } from "@/lib/format";
import { cn } from "@/lib/utils";

// GET /internal/metrics (through the /api/metrics BFF route) returns nested JSON: process
// counters since start-up, current database counts and 24-hour averages. Known metrics are
// grouped by topic; anything else the API adds is still rendered under "其他指标".

type Json = string | number | boolean | null | Json[] | { [key: string]: Json };
type JsonObject = { [key: string]: Json };

interface Snapshot {
  data: JsonObject;
  fetchedAt: string;
}

const REFRESH_MS = 15_000;

const isObject = (value: unknown): value is JsonObject => typeof value === "object" && value !== null && !Array.isArray(value);

/** Scalars keyed by dotted path, and maps of records (e.g. process.http by method) as tables. */
function flatten(value: JsonObject, prefix = "", scalars = new Map<string, Json>(), tables = new Map<string, JsonObject>()) {
  for (const [key, item] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (isObject(item)) {
      const children = Object.values(item);
      if (children.length > 0 && children.every(isObject)) tables.set(path, item);
      else flatten(item, path, scalars, tables);
    } else {
      scalars.set(path, item);
    }
  }
  return { scalars, tables };
}

type Group = { id: "requests" | "database" | "publication" | "usage" | "payments" | "anomalies"; paths: string[] };

const GROUPS: Group[] = [
  { id: "requests", paths: ["process.http"] },
  { id: "database", paths: ["process.database.acquisitions", "process.database.acquire_seconds", "process.pool_checked_out", "process.waiting_locks"] },
  { id: "publication", paths: ["publication.publication_backlog", "publication.publication_lag_seconds", "publication.lagging_nodes"] },
  {
    id: "usage",
    paths: ["billing.usage_backlog", "billing.waiting_for_facts", "billing.usage_review", "billing.event_receive_seconds", "billing.settlement_seconds"],
  },
  {
    id: "payments",
    paths: [
      "billing.payment_query_ms",
      "billing.notice_receive_seconds",
      "billing.payment_confirmation_to_credit_seconds",
      "billing.payment_posting_seconds",
    ],
  },
  {
    id: "anomalies",
    paths: [
      "billing.overdue_holds",
      "billing.duplicate_events",
      "billing.conflicting_events",
      "billing.consumption_cases",
      "billing.payment_cases",
      "billing.reconciliation_differences",
    ],
  },
];

/** Counts that indicate something needs attention whenever they are above zero. */
const ALERTING = new Set([
  "waiting_locks",
  "lagging_nodes",
  "usage_review",
  "overdue_holds",
  "duplicate_events",
  "conflicting_events",
  "consumption_cases",
  "payment_cases",
  "reconciliation_differences",
]);

const lastKey = (path: string) => path.slice(path.lastIndexOf(".") + 1);

const integerFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const decimalFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

function useFormatValue() {
  const { t } = useI18n();
  return (key: string, value: Json): { text: string; unit?: string; empty?: boolean } => {
    if (value === null || value === undefined) return { text: t("pf.metrics.noSamples"), empty: true };
    if (typeof value === "boolean") return { text: value ? t("common.yes") : t("common.no") };
    if (typeof value !== "number") return { text: String(value) };
    if (key.endsWith("_ms")) return value >= 1000 ? { text: decimalFormat.format(value / 1000), unit: "s" } : { text: decimalFormat.format(value), unit: "ms" };
    if (key.endsWith("_seconds") || key === "seconds") {
      return value < 1 ? { text: decimalFormat.format(value * 1000), unit: "ms" } : { text: decimalFormat.format(value), unit: "s" };
    }
    return { text: Number.isInteger(value) ? integerFormat.format(value) : decimalFormat.format(value) };
  };
}

function useLabel() {
  const { maybeT } = useI18n();
  return (path: string) => maybeT(`pf.metrics.labels.${lastKey(path)}`) ?? lastKey(path).replaceAll("_", " ");
}

function MetricCard({ path, value }: { path: string; value: Json }) {
  const { t, maybeT } = useI18n();
  const format = useFormatValue();
  const label = useLabel();
  const key = lastKey(path);
  const shown = format(key, value);
  const alerting = ALERTING.has(key) && typeof value === "number" && value > 0;
  const hint = maybeT(`pf.metrics.hints.${key}`);
  return (
    <div className={cn("flex min-w-0 flex-col gap-1 rounded-lg border bg-card px-3.5 py-3", alerting && "border-amber-300 bg-amber-50/60")}>
      <span className="flex items-start gap-2 text-xs text-muted-foreground">
        <span className="flex-1">{label(path)}</span>
        {alerting ? <StatusBadge tone="warning">{t("pf.metrics.attention")}</StatusBadge> : null}
      </span>
      <span className={cn("text-xl font-semibold tabular-nums", shown.empty && "text-sm font-normal text-muted-foreground")}>
        {shown.text}
        {shown.unit ? <span className="ml-1 text-xs font-normal text-muted-foreground">{shown.unit}</span> : null}
      </span>
      <span className="font-mono text-[10.5px] text-muted-foreground/80" title={hint}>
        {path}
      </span>
    </div>
  );
}

/** Average of a cumulative total over a cumulative count, as a derived card. */
function DerivedCard({ label, total, count }: { label: string; total: Json | undefined; count: Json | undefined }) {
  const { t } = useI18n();
  const value = typeof total === "number" && typeof count === "number" && count > 0 ? (total / count) * 1000 : null;
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-lg border border-dashed bg-card px-3.5 py-3">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className={cn("text-xl font-semibold tabular-nums", value === null && "text-sm font-normal text-muted-foreground")}>
        {value === null ? t("pf.metrics.noSamples") : decimalFormat.format(value)}
        {value === null ? null : <span className="ml-1 text-xs font-normal text-muted-foreground">ms</span>}
      </span>
      <span className="text-[10.5px] text-muted-foreground/80">{t("pf.metrics.derived")}</span>
    </div>
  );
}

function MapTable({ path, value }: { path: string; value: JsonObject }) {
  const { t } = useI18n();
  const format = useFormatValue();
  const label = useLabel();
  const rows = Object.entries(value) as [string, JsonObject][];
  const columns = Array.from(new Set(rows.flatMap(([, record]) => Object.keys(record))));
  const http = columns.includes("requests") && columns.includes("seconds");
  return (
    <div className="overflow-hidden rounded-lg border bg-card">
      <div className="overflow-x-auto">
        <Table>
          <caption className="sr-only">{label(path)}</caption>
          <TableHeader className="bg-muted/50">
            <TableRow className="hover:bg-transparent">
              <TableHead className="h-9 px-3 text-xs">{t(path === "process.http" ? "pf.metrics.method" : "pf.metrics.key")}</TableHead>
              {columns.map((column) => (
                <TableHead key={column} className="h-9 px-3 text-right text-xs">
                  {label(column)}
                </TableHead>
              ))}
              {http ? (
                <>
                  <TableHead className="h-9 px-3 text-right text-xs">{t("pf.metrics.errorRate")}</TableHead>
                  <TableHead className="h-9 px-3 text-right text-xs">{t("pf.metrics.avgLatency")}</TableHead>
                </>
              ) : null}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map(([name, record]) => {
              const requests = typeof record.requests === "number" ? record.requests : 0;
              const errors = typeof record.errors === "number" ? record.errors : 0;
              const seconds = typeof record.seconds === "number" ? record.seconds : 0;
              return (
                <TableRow key={name}>
                  <TableCell className="px-3 font-mono text-xs">{name}</TableCell>
                  {columns.map((column) => {
                    const shown = format(column, record[column] ?? null);
                    return (
                      <TableCell key={column} className="px-3 text-right tabular-nums">
                        {shown.text}
                        {shown.unit ? <span className="ml-0.5 text-xs text-muted-foreground">{shown.unit}</span> : null}
                      </TableCell>
                    );
                  })}
                  {http ? (
                    <>
                      <TableCell className={cn("px-3 text-right tabular-nums", errors > 0 && "text-amber-800")}>
                        {requests > 0 ? `${decimalFormat.format((errors / requests) * 100)}%` : "—"}
                      </TableCell>
                      <TableCell className="px-3 text-right tabular-nums">
                        {requests > 0 ? decimalFormat.format((seconds / requests) * 1000) : "—"}
                        {requests > 0 ? <span className="ml-0.5 text-xs text-muted-foreground">ms</span> : null}
                      </TableCell>
                    </>
                  ) : null}
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}

function GroupSection({ title, note, children }: { title: string; note?: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2.5">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-[15px] font-semibold">{title}</h2>
        {note ? <span className="text-xs text-muted-foreground">{note}</span> : null}
      </div>
      {children}
    </section>
  );
}

function MetricsBody({ data }: { data: JsonObject }) {
  const { t } = useI18n();
  const { scalars, tables } = flatten(data);
  const used = new Set(GROUPS.flatMap((group) => group.paths));
  const otherScalars = [...scalars.entries()].filter(([path]) => !used.has(path));
  const otherTables = [...tables.entries()].filter(([path]) => !used.has(path));

  return (
    <div className="flex flex-col gap-6">
      {GROUPS.map((group) => {
        const present = group.paths.filter((path) => scalars.has(path) || tables.has(path));
        if (present.length === 0) return null;
        return (
          <GroupSection key={group.id} title={t(`pf.metrics.groups.${group.id}.title`)} note={t(`pf.metrics.groups.${group.id}.note`)}>
            {present
              .filter((path) => tables.has(path))
              .map((path) => (
                <MapTable key={path} path={path} value={tables.get(path)!} />
              ))}
            <div className="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-2.5">
              {present
                .filter((path) => scalars.has(path))
                .map((path) => (
                  <MetricCard key={path} path={path} value={scalars.get(path)!} />
                ))}
              {group.id === "database" && scalars.has("process.database.acquisitions") ? (
                <DerivedCard
                  label={t("pf.metrics.avgAcquire")}
                  total={scalars.get("process.database.acquire_seconds")}
                  count={scalars.get("process.database.acquisitions")}
                />
              ) : null}
            </div>
          </GroupSection>
        );
      })}
      {otherScalars.length || otherTables.length ? (
        <GroupSection title={t("pf.metrics.groups.other.title")} note={t("pf.metrics.groups.other.note")}>
          {otherTables.map(([path, value]) => (
            <MapTable key={path} path={path} value={value} />
          ))}
          {otherScalars.length ? (
            <div className="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-2.5">
              {otherScalars.map(([path, value]) => (
                <MetricCard key={path} path={path} value={value} />
              ))}
            </div>
          ) : null}
        </GroupSection>
      ) : null}
    </div>
  );
}

export default function MetricsPage() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const metrics = useQuery({
    queryKey: ["internal-metrics"],
    queryFn: async (): Promise<Snapshot> => {
      let response: Response;
      try {
        response = await fetch("/api/metrics", {
          credentials: "same-origin",
          cache: "no-store",
          headers: { accept: "application/json" },
        });
      } catch (cause) {
        throw ApiError.network(cause);
      }
      const body: unknown = await response.json().catch(() => null);
      if (!response.ok) {
        // The BFF cleared the cookies; re-reading /me lets the shell's 401 handling take over.
        if (response.status === 401) void queryClient.invalidateQueries({ queryKey: ["me"] });
        throw ApiError.fromResponse(response, body);
      }
      if (!isObject(body)) throw ApiError.fromResponse(response, null);
      return { data: body, fetchedAt: new Date().toISOString() };
    },
    refetchInterval: REFRESH_MS,
    refetchIntervalInBackground: false,
  });

  const snapshot = metrics.data;
  return (
    <PageContainer>
      <PageHeader
        title={t("pf.metrics.title")}
        description={t("pf.metrics.subtitle")}
        actions={
          <>
            <span className="self-center text-xs text-muted-foreground tabular-nums">
              {snapshot
                ? t("pf.metrics.updatedAt", { time: `${formatTime(snapshot.fetchedAt)} ${t("common.utc")}` })
                : null}
              {" · "}
              {t("pf.metrics.autoRefresh", { seconds: REFRESH_MS / 1000 })}
            </span>
            <Button variant="outline" onClick={() => void metrics.refetch()} disabled={metrics.isFetching}>
              {metrics.isFetching ? <Spinner /> : <RefreshCwIcon />}
              {t("common.refresh")}
            </Button>
          </>
        }
      />
      {metrics.error ? (
        <ErrorAlert
          error={metrics.error}
          title={snapshot ? t("pf.metrics.staleTitle") : undefined}
          onRetry={() => void metrics.refetch()}
        />
      ) : null}
      {snapshot ? (
        <MetricsBody data={snapshot.data} />
      ) : metrics.isPending ? (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(200px,1fr))] gap-2.5">
          {Array.from({ length: 8 }, (_, index) => (
            <Skeleton key={index} className="h-[86px] rounded-lg" />
          ))}
        </div>
      ) : null}
    </PageContainer>
  );
}
