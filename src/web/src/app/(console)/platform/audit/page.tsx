"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useMemo, useState } from "react";

import { CopyButton } from "@/components/copy-button";
import { DataTable } from "@/components/data-table";
import { EmptyState } from "@/components/feedback";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { OrgFilter, useOrgName, useUserLabels } from "@/components/platform-finance/scope";
import { UUID_PATTERN } from "@/components/platform-finance/validation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useCursorList } from "@/lib/api/hooks";
import type { components } from "@/lib/api/schema";
import { formatDateTime, shortId } from "@/lib/format";

type AuditEvent = components["schemas"]["AuditEvent"];

/** Filters live in the URL (?org=&action=&trace=) so a filtered view can be shared. */
function useFilters() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const update = useCallback(
    (changes: Record<string, string | undefined>) => {
      const search = new URLSearchParams(params.toString());
      for (const [key, value] of Object.entries(changes)) {
        if (value) search.set(key, value);
        else search.delete(key);
      }
      const query = search.toString();
      router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
    },
    [params, pathname, router],
  );
  return {
    orgId: params.get("org") ?? undefined,
    action: params.get("action") ?? "",
    traceId: params.get("trace") ?? "",
    update,
  };
}

function AuditContent() {
  const { t } = useI18n();
  const { orgId, action, traceId, update } = useFilters();
  const [actionInput, setActionInput] = useState(action);
  const [traceInput, setTraceInput] = useState(traceId);
  const [traceInvalid, setTraceInvalid] = useState(false);
  const orgName = useOrgName();
  const userLabel = useUserLabels();

  const events = useCursorList<AuditEvent>({
    queryKey: ["audit-events", "platform", orgId ?? null, action, traceId],
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/audit-events", {
            params: {
              query: {
                organization_id: orgId,
                action: action || undefined,
                trace_id: traceId || undefined,
                limit: 50,
                cursor,
              },
            },
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
        cell: ({ row }) => {
          const id = row.original.actor_id;
          if (!id) return <span className="text-muted-foreground">{t("audit.system")}</span>;
          const login = userLabel(id);
          return login ? (
            <span title={id}>{login}</span>
          ) : (
            <code className="font-mono text-xs" title={id}>
              {shortId(id)}
            </code>
          );
        },
      },
      {
        header: t("pf.common.organization"),
        accessorKey: "organization_id",
        cell: ({ row }) =>
          row.original.organization_id ? (
            <button
              type="button"
              className="text-left hover:underline"
              title={row.original.organization_id}
              onClick={() => update({ org: row.original.organization_id ?? undefined })}
            >
              {orgName(row.original.organization_id)}
            </button>
          ) : (
            <span className="text-muted-foreground">{t("pf.audit.platformScope")}</span>
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
      {
        header: t("pf.audit.trace"),
        accessorKey: "trace_id",
        cell: ({ row }) =>
          row.original.trace_id ? (
            <span className="inline-flex items-center gap-0.5">
              <button
                type="button"
                className="font-mono text-xs text-muted-foreground hover:text-foreground hover:underline"
                title={t("pf.audit.filterByTrace")}
                onClick={() => {
                  setTraceInput(row.original.trace_id ?? "");
                  update({ trace: row.original.trace_id ?? undefined });
                }}
              >
                {shortId(row.original.trace_id)}
              </button>
              <CopyButton value={row.original.trace_id} />
            </span>
          ) : (
            <span className="text-muted-foreground">—</span>
          ),
      },
    ],
    [t, orgName, userLabel, update],
  );

  const filtered = Boolean(orgId || action || traceId);
  return (
    <PageContainer>
      <PageHeader title={t("pf.audit.title")} description={t("pf.audit.subtitle")} />
      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          const trace = traceInput.trim();
          if (trace && !UUID_PATTERN.test(trace)) {
            setTraceInvalid(true);
            return;
          }
          setTraceInvalid(false);
          update({ action: actionInput.trim() || undefined, trace: trace || undefined });
        }}
      >
        <OrgFilter value={orgId} onChange={(next) => update({ org: next })} />
        <Input
          aria-label={t("audit.action")}
          placeholder={t("audit.actionPlaceholder")}
          value={actionInput}
          maxLength={100}
          onChange={(event) => setActionInput(event.target.value)}
          className="w-56 font-mono text-xs"
        />
        <Input
          aria-label={t("pf.audit.trace")}
          placeholder={t("pf.audit.tracePlaceholder")}
          value={traceInput}
          onChange={(event) => setTraceInput(event.target.value)}
          aria-invalid={traceInvalid || undefined}
          className="w-80 font-mono text-xs"
        />
        <Button type="submit" variant="outline">
          {t("pf.audit.apply")}
        </Button>
        {filtered ? (
          <Button
            type="button"
            variant="ghost"
            onClick={() => {
              setActionInput("");
              setTraceInput("");
              setTraceInvalid(false);
              update({ org: undefined, action: undefined, trace: undefined });
            }}
          >
            {t("common.clearFilters")}
          </Button>
        ) : null}
        {traceInvalid ? (
          <span role="alert" className="basis-full text-xs text-destructive">
            {t("pf.common.fieldErrors.uuid")}
          </span>
        ) : null}
      </form>
      <DataTable
        columns={columns}
        data={events.items}
        loading={events.isPending}
        error={events.error}
        onRetry={() => void events.refetch()}
        getRowId={(event) => event.id}
        minWidth={1040}
        empty={<EmptyState title={filtered ? t("states.filteredEmpty") : t("audit.eventsEmpty")} />}
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
    </PageContainer>
  );
}

export default function PlatformAuditPage() {
  return (
    <Suspense>
      <AuditContent />
    </Suspense>
  );
}
