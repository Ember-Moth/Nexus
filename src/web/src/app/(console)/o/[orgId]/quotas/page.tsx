"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { GaugeIcon, PencilIcon, PlusIcon } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { ConflictAlert, EmptyState, ErrorAlert, Spinner } from "@/components/feedback";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { hasAnyRole, useOrgWorkspace, type Workspace } from "@/components/shell/workspace";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isApiError, isConflict } from "@/lib/api/errors";
import { useCursorList } from "@/lib/api/hooks";
import { orgKey, useProjectOptions } from "@/lib/api/queries";
import type { QuotaPolicy } from "@/lib/api/types";
import { shortId } from "@/lib/format";

type Layer = QuotaPolicy["layer"];
type ScopeType = QuotaPolicy["scope_type"];
type Metric = QuotaPolicy["metric"];

const METRICS: Metric[] = ["rpm", "tpm", "concurrency"];
const SCOPES: ScopeType[] = ["organization", "project", "key"];
const MAX_LIMIT = 2_000_000_000;

/** Owners maintain the customer layer; platform admins maintain the platform layer. */
function writableLayers(workspace: Workspace): Layer[] {
  const layers: Layer[] = [];
  if (hasAnyRole(workspace, "org_owner", "platform_admin")) layers.push("customer");
  if (hasAnyRole(workspace, "platform_admin")) layers.push("platform");
  return layers;
}

interface Target {
  layer: Layer;
  scope_type: ScopeType;
  scope_id: string;
  metric: Metric;
  /** Present when editing: the policy version becomes If-Match. */
  version?: number;
  limit_value?: number;
}

function QuotaDialog({
  orgId,
  target,
  layers,
  onClose,
}: {
  orgId: string;
  target: Target | "new" | null;
  layers: Layer[];
  onClose: () => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const editing = target !== null && target !== "new" ? target : null;
  const open = target !== null;
  const [layer, setLayer] = useState<Layer>(editing?.layer ?? layers[0] ?? "customer");
  const [scopeType, setScopeType] = useState<ScopeType>(editing?.scope_type ?? "organization");
  const [scopeId, setScopeId] = useState(editing?.scope_id ?? "");
  const [metric, setMetric] = useState<Metric>(editing?.metric ?? "rpm");
  const [limit, setLimit] = useState(editing?.limit_value ? String(editing.limit_value) : "");
  const [touched, setTouched] = useState(false);

  const projects = useProjectOptions(orgId, open && scopeType === "project");
  const keys = useQuery({
    queryKey: orgKey(orgId, "api-keys", "options"),
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/api-keys", {
            params: { path: { organization_id: orgId }, query: { limit: 100 } },
          }),
        )
      ).data.items,
    enabled: open && scopeType === "key",
  });

  // PUT on the natural key: without If-Match it only creates (201, or 428 when the policy
  // exists); with If-Match it replaces that version (200, or 412 when stale).
  const save = useMutation({
    mutationFn: async (value: Target & { limit_value: number }) =>
      call(() =>
        nexus.PUT(
          "/api/v1/organizations/{organization_id}/quota-policies/{layer}/{scope_type}/{scope_id}/{metric}",
          {
            params: {
              path: {
                organization_id: orgId,
                layer: value.layer,
                scope_type: value.scope_type,
                scope_id: value.scope_id,
                metric: value.metric,
              },
              header: value.version !== undefined ? { "If-Match": `"${value.version}"` } : undefined,
            },
            body: { limit_value: value.limit_value },
          },
        ),
      ),
    onSuccess: () => {
      toast.success(t("quotas.saved"));
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "quota-policies") });
      onClose();
    },
  });

  const resolvedScope = scopeType === "organization" ? orgId : scopeId;
  const limitValue = /^\d+$/.test(limit.trim()) ? Number(limit.trim()) : NaN;
  const limitOk = Number.isInteger(limitValue) && limitValue >= 1 && limitValue <= MAX_LIMIT;
  const scopeOk = Boolean(resolvedScope);

  const submit = () => {
    setTouched(true);
    if (!limitOk || !scopeOk) return;
    save.mutate({
      layer,
      scope_type: scopeType,
      scope_id: resolvedScope,
      metric,
      version: editing?.version,
      limit_value: limitValue,
    });
  };

  const exists = isApiError(save.error) && save.error.status === 428;

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-[440px]">
        <DialogHeader>
          <DialogTitle>
            {editing ? t("quotas.editTitle", { metric: t(`quotas.metrics.${editing.metric}`) }) : t("quotas.create")}
          </DialogTitle>
          <DialogDescription>{t("quotas.subtitle")}</DialogDescription>
        </DialogHeader>
        {isConflict(save.error) ? (
          <ConflictAlert
            onReload={() => {
              save.reset();
              void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "quota-policies") });
              onClose();
            }}
          >
            <span>{t("policyWrite.stale")}</span>
          </ConflictAlert>
        ) : exists ? (
          <p role="alert" className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
            {t("policyWrite.exists")}
          </p>
        ) : save.error ? (
          <ErrorAlert error={save.error} />
        ) : null}
        <div className="grid grid-cols-2 gap-3">
          <div className="grid gap-2">
            <Label htmlFor="quota-layer">{t("quotas.layer")}</Label>
            <Select value={layer} onValueChange={(value) => setLayer(value as Layer)} disabled={Boolean(editing)}>
              <SelectTrigger id="quota-layer" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(editing ? [editing.layer] : layers).map((value) => (
                  <SelectItem key={value} value={value}>
                    {t(`quotas.layers.${value}`)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="quota-metric">{t("quotas.metric")}</Label>
            <Select value={metric} onValueChange={(value) => setMetric(value as Metric)} disabled={Boolean(editing)}>
              <SelectTrigger id="quota-metric" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {METRICS.map((value) => (
                  <SelectItem key={value} value={value}>
                    {t(`quotas.metrics.${value}`)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        <div className="grid gap-2">
          <Label htmlFor="quota-scope">{t("quotas.scopeType")}</Label>
          <Select
            value={scopeType}
            onValueChange={(value) => {
              setScopeType(value as ScopeType);
              setScopeId("");
            }}
            disabled={Boolean(editing)}
          >
            <SelectTrigger id="quota-scope" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {SCOPES.map((value) => (
                <SelectItem key={value} value={value}>
                  {t(`quotas.scopes.${value}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {scopeType === "project" && !editing ? (
            <Select value={scopeId} onValueChange={setScopeId}>
              <SelectTrigger aria-label={t("quotas.scopeProject")} className="w-full" aria-invalid={(touched && !scopeOk) || undefined}>
                <SelectValue placeholder={t("quotas.selectProject")} />
              </SelectTrigger>
              <SelectContent>
                {(projects.data ?? []).map((project) => (
                  <SelectItem key={project.id} value={project.id}>
                    {project.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : null}
          {scopeType === "key" && !editing ? (
            <Select value={scopeId} onValueChange={setScopeId}>
              <SelectTrigger aria-label={t("quotas.scopeKey")} className="w-full" aria-invalid={(touched && !scopeOk) || undefined}>
                <SelectValue placeholder={t("quotas.selectKey")} />
              </SelectTrigger>
              <SelectContent>
                {(keys.data ?? []).map((key) => (
                  <SelectItem key={key.id} value={key.id}>
                    {key.name} <span className="font-mono text-muted-foreground">{key.prefix}…</span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : null}
          {touched && !scopeOk ? <span className="text-xs text-destructive">{t("quotas.scopeRequired")}</span> : null}
        </div>
        <div className="grid gap-2">
          <Label htmlFor="quota-limit">{t("quotas.limit")}</Label>
          <Input
            id="quota-limit"
            inputMode="numeric"
            value={limit}
            onChange={(event) => setLimit(event.target.value)}
            aria-invalid={(touched && !limitOk) || undefined}
            className="tabular-nums"
          />
          <span className={touched && !limitOk ? "text-xs text-destructive" : "text-xs text-muted-foreground"}>
            {touched && !limitOk ? t("quotas.limitInvalid") : t("quotas.limitHint")}
          </span>
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button type="button" onClick={submit} disabled={save.isPending || isConflict(save.error)}>
            {save.isPending ? <Spinner /> : null}
            {t("common.save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default function QuotasPage() {
  const t = useT();
  const workspace = useOrgWorkspace();
  const { orgId } = workspace;
  const layers = writableLayers(workspace);
  const [target, setTarget] = useState<Target | "new" | null>(null);
  const projects = useProjectOptions(orgId);
  const projectName = useMemo(() => new Map((projects.data ?? []).map((p) => [p.id, p.name])), [projects.data]);

  const quotas = useCursorList<QuotaPolicy>({
    queryKey: orgKey(orgId, "quota-policies"),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/quota-policies", {
            params: { path: { organization_id: orgId }, query: { limit: 50, cursor } },
          }),
        )
      ).data,
  });

  const columns = useMemo<ColumnDef<QuotaPolicy, unknown>[]>(
    () => [
      { header: t("quotas.columns.layer"), accessorKey: "layer", cell: ({ row }) => t(`quotas.layers.${row.original.layer}`) },
      {
        id: "scope",
        header: t("quotas.columns.scope"),
        cell: ({ row }) => {
          const policy = row.original;
          if (policy.scope_type === "organization") return t("quotas.scopes.organization");
          const name = policy.scope_type === "project" ? projectName.get(policy.scope_id) : undefined;
          return (
            <span className="flex flex-col">
              <span>{t(`quotas.scopes.${policy.scope_type}`)}</span>
              <span className="text-xs text-muted-foreground" title={policy.scope_id}>
                {name ?? shortId(policy.scope_id)}
              </span>
            </span>
          );
        },
      },
      { header: t("quotas.columns.metric"), accessorKey: "metric", cell: ({ row }) => t(`quotas.metrics.${row.original.metric}`) },
      {
        header: t("quotas.columns.limit"),
        accessorKey: "limit_value",
        meta: { align: "right" },
        cell: ({ row }) => <span className="font-medium tabular-nums">{row.original.limit_value.toLocaleString("en-US")}</span>,
      },
      {
        header: t("quotas.columns.version"),
        accessorKey: "version",
        cell: ({ row }) => <span className="text-muted-foreground tabular-nums">v{row.original.version}</span>,
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("quotas.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) =>
          layers.includes(row.original.layer) ? (
            <Button variant="outline" size="sm" onClick={() => setTarget({ ...row.original })}>
              <PencilIcon />
              {t("quotas.edit")}
            </Button>
          ) : null,
      },
    ],
    [t, projectName, layers],
  );

  return (
    <PageContainer>
      <PageHeader
        title={t("quotas.title")}
        description={t("quotas.subtitle")}
        actions={
          layers.length > 0 ? (
            <Button onClick={() => setTarget("new")}>
              <PlusIcon />
              {t("quotas.create")}
            </Button>
          ) : null
        }
      />
      <DataTable
        columns={columns}
        data={quotas.items}
        loading={quotas.isPending}
        error={quotas.error}
        onRetry={() => void quotas.refetch()}
        getRowId={(row) => row.id}
        minWidth={760}
        empty={<EmptyState icon={<GaugeIcon className="size-5" />} title={t("quotas.emptyTitle")} description={t("quotas.emptyBody")} />}
        footer={
          quotas.items.length > 0 ? (
            <LoadMoreFooter
              count={quotas.items.length}
              hasNext={Boolean(quotas.hasNextPage)}
              loading={quotas.isFetchingNextPage}
              onLoadMore={() => void quotas.fetchNextPage()}
            />
          ) : null
        }
      />
      {target !== null ? (
        <QuotaDialog
          key={target === "new" ? "new" : `${target.layer}:${target.scope_type}:${target.scope_id}:${target.metric}`}
          orgId={orgId}
          target={target}
          layers={layers}
          onClose={() => setTarget(null)}
        />
      ) : null}
    </PageContainer>
  );
}
