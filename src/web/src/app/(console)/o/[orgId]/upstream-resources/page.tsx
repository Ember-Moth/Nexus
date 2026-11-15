"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { CloudIcon, EllipsisIcon, Trash2Icon } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";

import { CopyButton } from "@/components/copy-button";
import { DataTable } from "@/components/data-table";
import { EmptyState } from "@/components/feedback";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { hasAnyRole, useOrgWorkspace } from "@/components/shell/workspace";
import { StatusBadge, type Tone } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useKeyOptions } from "@/components/workspace/lookups";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useCursorList } from "@/lib/api/hooks";
import { orgKey, useProjectOptions } from "@/lib/api/queries";
import type { ApiKey, UpstreamResource, UpstreamResourceKind, UpstreamResourceState } from "@/lib/api/types";
import { formatDateTime, shortId } from "@/lib/format";

import { PurgeDialog, type PurgeTarget } from "./purge-dialog";

const ALL = "__all";
const KINDS: UpstreamResourceKind[] = ["response", "file", "conversation", "vector_store", "container", "batch"];
const STATES: UpstreamResourceState[] = ["retained", "pending", "purged", "failed", "deleted"];
const STATE_TONES: Record<UpstreamResourceState, Tone> = {
  retained: "neutral",
  pending: "info",
  purged: "success",
  failed: "danger",
  deleted: "neutral",
};

function keyLabel(key: ApiKey | undefined, id: string) {
  return key ? `${key.name} · ${key.prefix}…` : shortId(id);
}

function UpstreamResourcesView() {
  const t = useT();
  const workspace = useOrgWorkspace();
  const { orgId } = workspace;
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  // Deleting needs an owner or a platform administrator; developers only list.
  const canPurge = hasAnyRole(workspace, "org_owner", "platform_admin");
  const [projectId, setProjectId] = useState<string>(ALL);
  const [kind, setKind] = useState<string>(ALL);
  const [state, setState] = useState<string>(ALL);
  const [target, setTarget] = useState<PurgeTarget | null>(null);
  // The key filter lives in the URL so the keys page can link here.
  const apiKeyId = params.get("api_key_id") ?? ALL;
  const setApiKeyId = (value: string) =>
    router.replace(value === ALL ? pathname : `${pathname}?api_key_id=${value}`, { scroll: false });

  const projects = useProjectOptions(orgId);
  const keys = useKeyOptions(orgId);
  const projectFilter = projectId === ALL ? undefined : projectId;
  const keyFilter = apiKeyId === ALL ? undefined : apiKeyId;
  const kindFilter = kind === ALL ? undefined : (kind as UpstreamResourceKind);
  const stateFilter = state === ALL ? undefined : (state as UpstreamResourceState);

  const resources = useCursorList<UpstreamResource>({
    queryKey: orgKey(orgId, "upstream-resources", "list", projectFilter, keyFilter, kindFilter, stateFilter),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/upstream-resources", {
            params: {
              path: { organization_id: orgId },
              query: {
                project_id: projectFilter,
                api_key_id: keyFilter,
                kind: kindFilter,
                state: stateFilter,
                limit: 50,
                cursor,
              },
            },
          }),
        )
      ).data,
  });

  const keyById = useMemo(() => new Map((keys.data ?? []).map((key) => [key.id, key])), [keys.data]);
  const projectName = useMemo(() => new Map((projects.data ?? []).map((p) => [p.id, p.name])), [projects.data]);
  const visibleKeys = (keys.data ?? []).filter((key) => !projectFilter || key.project_id === projectFilter);
  const filtered = Boolean(projectFilter || keyFilter || kindFilter || stateFilter);

  const columns = useMemo<ColumnDef<UpstreamResource, unknown>[]>(
    () => [
      {
        id: "handle",
        header: t("ws.upstream.columns.handle"),
        cell: ({ row }) =>
          row.original.handle ? (
            <span className="flex items-center gap-1">
              <code className="font-mono text-[12.5px]">{row.original.handle}</code>
              <CopyButton value={row.original.handle} />
            </span>
          ) : (
            <span className="text-muted-foreground">{t("ws.upstream.unsaved")}</span>
          ),
      },
      {
        id: "kind",
        header: t("ws.upstream.columns.kind"),
        cell: ({ row }) => t(`ws.upstream.kinds.${row.original.kind}`),
      },
      {
        id: "key",
        header: t("ws.upstream.columns.key"),
        cell: ({ row }) => {
          const key = keyById.get(row.original.api_key_id);
          return (
            <span className="flex flex-col">
              <span className="flex items-center gap-1.5">
                {key ? key.name : t("ws.upstream.unknownKey", { id: shortId(row.original.api_key_id) })}
                {key?.revoked_at ? <StatusBadge tone="danger">{t("ws.upstream.revokedKey")}</StatusBadge> : null}
              </span>
              <span className="text-xs text-muted-foreground">
                {row.original.project_id ? (projectName.get(row.original.project_id) ?? "—") : "—"}
              </span>
            </span>
          );
        },
      },
      {
        id: "created",
        header: t("ws.upstream.columns.created"),
        cell: ({ row }) => (
          <span className="text-muted-foreground tabular-nums">
            {formatDateTime(row.original.created_at, { seconds: false })}
          </span>
        ),
      },
      {
        id: "state",
        header: t("ws.upstream.columns.state"),
        cell: ({ row }) => {
          const { purge } = row.original;
          return (
            <span className="flex flex-col items-start gap-1">
              <StatusBadge tone={STATE_TONES[row.original.state]}>
                {t(`ws.upstream.states.${row.original.state}`)}
              </StatusBadge>
              {purge ? (
                <span className="flex max-w-[300px] flex-col text-xs whitespace-normal text-muted-foreground">
                  <span>
                    {t(`ws.upstream.reasons.${purge.reason}`)}
                    {purge.attempts > 0 ? ` · ${t("ws.upstream.attempts", { count: purge.attempts })}` : null}
                  </span>
                  {purge.last_error ? (
                    <span>
                      {t("ws.upstream.lastError", { code: purge.last_error })}
                      {purge.next_attempt_at
                        ? ` · ${t("ws.upstream.nextAttempt", { time: formatDateTime(purge.next_attempt_at, { seconds: false }) })}`
                        : null}
                    </span>
                  ) : null}
                </span>
              ) : null}
            </span>
          );
        },
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("ws.upstream.columns.actions")}</span>,
        meta: { className: "w-12" },
        cell: ({ row }) => {
          const { state: resourceState, handle, api_key_id: keyId } = row.original;
          if (!canPurge || (resourceState !== "retained" && resourceState !== "failed")) return null;
          return (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={t("ws.upstream.menuLabel", { handle: handle ?? t("ws.upstream.unsaved") })}
                >
                  <EllipsisIcon />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-48">
                <DropdownMenuItem
                  variant="destructive"
                  onSelect={() =>
                    setTarget({
                      scope: "resource",
                      resource: row.original,
                      keyLabel: keyLabel(keyById.get(keyId), keyId),
                    })
                  }
                >
                  <Trash2Icon />
                  {resourceState === "failed" ? t("ws.upstream.retryOne") : t("ws.upstream.purgeOne")}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          );
        },
      },
    ],
    [t, keyById, projectName, canPurge],
  );

  return (
    <PageContainer>
      <PageHeader
        title={t("ws.upstream.title")}
        description={t("ws.upstream.subtitle")}
        actions={
          canPurge ? (
            <Button
              variant="outline"
              className="text-destructive"
              disabled={!keyFilter}
              title={keyFilter ? undefined : t("ws.upstream.purgeKeyHint")}
              onClick={() =>
                keyFilter &&
                setTarget({ scope: "key", apiKeyId: keyFilter, keyLabel: keyLabel(keyById.get(keyFilter), keyFilter) })
              }
            >
              <Trash2Icon />
              {t("ws.upstream.purgeKey")}
            </Button>
          ) : null
        }
      />
      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={projectId}
          onValueChange={(value) => {
            setProjectId(value);
            setApiKeyId(ALL);
          }}
        >
          <SelectTrigger aria-label={t("ws.upstream.filterProject")} className="min-w-44">
            <span className="text-muted-foreground">{t("ws.upstream.filterProject")}</span>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>{t("ws.upstream.allProjects")}</SelectItem>
            {(projects.data ?? []).map((project) => (
              <SelectItem key={project.id} value={project.id}>
                {project.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={apiKeyId} onValueChange={setApiKeyId}>
          <SelectTrigger aria-label={t("ws.upstream.filterKey")} className="min-w-52">
            <span className="text-muted-foreground">{t("ws.upstream.filterKey")}</span>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>{t("ws.upstream.allKeys")}</SelectItem>
            {visibleKeys.map((key) => (
              <SelectItem key={key.id} value={key.id}>
                {keyLabel(key, key.id)}
                {key.revoked_at ? ` · ${t("ws.upstream.revokedKey")}` : ""}
              </SelectItem>
            ))}
            {keyFilter && !keyById.has(keyFilter) ? (
              <SelectItem value={keyFilter}>{shortId(keyFilter)}</SelectItem>
            ) : null}
          </SelectContent>
        </Select>
        <Select value={kind} onValueChange={setKind}>
          <SelectTrigger aria-label={t("ws.upstream.filterKind")} className="min-w-40">
            <span className="text-muted-foreground">{t("ws.upstream.filterKind")}</span>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>{t("ws.upstream.allKinds")}</SelectItem>
            {KINDS.map((value) => (
              <SelectItem key={value} value={value}>
                {t(`ws.upstream.kinds.${value}`)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={state} onValueChange={setState}>
          <SelectTrigger aria-label={t("ws.upstream.filterState")} className="min-w-36">
            <span className="text-muted-foreground">{t("ws.upstream.filterState")}</span>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>{t("ws.upstream.allStates")}</SelectItem>
            {STATES.map((value) => (
              <SelectItem key={value} value={value}>
                {t(`ws.upstream.states.${value}`)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {!canPurge ? <p className="text-sm text-muted-foreground">{t("ws.upstream.ownerOnly")}</p> : null}
      <DataTable
        columns={columns}
        data={resources.items}
        loading={resources.isPending}
        error={resources.error}
        onRetry={() => void resources.refetch()}
        getRowId={(resource) => `${resource.kind}:${resource.id}`}
        minWidth={980}
        rowClassName={(resource) =>
          resource.state === "purged" || resource.state === "deleted" ? "text-muted-foreground" : undefined
        }
        empty={
          <EmptyState
            icon={<CloudIcon className="size-5" />}
            title={filtered ? t("ws.upstream.emptyFiltered") : t("ws.upstream.emptyTitle")}
            description={filtered ? undefined : t("ws.upstream.emptyBody")}
          />
        }
        footer={
          resources.items.length > 0 ? (
            <LoadMoreFooter
              count={resources.items.length}
              hasNext={Boolean(resources.hasNextPage)}
              loading={resources.isFetchingNextPage}
              onLoadMore={() => void resources.fetchNextPage()}
            />
          ) : null
        }
      />
      <PurgeDialog orgId={orgId} target={target} onOpenChange={(open) => !open && setTarget(null)} />
    </PageContainer>
  );
}

export default function UpstreamResourcesPage() {
  return (
    <Suspense>
      <UpstreamResourcesView />
    </Suspense>
  );
}
