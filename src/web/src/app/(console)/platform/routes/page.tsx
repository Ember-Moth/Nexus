"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { PencilIcon, PlusIcon, SplitIcon, Trash2Icon } from "lucide-react";
import { useMemo, useState } from "react";
import { Controller, useFieldArray, useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { DataTable } from "@/components/data-table";
import { ConflictAlert, EmptyState, ErrorAlert, Spinner } from "@/components/feedback";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isApiError, isConflict } from "@/lib/api/errors";
import { useCursorList } from "@/lib/api/hooks";
import type { Deployment, Model, Organization, RoutePolicy } from "@/lib/api/types";
import { shortId } from "@/lib/format";

const integer = (min: number, max: number) => z.coerce.number().int().min(min).max(max);

const schema = z
  .object({
    organizationId: z.string().min(1),
    modelId: z.string().min(1),
    candidates: z
      .array(z.object({ deploymentId: z.string().min(1), priority: integer(0, 10000), weight: integer(1, 10000) }))
      .min(1)
      .max(256),
  })
  .superRefine((value, ctx) => {
    const ids = value.candidates.map((c) => c.deploymentId);
    if (new Set(ids).size !== ids.length) ctx.addIssue({ code: "custom", path: ["candidates"], message: "duplicate" });
  });
type Values = z.input<typeof schema>;
type Parsed = z.output<typeof schema>;

/** Read catalog options by cursor. Candidate choices are separately scoped to the selected model. */
function useDeployments(modelId?: string) {
  return useCursorList<Deployment>({
    queryKey: ["catalog", "deployments", "options", modelId ?? null],
    enabled: modelId !== "",
    fetchPage: async (cursor) =>
      (await call(() =>
        nexus.GET("/api/v1/deployments", { params: { query: { limit: 100, cursor, model_id: modelId } } }),
      )).data,
  });
}

function useCatalogNames() {
  const organizations = useCursorList<Organization>({
    queryKey: ["organizations", "route-options"],
    fetchPage: async (cursor) =>
      (await call(() => nexus.GET("/api/v1/organizations", { params: { query: { limit: 100, cursor } } }))).data,
  });
  const models = useCursorList<Model>({
    queryKey: ["catalog", "models", "route-options"],
    fetchPage: async (cursor) =>
      (await call(() => nexus.GET("/api/v1/models", { params: { query: { limit: 100, cursor } } }))).data,
  });
  const deployments = useDeployments();
  return { organizations, models, deployments };
}

type OptionsPage = {
  error: Error | null;
  refetch: () => Promise<unknown>;
  hasNextPage: boolean;
  fetchNextPage: () => Promise<unknown>;
  isFetchingNextPage: boolean;
};

function MoreOptions({ query }: { query: OptionsPage }) {
  const t = useT();
  if (query.error) return <ErrorAlert error={query.error} onRetry={() => void query.refetch()} />;
  if (!query.hasNextPage) return null;
  return (
    <Button type="button" variant="outline" size="sm" disabled={query.isFetchingNextPage}
      onClick={() => void query.fetchNextPage()}>
      {t(query.isFetchingNextPage ? "common.loading" : "common.loadMore")}
    </Button>
  );
}

function RouteDialog({
  policy,
  onClose,
  catalog,
}: {
  policy: RoutePolicy | "new";
  onClose: () => void;
  catalog: ReturnType<typeof useCatalogNames>;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const editing = policy === "new" ? null : policy;
  const form = useForm<Values, unknown, Parsed>({
    resolver: zodResolver(schema),
    defaultValues: {
      organizationId: editing?.organization_id ?? "",
      modelId: editing?.model_id ?? "",
      candidates: editing
        ? editing.candidates.map((c) => ({ deploymentId: c.deployment_id, priority: c.priority ?? 0, weight: c.weight ?? 1 }))
        : [{ deploymentId: "", priority: 0, weight: 1 }],
    },
  });
  const candidates = useFieldArray({ control: form.control, name: "candidates" });
  const modelId = useWatch({ control: form.control, name: "modelId" });
  const deploymentOptions = useDeployments(modelId);
  const deployments = deploymentOptions.items;

  // PUT /route-policies/{organization_id}/{model_id}: create without If-Match (428 if it
  // already exists), replace with If-Match "<version>" (412 when the version is stale).
  const save = useMutation({
    mutationFn: async (values: Parsed) =>
      call(() =>
        nexus.PUT("/api/v1/route-policies/{organization_id}/{model_id}", {
          params: {
            path: { organization_id: values.organizationId, model_id: values.modelId },
            header: editing ? { "If-Match": `"${editing.version}"` } : undefined,
          },
          body: {
            candidates: values.candidates.map((c) => ({
              deployment_id: c.deploymentId,
              priority: c.priority,
              weight: c.weight,
            })),
          },
        }),
      ),
    onSuccess: () => {
      toast.success(t("routes.saved"));
      void queryClient.invalidateQueries({ queryKey: ["route-policies"] });
      onClose();
    },
  });

  const errors = form.formState.errors;
  const invalid = Boolean(errors.organizationId || errors.modelId || errors.candidates?.root || errors.candidates?.message);
  const numberInvalid = Array.isArray(errors.candidates) && errors.candidates.some((c) => c?.priority || c?.weight);
  const duplicate = errors.candidates?.message === "duplicate";

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-[600px]">
        <form noValidate className="grid gap-5" onSubmit={form.handleSubmit((values) => save.mutate(values))}>
          <DialogHeader>
            <DialogTitle>{editing ? t("routes.editTitle") : t("routes.create")}</DialogTitle>
            <DialogDescription>{t("routes.subtitle")}</DialogDescription>
          </DialogHeader>
          {isConflict(save.error) ? (
            <ConflictAlert
              onReload={() => {
                save.reset();
                void queryClient.invalidateQueries({ queryKey: ["route-policies"] });
                onClose();
              }}
            >
              <span>{t("policyWrite.stale")}</span>
            </ConflictAlert>
          ) : isApiError(save.error) && save.error.status === 428 ? (
            <p role="alert" className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
              {t("policyWrite.exists")}
            </p>
          ) : save.error ? (
            <ErrorAlert error={save.error} />
          ) : null}
          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-2">
              <Label htmlFor="route-org">{t("routes.organization")}</Label>
              <Controller
                control={form.control}
                name="organizationId"
                render={({ field, fieldState }) => (
                  <Select value={field.value} onValueChange={field.onChange} disabled={Boolean(editing)}>
                    <SelectTrigger id="route-org" className="w-full" aria-invalid={fieldState.invalid || undefined}>
                      <SelectValue placeholder={t("routes.selectOrganization")}>
                        {editing ? (catalog.organizations.items.find((o) => o.id === editing.organization_id)?.name ?? shortId(editing.organization_id)) : undefined}
                      </SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      {catalog.organizations.items.map((org) => (
                        <SelectItem key={org.id} value={org.id}>
                          {org.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
              {!editing && <MoreOptions query={catalog.organizations} />}
            </div>
            <div className="grid gap-2">
              <Label htmlFor="route-model">{t("routes.model")}</Label>
              <Controller
                control={form.control}
                name="modelId"
                render={({ field, fieldState }) => (
                  <Select value={field.value} onValueChange={(value) => {
                    if (value !== field.value) candidates.replace([{ deploymentId: "", priority: 0, weight: 1 }]);
                    field.onChange(value);
                  }} disabled={Boolean(editing)}>
                    <SelectTrigger id="route-model" className="w-full" aria-invalid={fieldState.invalid || undefined}>
                      <SelectValue placeholder={t("routes.selectModel")}>
                        {editing ? (catalog.models.items.find((m) => m.id === editing.model_id)?.public_name ?? shortId(editing.model_id)) : undefined}
                      </SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      {catalog.models.items.map((model) => (
                        <SelectItem key={model.id} value={model.id}>
                          <span className="font-mono">{model.public_name}</span>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
              {!editing && <MoreOptions query={catalog.models} />}
            </div>
          </div>
          <fieldset className="grid gap-2">
            <legend className="mb-1 text-sm font-medium">{t("routes.candidates")}</legend>
            <p className="text-xs text-muted-foreground">{t("routes.candidatesHint")}</p>
            <div className="grid grid-cols-[1fr_92px_92px_36px] gap-2 text-xs text-muted-foreground">
              <span>{t("routes.deployment")}</span>
              <span>{t("routes.priority")}</span>
              <span>{t("routes.weight")}</span>
              <span />
            </div>
            {candidates.fields.map((field, index) => (
              <div key={field.id} className="grid grid-cols-[1fr_92px_92px_36px] items-center gap-2">
                <Controller
                  control={form.control}
                  name={`candidates.${index}.deploymentId`}
                  render={({ field: f, fieldState }) => (
                    <Select value={f.value} onValueChange={f.onChange}>
                      <SelectTrigger
                        aria-label={`${t("routes.deployment")} ${index + 1}`}
                        className="w-full"
                        aria-invalid={fieldState.invalid || undefined}
                      >
                        <SelectValue placeholder={deploymentOptions.isFetching ? t("common.loading") : deployments.length ? t("routes.selectDeployment") : t("routes.noDeployments")}>
                          {f.value ? (deployments.find((d) => d.id === f.value)?.name ?? shortId(f.value)) : undefined}
                        </SelectValue>
                      </SelectTrigger>
                      <SelectContent>
                        {deployments.map((deployment) => (
                          <SelectItem key={deployment.id} value={deployment.id}>
                            {deployment.name}
                            <span className="text-muted-foreground">· {deployment.region}</span>
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                <Input
                  inputMode="numeric"
                  aria-label={`${t("routes.priority")} ${index + 1}`}
                  className="tabular-nums"
                  {...form.register(`candidates.${index}.priority`)}
                />
                <Input
                  inputMode="numeric"
                  aria-label={`${t("routes.weight")} ${index + 1}`}
                  className="tabular-nums"
                  {...form.register(`candidates.${index}.weight`)}
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label={t("routes.removeCandidate")}
                  disabled={candidates.fields.length === 1}
                  onClick={() => candidates.remove(index)}
                >
                  <Trash2Icon />
                </Button>
              </div>
            ))}
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="w-fit"
              onClick={() => candidates.append({ deploymentId: "", priority: 0, weight: 1 })}
            >
              <PlusIcon />
              {t("routes.addCandidate")}
            </Button>
            {duplicate ? <span className="text-xs text-destructive">{t("routes.duplicate")}</span> : null}
            {numberInvalid ? <span className="text-xs text-destructive">{t("routes.invalidNumber")}</span> : null}
            {invalid && !duplicate ? <span className="text-xs text-destructive">{t("routes.required")}</span> : null}
            {modelId && <MoreOptions query={deploymentOptions} />}
          </fieldset>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={save.isPending || isConflict(save.error)}>
              {save.isPending ? <Spinner /> : null}
              {t("common.save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export default function RoutesPage() {
  const t = useT();
  const catalog = useCatalogNames();
  const [editing, setEditing] = useState<RoutePolicy | "new" | null>(null);

  const routes = useCursorList<RoutePolicy>({
    queryKey: ["route-policies"],
    fetchPage: async (cursor) =>
      (await call(() => nexus.GET("/api/v1/route-policies", { params: { query: { limit: 50, cursor } } }))).data,
  });

  const names = useMemo(
    () => ({
      organization: new Map(catalog.organizations.items.map((o) => [o.id, o.name])),
      model: new Map(catalog.models.items.map((m) => [m.id, m.public_name])),
      deployment: new Map(catalog.deployments.items.map((d) => [d.id, d.name])),
    }),
    [catalog.organizations.items, catalog.models.items, catalog.deployments.items],
  );

  const columns = useMemo<ColumnDef<RoutePolicy, unknown>[]>(
    () => [
      {
        header: t("routes.columns.organization"),
        accessorKey: "organization_id",
        cell: ({ row }) => names.organization.get(row.original.organization_id) ?? shortId(row.original.organization_id),
      },
      {
        header: t("routes.columns.model"),
        accessorKey: "model_id",
        cell: ({ row }) => (
          <span className="font-mono text-[12.5px]">{names.model.get(row.original.model_id) ?? shortId(row.original.model_id)}</span>
        ),
      },
      {
        id: "candidates",
        header: t("routes.columns.candidates"),
        cell: ({ row }) => (
          <ul className="flex flex-col gap-0.5">
            {row.original.candidates.map((c) => (
              <li key={c.deployment_id}>
                {t("routes.candidateSummary", {
                  name: names.deployment.get(c.deployment_id) ?? shortId(c.deployment_id),
                  priority: c.priority ?? 0,
                  weight: c.weight ?? 1,
                })}
              </li>
            ))}
          </ul>
        ),
      },
      {
        header: t("routes.columns.version"),
        accessorKey: "version",
        cell: ({ row }) => <span className="text-muted-foreground tabular-nums">v{row.original.version}</span>,
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("routes.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <Button variant="outline" size="sm" onClick={() => setEditing(row.original)}>
            <PencilIcon />
            {t("routes.edit")}
          </Button>
        ),
      },
    ],
    [t, names],
  );

  return (
    <PageContainer>
      <PageHeader
        title={t("routes.title")}
        description={t("routes.subtitle")}
        actions={
          <Button onClick={() => setEditing("new")}>
            <PlusIcon />
            {t("routes.create")}
          </Button>
        }
      />
      <DataTable
        columns={columns}
        data={routes.items}
        loading={routes.isPending}
        error={routes.error}
        onRetry={() => void routes.refetch()}
        getRowId={(row) => `${row.organization_id}:${row.model_id}`}
        minWidth={820}
        empty={<EmptyState icon={<SplitIcon className="size-5" />} title={t("routes.emptyTitle")} />}
        footer={
          routes.items.length > 0 ? (
            <LoadMoreFooter
              count={routes.items.length}
              hasNext={Boolean(routes.hasNextPage)}
              loading={routes.isFetchingNextPage}
              onLoadMore={() => void routes.fetchNextPage()}
            />
          ) : null
        }
      />
      {catalog.organizations.error || catalog.models.error || catalog.deployments.error ? (
        <ErrorAlert error={catalog.organizations.error ?? catalog.models.error ?? catalog.deployments.error} />
      ) : null}
      {editing !== null ? (
        <RouteDialog
          key={editing === "new" ? "new" : `${editing.organization_id}:${editing.model_id}:${editing.version}`}
          policy={editing}
          catalog={catalog}
          onClose={() => setEditing(null)}
        />
      ) : null}
    </PageContainer>
  );
}
