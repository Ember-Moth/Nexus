"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { DownloadIcon } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { DataTable } from "@/components/data-table";
import { EmptyState, ErrorAlert, Spinner } from "@/components/feedback";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { useOrgWorkspace } from "@/components/shell/workspace";
import { StatusBadge, toneFor } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useI18n } from "@/i18n/provider";
import { API_BASE, callAs, nexus } from "@/lib/api/client";
import { pollUntil, useCursorList } from "@/lib/api/hooks";
import { orgKey } from "@/lib/api/queries";
import type { AuditEvent, ExportJob, ExportKind, Page } from "@/lib/api/types";
import { formatDate, formatDateTime, shortId, startOfUtcDay, startOfUtcMonth, utcInputToIso } from "@/lib/format";

import { isFinished, rememberedExports, rememberExport } from "./exports";

function ExportRow({ orgId, exportId }: { orgId: string; exportId: string }) {
  const { t, maybeT } = useI18n();
  // 202 workflow: poll the job until it is ready or failed (2 s, then 5 s, then 10 s).
  const job = useQuery({
    queryKey: orgKey(orgId, "exports", exportId),
    queryFn: async () =>
      (
        await callAs<ExportJob>(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/exports/{export_id}", {
            params: { path: { organization_id: orgId, export_id: exportId } },
          }),
        )
      ).data,
    refetchInterval: pollUntil<ExportJob>(isFinished),
  });
  if (job.error) {
    return (
      <div className="border-t px-4 py-3">
        <ErrorAlert error={job.error} />
      </div>
    );
  }
  const data = job.data;
  const state = data?.state ?? "queued";
  return (
    <div className="flex flex-wrap items-center gap-3 border-t px-4 py-3">
      <span className="flex min-w-[240px] flex-1 flex-col gap-0.5">
        <span className="font-medium">
          {data ? (maybeT(`audit.kinds.${data.kind}`) ?? data.kind) : "…"}
          <span className="ml-2 font-mono text-xs text-muted-foreground">{shortId(exportId)}</span>
        </span>
        <span className="text-xs text-muted-foreground">
          {data
            ? data.state === "ready"
              ? t("audit.exportMeta", { rows: data.row_count ?? 0, expires: `${formatDateTime(data.expires_at, { seconds: false })} UTC` })
              : data.error_code
                ? data.error_code
                : formatDateTime(data.created_at)
            : t("common.loading")}
        </span>
      </span>
      <StatusBadge tone={toneFor(state)}>
        {!isFinished(data) ? <Spinner className="size-3" /> : null}
        {maybeT(`audit.exportStates.${state}`) ?? state}
      </StatusBadge>
      {state === "ready" ? (
        <Button variant="outline" size="sm" asChild>
          <a href={`${API_BASE}/api/v1/organizations/${orgId}/exports/${exportId}/download`} download>
            <DownloadIcon />
            {t("audit.download")}
          </a>
        </Button>
      ) : null}
    </div>
  );
}

function Exports({ orgId }: { orgId: string }) {
  const { t } = useI18n();
  const [ids, setIds] = useState<string[]>([]);
  const [kind, setKind] = useState<ExportKind>("ledger");
  const [from, setFrom] = useState(() => formatDate(startOfUtcMonth()));
  const [to, setTo] = useState(() => formatDate(startOfUtcDay(-1)));

  useEffect(() => {
    const sync = () => setIds(rememberedExports(orgId));
    sync();
    window.addEventListener("nexus-exports", sync);
    return () => window.removeEventListener("nexus-exports", sync);
  }, [orgId]);

  const create = useMutation({
    mutationFn: async () =>
      (
        await callAs<ExportJob>(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/exports", {
            params: { path: { organization_id: orgId } },
            body: {
              kind,
              filters: {
                start: utcInputToIso(from) ?? undefined,
                end: utcInputToIso(to) ?? undefined,
                time_basis: "occurred_at",
              },
            },
          }),
        )
      ).data,
    onSuccess: (job) => rememberExport(orgId, job),
  });

  return (
    <section aria-labelledby="exports" className="overflow-hidden rounded-xl border bg-card">
      <form
        className="flex flex-wrap items-end gap-3 border-b bg-muted/40 p-4"
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
      >
        <h2 id="exports" className="basis-full text-[15px] font-semibold">
          {t("audit.newExport")}
        </h2>
        <div className="grid gap-1.5">
          <Label className="text-xs">{t("audit.kind")}</Label>
          <Select value={kind} onValueChange={(value) => setKind(value as ExportKind)}>
            <SelectTrigger className="w-40 bg-background" aria-label={t("audit.kind")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {(["requests", "charges", "ledger"] as const).map((value) => (
                <SelectItem key={value} value={value}>
                  {t(`audit.kinds.${value}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="export-from" className="text-xs">
            {t("audit.from")}
          </Label>
          <Input id="export-from" type="date" value={from} onChange={(e) => setFrom(e.target.value)} className="w-40 bg-background" />
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="export-to" className="text-xs">
            {t("audit.to")}
          </Label>
          <Input id="export-to" type="date" value={to} onChange={(e) => setTo(e.target.value)} className="w-40 bg-background" />
        </div>
        <Button type="submit" disabled={create.isPending}>
          {create.isPending ? <Spinner /> : null}
          {t("audit.create")}
        </Button>
        {create.error ? <ErrorAlert error={create.error} className="basis-full" /> : null}
      </form>
      {ids.length === 0 ? (
        <p className="px-4 py-6 text-sm text-muted-foreground">{t("audit.exportsEmpty")}</p>
      ) : (
        ids.map((id) => <ExportRow key={id} orgId={orgId} exportId={id} />)
      )}
    </section>
  );
}

export default function AuditPage() {
  const { t } = useI18n();
  const workspace = useOrgWorkspace();
  const { orgId } = workspace;
  const [action, setAction] = useState("");
  const [appliedAction, setAppliedAction] = useState("");

  const events = useCursorList<AuditEvent>({
    queryKey: orgKey(orgId, "audit-events", appliedAction),
    fetchPage: async (cursor) =>
      (
        await callAs<Page<AuditEvent>>(() =>
          nexus.GET("/api/v1/audit-events", {
            params: { query: { organization_id: orgId, action: appliedAction || undefined, limit: 50, cursor } },
          }),
        )
      ).data,
  });

  const columns = useMemo<ColumnDef<AuditEvent, unknown>[]>(
    () => [
      {
        header: t("audit.columns.time"),
        accessorKey: "created_at",
        cell: ({ row }) => <span className="tabular-nums">{formatDateTime(row.original.created_at)}</span>,
      },
      {
        header: t("audit.columns.actor"),
        accessorKey: "actor_id",
        cell: ({ row }) =>
          row.original.actor_id === workspace.me.user.id ? (
            workspace.me.user.login
          ) : row.original.actor_id ? (
            <code className="font-mono text-xs" title={row.original.actor_id}>
              {shortId(row.original.actor_id)}
            </code>
          ) : (
            <span className="text-muted-foreground">{t("audit.system")}</span>
          ),
      },
      {
        header: t("audit.columns.action"),
        accessorKey: "action",
        cell: ({ row }) => (
          <span className="flex flex-col">
            <code className="font-mono text-[12.5px]">{row.original.action}</code>
            <span className="text-xs text-muted-foreground">{row.original.module}</span>
          </span>
        ),
      },
      {
        header: t("audit.columns.resource"),
        accessorKey: "resource_id",
        cell: ({ row }) => (
          <code className="font-mono text-xs text-muted-foreground" title={row.original.resource_id ?? undefined}>
            {shortId(row.original.resource_id)}
          </code>
        ),
      },
      {
        header: t("audit.columns.reason"),
        accessorKey: "reason",
        cell: ({ row }) => <code className="font-mono text-xs">{row.original.reason ?? "—"}</code>,
      },
    ],
    [t, workspace.me.user.id, workspace.me.user.login],
  );

  return (
    <PageContainer>
      <PageHeader title={t("audit.title")} description={t("audit.subtitle")} />
      <Exports orgId={orgId} />
      <section aria-labelledby="events" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-end gap-2">
          <h2 id="events" className="flex-1 text-lg font-semibold">
            {t("audit.eventsTitle")}
          </h2>
          <form
            className="flex gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              setAppliedAction(action.trim());
            }}
          >
            <Input
              aria-label={t("audit.action")}
              placeholder={t("audit.actionPlaceholder")}
              value={action}
              onChange={(event) => setAction(event.target.value)}
              className="w-56 font-mono text-xs"
            />
            <Button type="submit" variant="outline">
              {t("common.refresh")}
            </Button>
          </form>
        </div>
        <DataTable
          columns={columns}
          data={events.items}
          loading={events.isPending}
          error={events.error}
          onRetry={() => void events.refetch()}
          getRowId={(event) => event.id}
          minWidth={860}
          empty={<EmptyState title={t("audit.eventsEmpty")} />}
          footer={
            events.items.length > 0 ? (
              <LoadMoreFooter
                count={events.items.length}
                hasNext={Boolean(events.hasNextPage)}
                loading={events.isFetchingNextPage}
                onLoadMore={() => void events.fetchNextPage()}
              />
            ) : null
          }
        />
      </section>
    </PageContainer>
  );
}
