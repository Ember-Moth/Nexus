"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CircleAlertIcon, InfoIcon, PencilIcon, PlusIcon, SearchIcon, ShieldIcon } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { DataTable } from "@/components/data-table";
import { ConflictAlert, EmptyState, ErrorAlert, Spinner } from "@/components/feedback";
import { hasAnyRole, useOrgWorkspace, type Workspace } from "@/components/shell/workspace";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { collectPages, useKeyOptions, useModelCatalog, useModelNames } from "@/components/workspace/lookups";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isApiError, isConflict } from "@/lib/api/errors";
import { orgKey, useProjectOptions } from "@/lib/api/queries";
import type { components } from "@/lib/api/schema";
import type { ApiKey, Model } from "@/lib/api/types";
import { formatDateTime, shortId } from "@/lib/format";
import { cn } from "@/lib/utils";

export type AccessPolicy = components["schemas"]["AccessPolicyView"];
type Layer = components["schemas"]["AccessPolicyCreate"]["layer"];
type ScopeType = components["schemas"]["AccessPolicyCreate"]["scope_type"];

const SCOPES: ScopeType[] = ["organization", "project", "key"];

// ---------------------------------------------------------------------------------------------
// Data

/**
 * Every policy visible to the caller (up to 1000). The list has no scope filter, so the project
 * tab filters on the client; developers only receive organization-level policies and those of
 * projects they are granted.
 */
export function useAccessPolicies(orgId: string, enabled = true) {
  return useQuery({
    queryKey: orgKey(orgId, "access-policies", "all"),
    queryFn: async () =>
      collectPages<AccessPolicy>(async (cursor) =>
        (
          await call(() =>
            nexus.GET("/api/v1/organizations/{organization_id}/access-policies", {
              params: { path: { organization_id: orgId }, query: { limit: 100, cursor } },
            }),
          )
        ).data,
      ),
    enabled,
  });
}

/**
 * Who may write which policies (catalog-api.md): platform policies need platform_admin; owners
 * narrow the organization and its projects/keys; developers narrow their granted projects and
 * keys only. The API re-checks every write.
 */
export function policyPermissions(workspace: Workspace) {
  const admin = hasAnyRole(workspace, "platform_admin");
  const owner = hasAnyRole(workspace, "org_owner");
  const developer = hasAnyRole(workspace, "org_developer");
  const layers: Layer[] = admin ? ["customer", "platform"] : owner || developer ? ["customer"] : [];
  return {
    admin,
    canView: admin || owner || developer,
    layers,
    scopesFor(layer: Layer): ScopeType[] {
      if (layer === "platform") return admin ? SCOPES : [];
      return admin || owner ? SCOPES : developer ? ["project", "key"] : [];
    },
    canWrite(policy: Pick<AccessPolicy, "layer" | "scope_type">) {
      if (policy.layer === "platform") return admin;
      if (policy.scope_type === "organization") return admin || owner;
      return admin || owner || developer;
    },
  };
}
export type PolicyPermissions = ReturnType<typeof policyPermissions>;

interface PolicyTarget {
  layer: Layer;
  scopeType: ScopeType;
  scopeId: string;
  projectId: string | null;
  keyId: string | null;
}

/**
 * The models a policy at `target` may allow, mirroring the API: the intersection of every other
 * policy on its path, starting from the platform organization baseline (no baseline = nothing).
 * Platform policies are only bounded by other platform policies. `null` = unbounded (the
 * platform baseline itself).
 */
function parentBound(policies: AccessPolicy[], target: PolicyTarget): Set<string> | null {
  if (target.layer === "platform" && target.scopeType === "organization") return null;
  const applicable = policies.filter(
    (p) =>
      p.scope_type === "organization" ||
      (p.scope_type === "project" && target.projectId !== null && p.project_id === target.projectId) ||
      (p.scope_type === "key" && target.keyId !== null && p.api_key_id === target.keyId),
  );
  const baseline = applicable.find((p) => p.layer === "platform" && p.scope_type === "organization");
  if (!baseline) return new Set();
  const allowed = new Set(baseline.allowed_models);
  for (const policy of applicable) {
    if (target.layer === "platform" && policy.layer !== "platform") continue;
    if (policy.layer === target.layer && policy.scope_type === target.scopeType && policy.scope_id === target.scopeId) continue;
    const own = new Set(policy.allowed_models);
    for (const id of allowed) if (!own.has(id)) allowed.delete(id);
  }
  return allowed;
}

export interface PolicyNames {
  project: (id: string | null | undefined) => string;
  key: (id: string | null | undefined) => string;
  model: (id: string) => Model | undefined;
}

export function usePolicyNames(orgId: string, workspace: Workspace, modelNames: Map<string, Model>): PolicyNames {
  const canListKeys = hasAnyRole(workspace, "org_owner", "org_developer", "platform_admin");
  const projects = useProjectOptions(orgId);
  const keys = useKeyOptions(orgId, canListKeys);
  return useMemo(() => {
    const projectName = new Map((projects.data ?? []).map((p) => [p.id, p.name]));
    const keyById = new Map<string, ApiKey>((keys.data ?? []).map((k) => [k.id, k]));
    return {
      project: (id) => (id ? (projectName.get(id) ?? shortId(id)) : "—"),
      key: (id) => {
        if (!id) return "—";
        const key = keyById.get(id);
        return key ? `${key.name} (${key.prefix}…)` : shortId(id);
      },
      model: (id) => modelNames.get(id),
    };
  }, [projects.data, keys.data, modelNames]);
}

// ---------------------------------------------------------------------------------------------
// Presentation

export function InheritanceNote({ className }: { className?: string }) {
  const { t } = useI18n();
  return (
    <div className={cn("flex gap-2.5 rounded-lg border bg-muted/40 px-3 py-2.5 text-[13px]", className)}>
      <InfoIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
      <ul className="flex flex-col gap-1 text-foreground/80">
        <li>{t("ws.policies.rules.baseline")}</li>
        <li>{t("ws.policies.rules.intersection")}</li>
        <li>{t("ws.policies.rules.inherit")}</li>
        <li>{t("ws.policies.rules.noDelete")}</li>
      </ul>
    </div>
  );
}

export function MissingBaseline() {
  const { t } = useI18n();
  return (
    <div role="alert" className="flex gap-2.5 rounded-md border border-amber-200 bg-amber-50 px-3 py-2.5 text-sm text-amber-900">
      <CircleAlertIcon className="mt-0.5 size-4 shrink-0 text-amber-700" aria-hidden />
      <span>{t("ws.policies.missingBaseline")}</span>
    </div>
  );
}

function ModelChips({ ids, names, max = 4 }: { ids: string[]; names: PolicyNames; max?: number }) {
  const { t } = useI18n();
  if (ids.length === 0) return <StatusBadge tone="danger">{t("ws.policies.denyAll")}</StatusBadge>;
  const shown = ids.slice(0, max);
  return (
    <span className="flex flex-wrap items-center gap-1" title={ids.map((id) => names.model(id)?.public_name ?? id).join("\n")}>
      {shown.map((id) => {
        const model = names.model(id);
        return (
          <Badge key={id} variant="outline" className="font-mono font-normal">
            {model?.public_name ?? shortId(id)}
          </Badge>
        );
      })}
      {ids.length > max ? <span className="text-xs text-muted-foreground">{t("ws.policies.moreModels", { count: ids.length - max })}</span> : null}
    </span>
  );
}

export function PolicyTable({
  policies,
  names,
  permissions,
  onEdit,
  loading,
  error,
  onRetry,
  empty,
}: {
  policies: AccessPolicy[];
  names: PolicyNames;
  permissions: PolicyPermissions;
  onEdit: (policy: AccessPolicy) => void;
  loading?: boolean;
  error?: unknown;
  onRetry?: () => void;
  empty?: ReactNode;
}) {
  const { t, maybeT } = useI18n();
  const columns = useMemo<ColumnDef<AccessPolicy, unknown>[]>(
    () => [
      {
        id: "layer",
        header: t("ws.policies.columns.layer"),
        cell: ({ row }) => (
          <Badge variant={row.original.layer === "platform" ? "secondary" : "outline"}>
            {maybeT(`ws.policies.layers.${row.original.layer}`) ?? row.original.layer}
          </Badge>
        ),
      },
      {
        id: "scope",
        header: t("ws.policies.columns.scope"),
        cell: ({ row }) => {
          const policy = row.original;
          const type = maybeT(`ws.policies.scopes.${policy.scope_type}`) ?? policy.scope_type;
          if (policy.scope_type === "organization") return type;
          return (
            <span className="flex flex-col">
              <span>{type}</span>
              <span className="text-xs text-muted-foreground" title={policy.scope_id}>
                {policy.scope_type === "project"
                  ? names.project(policy.project_id ?? policy.scope_id)
                  : `${names.key(policy.api_key_id ?? policy.scope_id)} · ${names.project(policy.project_id)}`}
              </span>
            </span>
          );
        },
      },
      {
        id: "models",
        header: t("ws.policies.columns.models"),
        cell: ({ row }) => <ModelChips ids={row.original.allowed_models} names={names} />,
      },
      {
        id: "version",
        header: t("ws.policies.columns.version"),
        cell: ({ row }) => (
          <span className="flex flex-col text-muted-foreground tabular-nums">
            <span>v{row.original.version}</span>
            <span className="text-xs">{formatDateTime(row.original.created_at, { seconds: false })}</span>
          </span>
        ),
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("ws.policies.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) =>
          permissions.canWrite(row.original) ? (
            <Button variant="outline" size="sm" onClick={() => onEdit(row.original)}>
              <PencilIcon />
              {t("ws.policies.edit")}
            </Button>
          ) : null,
      },
    ],
    [t, maybeT, names, permissions, onEdit],
  );
  return (
    <DataTable
      columns={columns}
      data={policies}
      loading={loading}
      error={error}
      onRetry={onRetry}
      getRowId={(row) => row.id}
      minWidth={820}
      empty={empty ?? <EmptyState icon={<ShieldIcon className="size-5" />} title={t("ws.policies.emptyTitle")} />}
    />
  );
}

// ---------------------------------------------------------------------------------------------
// Model picker

interface Candidate {
  id: string;
  model?: Model;
}

function ModelPicker({
  candidates,
  value,
  onChange,
  bounded,
}: {
  candidates: Candidate[];
  value: string[];
  onChange: (next: string[]) => void;
  bounded: boolean;
}) {
  const { t } = useI18n();
  const [search, setSearch] = useState("");
  const selected = new Set(value);
  const needle = search.trim().toLowerCase();
  const shown = needle
    ? candidates.filter(
        (c) =>
          c.id.includes(needle) ||
          c.model?.public_name.toLowerCase().includes(needle) ||
          c.model?.display_name.toLowerCase().includes(needle),
      )
    : candidates;
  const toggle = (id: string, checked: boolean) => {
    const next = new Set(selected);
    if (checked) next.add(id);
    else next.delete(id);
    onChange(candidates.map((c) => c.id).filter((cid) => next.has(cid)));
  };

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[200px] flex-1">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
          <Input
            aria-label={t("ws.policies.searchModels")}
            placeholder={t("ws.policies.searchModels")}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            className="h-8 pl-8"
          />
        </div>
        <Button type="button" variant="ghost" size="sm" onClick={() => onChange(candidates.map((c) => c.id))}>
          {t("ws.policies.selectAll")}
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={() => onChange([])}>
          {t("ws.policies.clear")}
        </Button>
      </div>
      <div className="max-h-64 overflow-y-auto rounded-md border">
        {shown.length === 0 ? (
          <p className="px-3 py-6 text-center text-sm text-muted-foreground">
            {candidates.length === 0 ? t("ws.policies.noCandidates") : t("ws.policies.noMatch")}
          </p>
        ) : (
          <ul className="divide-y">
            {shown.map((candidate) => {
              const inputId = `policy-model-${candidate.id}`;
              const model = candidate.model;
              return (
                <li key={candidate.id}>
                  <Label htmlFor={inputId} className="flex cursor-pointer items-center gap-3 px-3 py-2 font-normal hover:bg-muted/60">
                    <Checkbox
                      id={inputId}
                      checked={selected.has(candidate.id)}
                      onCheckedChange={(checked) => toggle(candidate.id, checked === true)}
                    />
                    <span className="flex min-w-0 flex-1 flex-col">
                      <span className="truncate font-mono text-[12.5px] font-medium" title={candidate.id}>
                        {model?.public_name ?? shortId(candidate.id)}
                      </span>
                      <span className="truncate text-xs text-muted-foreground">
                        {model ? model.display_name : t("ws.policies.unknownModel")}
                      </span>
                    </span>
                    {model && model.status !== "active" ? (
                      <StatusBadge tone="neutral">{t("ws.policies.inactiveModel")}</StatusBadge>
                    ) : model && !model.configured ? (
                      <StatusBadge tone="warning">{t("ws.policies.unconfiguredModel")}</StatusBadge>
                    ) : null}
                  </Label>
                </li>
              );
            })}
          </ul>
        )}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
        <span>{t("ws.policies.selectedCount", { count: value.length, total: candidates.length })}</span>
        <span>{bounded ? t("ws.policies.boundedHint") : t("ws.policies.unboundedHint")}</span>
      </div>
      {value.length === 0 ? (
        <p className="flex items-center gap-1.5 text-xs font-medium text-red-700">
          <CircleAlertIcon className="size-3.5" aria-hidden />
          {t("ws.policies.emptyWarning")}
        </p>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// Create / edit dialog

const schema = z
  .object({
    layer: z.enum(["platform", "customer"]),
    scopeType: z.enum(["organization", "project", "key"]),
    scopeId: z.string(),
    models: z.array(z.string()).max(1000),
  })
  .superRefine((value, ctx) => {
    if (value.scopeType !== "organization" && !value.scopeId) {
      ctx.addIssue({ code: "custom", path: ["scopeId"], message: "required" });
    }
  });
type Values = z.infer<typeof schema>;

export type PolicyDialogTarget =
  | { kind: "create"; projectId?: string }
  | { kind: "edit"; policy: AccessPolicy };

export function PolicyDialog({
  orgId,
  target,
  policies,
  permissions,
  names,
  onClose,
}: {
  orgId: string;
  target: PolicyDialogTarget;
  policies: AccessPolicy[];
  permissions: PolicyPermissions;
  names: PolicyNames;
  onClose: () => void;
}) {
  const { t, maybeT } = useI18n();
  const queryClient = useQueryClient();
  const editing = target.kind === "edit" ? target.policy : null;
  const fixedProject = target.kind === "create" ? target.projectId : undefined;

  // Editing reads the single policy for its ETag (If-Match) and current models.
  const detail = useQuery({
    queryKey: orgKey(orgId, "access-policies", "detail", editing?.id),
    queryFn: async () =>
      call(() =>
        nexus.GET("/api/v1/organizations/{organization_id}/access-policies/{policy_id}", {
          params: { path: { organization_id: orgId, policy_id: editing!.id } },
        }),
      ),
    enabled: editing !== null,
    staleTime: 0,
    refetchOnWindowFocus: false,
  });
  const current = detail.data?.data ?? editing;
  const etag = editing ? (detail.data?.etag ?? `"${current!.version}"`) : null;

  const defaultLayer: Layer = editing ? (editing.layer as Layer) : (permissions.layers[0] ?? "customer");
  const defaultScope: ScopeType = editing
    ? (editing.scope_type as ScopeType)
    : fixedProject
      ? "project"
      : (permissions.scopesFor(defaultLayer)[0] ?? "project");
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: {
      layer: defaultLayer,
      scopeType: defaultScope,
      scopeId: editing ? editing.scope_id : (fixedProject ?? ""),
      models: editing ? editing.allowed_models : [],
    },
    values: current
      ? {
          layer: current.layer as Layer,
          scopeType: current.scope_type as ScopeType,
          scopeId: current.scope_id,
          models: current.allowed_models,
        }
      : undefined,
  });
  const layer = useWatch({ control: form.control, name: "layer" });
  const scopeType = useWatch({ control: form.control, name: "scopeType" });
  const scopeId = useWatch({ control: form.control, name: "scopeId" });
  const models = useWatch({ control: form.control, name: "models" });

  const projects = useProjectOptions(orgId, !editing && scopeType === "project" && !fixedProject);
  const keys = useKeyOptions(orgId, !editing && scopeType === "key");
  const keyOptions = (keys.data ?? []).filter(
    (key) => !key.revoked_at && (fixedProject === undefined || key.project_id === fixedProject),
  );

  // Candidate models: the parent bound (plus anything already selected), or the full active
  // catalog for the platform baseline, which only platform admins can write.
  const resolvedScopeId = scopeType === "organization" ? orgId : scopeId;
  const targetProject = editing
    ? (editing.project_id ?? null)
    : scopeType === "project"
      ? scopeId || null
      : scopeType === "key"
        ? (keyOptions.find((key) => key.id === scopeId)?.project_id ?? null)
        : null;
  const bound = parentBound(policies, {
    layer,
    scopeType,
    scopeId: resolvedScopeId,
    projectId: targetProject,
    keyId: scopeType === "key" ? scopeId || null : null,
  });
  const fullCatalog = useModelCatalog(undefined, {}, permissions.admin && bound === null);
  const candidates = useMemo<Candidate[]>(() => {
    const ids = new Set<string>(
      bound === null ? (fullCatalog.data ?? []).filter((m) => m.status === "active").map((m) => m.id) : bound,
    );
    for (const id of current?.allowed_models ?? []) ids.add(id);
    return [...ids]
      .map((id) => ({ id, model: names.model(id) ?? fullCatalog.data?.find((m) => m.id === id) }))
      .sort((a, b) => (a.model?.public_name ?? `~${a.id}`).localeCompare(b.model?.public_name ?? `~${b.id}`));
  }, [bound, fullCatalog.data, current?.allowed_models, names]);

  const baselineMissing =
    !(layer === "platform" && scopeType === "organization") &&
    !policies.some((p) => p.layer === "platform" && p.scope_type === "organization");
  const duplicate =
    !editing &&
    Boolean(resolvedScopeId) &&
    policies.some((p) => p.layer === layer && p.scope_type === scopeType && p.scope_id === resolvedScopeId);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: orgKey(orgId, "access-policies") });
  const invalidateCatalog = () => queryClient.invalidateQueries({ queryKey: orgKey(orgId, "models") });

  const save = useMutation({
    mutationFn: async (values: Values) => {
      if (editing) {
        return (
          await call(() =>
            nexus.PATCH("/api/v1/organizations/{organization_id}/access-policies/{policy_id}", {
              params: { path: { organization_id: orgId, policy_id: editing.id }, header: { "If-Match": etag! } },
              body: { allowed_models: values.models },
            }),
          )
        ).data;
      }
      return (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/access-policies", {
            params: { path: { organization_id: orgId } },
            body: {
              layer: values.layer,
              scope_type: values.scopeType,
              scope_id: values.scopeType === "organization" ? null : values.scopeId,
              allowed_models: values.models,
            },
          }),
        )
      ).data;
    },
    onSuccess: () => {
      toast.success(editing ? t("ws.policies.saved") : t("ws.policies.created"));
      void invalidate();
      void invalidateCatalog();
      onClose();
    },
  });

  const conflict = isConflict(save.error);
  const exists = !editing && isApiError(save.error) && save.error.status === 409 && save.error.code === "conflict";
  const forbidden = isApiError(save.error) && save.error.status === 403;
  // A different scope has a different parent bound, so earlier selections no longer apply.
  const clearModels = () => form.setValue("models", []);
  const selectScope = (onChange: (value: string) => void) => (value: string) => {
    onChange(value);
    clearModels();
  };
  const scopes = editing
    ? [editing.scope_type as ScopeType]
    : permissions.scopesFor(layer).filter((scope) => !fixedProject || scope !== "organization");
  const errors = form.formState.errors;

  const scopeName = editing
    ? editing.scope_type === "organization"
      ? t("ws.policies.scopes.organization")
      : editing.scope_type === "project"
        ? names.project(editing.project_id ?? editing.scope_id)
        : names.key(editing.api_key_id ?? editing.scope_id)
    : null;

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-[600px]">
        <form noValidate className="grid gap-5" onSubmit={form.handleSubmit((values) => save.mutate(values))}>
          <DialogHeader>
            <DialogTitle>{editing ? t("ws.policies.editTitle") : t("ws.policies.createTitle")}</DialogTitle>
            <DialogDescription>{t("ws.policies.dialogDescription")}</DialogDescription>
          </DialogHeader>
          {conflict ? (
            <ConflictAlert
              onReload={() => {
                save.reset();
                void detail.refetch();
                void invalidate();
              }}
            />
          ) : exists ? (
            <p role="alert" className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
              {t("ws.policies.exists")}
            </p>
          ) : save.error ? (
            <ErrorAlert error={save.error} />
          ) : null}
          {forbidden ? <p className="-mt-3 text-xs text-muted-foreground">{t("ws.policies.forbiddenHint")}</p> : null}
          {detail.error ? <ErrorAlert error={detail.error} onRetry={() => void detail.refetch()} /> : null}
          {editing ? (
            <dl className="grid grid-cols-[88px_1fr] gap-y-1.5 rounded-md border px-3 py-2.5 text-[13px]">
              <dt className="text-muted-foreground">{t("ws.policies.fields.layer")}</dt>
              <dd>{maybeT(`ws.policies.layers.${editing.layer}`) ?? editing.layer}</dd>
              <dt className="text-muted-foreground">{t("ws.policies.fields.scope")}</dt>
              <dd>
                {maybeT(`ws.policies.scopes.${editing.scope_type}`) ?? editing.scope_type}
                {editing.scope_type !== "organization" ? ` · ${scopeName}` : ""}
              </dd>
              <dt className="text-muted-foreground">{t("ws.policies.fields.version")}</dt>
              <dd className="tabular-nums">v{current?.version}</dd>
            </dl>
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <div className="grid gap-2">
                <Label htmlFor="policy-layer">{t("ws.policies.fields.layer")}</Label>
                <Controller
                  control={form.control}
                  name="layer"
                  render={({ field }) => (
                    <Select
                      value={field.value}
                      onValueChange={(value) => {
                        field.onChange(value);
                        const allowed = permissions.scopesFor(value as Layer);
                        if (!allowed.includes(form.getValues("scopeType"))) {
                          form.setValue("scopeType", allowed[0] ?? "project");
                          form.setValue("scopeId", fixedProject ?? "");
                        }
                        clearModels();
                      }}
                    >
                      <SelectTrigger id="policy-layer" className="w-full">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {permissions.layers.map((value) => (
                          <SelectItem key={value} value={value}>
                            {t(`ws.policies.layers.${value}`)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="policy-scope">{t("ws.policies.fields.scope")}</Label>
                <Controller
                  control={form.control}
                  name="scopeType"
                  render={({ field }) => (
                    <Select
                      value={field.value}
                      onValueChange={(value) => {
                        field.onChange(value);
                        form.setValue("scopeId", value === "project" && fixedProject ? fixedProject : "");
                        clearModels();
                      }}
                    >
                      <SelectTrigger id="policy-scope" className="w-full">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {scopes.map((value) => (
                          <SelectItem key={value} value={value}>
                            {t(`ws.policies.scopes.${value}`)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
              </div>
              {scopeType === "project" && !fixedProject ? (
                <Controller
                  control={form.control}
                  name="scopeId"
                  render={({ field, fieldState }) => (
                    <Select value={field.value} onValueChange={selectScope(field.onChange)}>
                      <SelectTrigger
                        aria-label={t("ws.policies.selectProject")}
                        className="col-span-2 w-full"
                        aria-invalid={fieldState.invalid || undefined}
                      >
                        <SelectValue placeholder={t("ws.policies.selectProject")} />
                      </SelectTrigger>
                      <SelectContent>
                        {(projects.data ?? []).map((project) => (
                          <SelectItem key={project.id} value={project.id}>
                            {project.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
              ) : null}
              {scopeType === "project" && fixedProject ? (
                <p className="col-span-2 text-sm text-muted-foreground">
                  {t("ws.policies.fixedProject", { name: names.project(fixedProject) })}
                </p>
              ) : null}
              {scopeType === "key" ? (
                <Controller
                  control={form.control}
                  name="scopeId"
                  render={({ field, fieldState }) => (
                    <Select value={field.value} onValueChange={selectScope(field.onChange)}>
                      <SelectTrigger
                        aria-label={t("ws.policies.selectKey")}
                        className="col-span-2 w-full"
                        aria-invalid={fieldState.invalid || undefined}
                      >
                        <SelectValue placeholder={keys.isPending ? t("common.loading") : t("ws.policies.selectKey")} />
                      </SelectTrigger>
                      <SelectContent>
                        {keyOptions.map((key) => (
                          <SelectItem key={key.id} value={key.id}>
                            {key.name} <span className="font-mono text-muted-foreground">{key.prefix}…</span>
                            {fixedProject ? null : (
                              <span className="text-muted-foreground"> · {names.project(key.project_id)}</span>
                            )}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
              ) : null}
              {errors.scopeId ? (
                <span className="col-span-2 text-xs text-destructive">{t("ws.policies.scopeRequired")}</span>
              ) : null}
            </div>
          )}
          {duplicate ? (
            <p role="alert" className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
              {t("ws.policies.exists")}
            </p>
          ) : null}
          {baselineMissing ? <MissingBaseline /> : null}
          <fieldset className="grid gap-2">
            <legend className="mb-1 text-sm font-medium">{t("ws.policies.fields.models")}</legend>
            <Controller
              control={form.control}
              name="models"
              render={({ field }) => (
                <ModelPicker candidates={candidates} value={field.value} onChange={field.onChange} bounded={bound !== null} />
              )}
            />
          </fieldset>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button
              type="submit"
              disabled={save.isPending || conflict || duplicate || (editing !== null && detail.isFetching)}
            >
              {save.isPending ? <Spinner /> : null}
              {editing ? t("common.save") : t("common.create")}
              {models.length === 0 ? ` · ${t("ws.policies.denyAll")}` : ""}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------------------------
// Project tab

/** Access policies that apply to one project: inherited organization rows plus its own. */
export function ProjectPolicies({ orgId, projectId }: { orgId: string; projectId: string }) {
  const { t } = useI18n();
  const workspace = useOrgWorkspace();
  const modelNames = useModelNames(workspace);
  const permissions = policyPermissions(workspace);
  const policies = useAccessPolicies(orgId, permissions.canView);
  const names = usePolicyNames(orgId, workspace, modelNames);
  const [target, setTarget] = useState<PolicyDialogTarget | null>(null);
  const all = policies.data?.items ?? [];
  const inherited = all.filter((p) => p.scope_type === "organization");
  const own = all.filter((p) => p.scope_type !== "organization" && p.project_id === projectId);
  const canCreate = permissions.scopesFor("customer").includes("project");

  if (!permissions.canView) {
    return <p className="text-sm text-muted-foreground">{t("ws.policies.noAccess")}</p>;
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <p className="min-w-[240px] flex-1 text-sm text-muted-foreground">{t("ws.policies.projectHint")}</p>
        {canCreate ? (
          <Button variant="outline" onClick={() => setTarget({ kind: "create", projectId })} disabled={!policies.data}>
            <PlusIcon />
            {t("ws.policies.createProject")}
          </Button>
        ) : null}
      </div>
      {policies.data && !inherited.some((p) => p.layer === "platform") ? <MissingBaseline /> : null}
      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold">{t("ws.policies.inheritedTitle")}</h3>
        <PolicyTable
          policies={inherited}
          names={names}
          permissions={permissions}
          onEdit={(policy) => setTarget({ kind: "edit", policy })}
          loading={policies.isPending}
          error={policies.error}
          onRetry={() => void policies.refetch()}
          empty={<EmptyState title={t("ws.policies.inheritedEmpty")} className="py-6" />}
        />
      </section>
      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold">{t("ws.policies.projectTitle")}</h3>
        <PolicyTable
          policies={own}
          names={names}
          permissions={permissions}
          onEdit={(policy) => setTarget({ kind: "edit", policy })}
          loading={policies.isPending}
          error={policies.error}
          onRetry={() => void policies.refetch()}
          empty={<EmptyState title={t("ws.policies.projectEmpty")} description={t("ws.policies.projectEmptyBody")} className="py-6" />}
        />
      </section>
      {policies.data?.truncated ? <p className="text-xs text-muted-foreground">{t("ws.policies.truncated")}</p> : null}
      <InheritanceNote />
      {target ? (
        <PolicyDialog
          key={target.kind === "edit" ? target.policy.id : "new"}
          orgId={orgId}
          target={target}
          policies={all}
          permissions={permissions}
          names={names}
          onClose={() => setTarget(null)}
        />
      ) : null}
    </div>
  );
}
