"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CalendarRangeIcon, CircleAlertIcon, PencilIcon, PlusIcon, TargetIcon } from "lucide-react";
import { useMemo, useState } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { DataTable } from "@/components/data-table";
import { ConflictAlert, EmptyState, ErrorAlert, LoadingRows, Spinner } from "@/components/feedback";
import { Money } from "@/components/money";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { hasAnyRole, useOrgWorkspace, type Workspace } from "@/components/shell/workspace";
import { StatusBadge, toneFor } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { checkDecimal, useKeyNames, useKeyOptions } from "@/components/workspace/lookups";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import { useCursorList } from "@/lib/api/hooks";
import { orgKey, useAccount, useProjectOptions } from "@/lib/api/queries";
import type { Budget, BudgetPeriod } from "@/lib/api/types";
import { formatDate, formatDateTime, shortId } from "@/lib/format";
import { addMoney, compareMoney, percentOf } from "@/lib/money";

type Layer = NonNullable<Budget["layer"]>;
type ScopeType = NonNullable<Budget["scope_type"]>;
type Period = Budget["period"];

const SCOPES: ScopeType[] = ["organization", "project", "key"];
const PERIODS: Period[] = ["day", "month"];

/** Owners and platform admins write the organization layer; only platform admins the platform layer. */
function writableLayers(workspace: Workspace): Layer[] {
  const layers: Layer[] = [];
  if (hasAnyRole(workspace, "org_owner", "platform_admin")) layers.push("organization");
  if (hasAnyRole(workspace, "platform_admin")) layers.push("platform");
  return layers;
}

const amountSchema = z.string().superRefine((value, ctx) => {
  const problem = checkDecimal(value);
  if (problem) ctx.addIssue({ code: "custom", message: problem });
});

const createSchema = z
  .object({
    layer: z.enum(["organization", "platform"]),
    scopeType: z.enum(["organization", "project", "key"]),
    scopeId: z.string(),
    period: z.enum(["day", "month"]),
    limit: amountSchema,
  })
  .superRefine((value, ctx) => {
    if (value.scopeType !== "organization" && !value.scopeId) {
      ctx.addIssue({ code: "custom", path: ["scopeId"], message: "required" });
    }
  });
type CreateValues = z.infer<typeof createSchema>;

function AmountMessage({ problem }: { problem?: string }) {
  const { t, maybeT } = useI18n();
  if (!problem) return <span className="text-xs text-muted-foreground">{t("ws.budgets.limitHint")}</span>;
  return <span className="text-xs text-destructive">{maybeT(`ws.budgets.amountProblems.${problem}`) ?? problem}</span>;
}

function CreateBudgetDialog({
  orgId,
  open,
  layers,
  currency,
  onClose,
}: {
  orgId: string;
  open: boolean;
  layers: Layer[];
  currency: string | undefined;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { layer: layers[0] ?? "organization", scopeType: "organization", scopeId: "", period: "month", limit: "" },
  });
  const scopeType = useWatch({ control: form.control, name: "scopeType" });
  const projects = useProjectOptions(orgId, open && scopeType === "project");
  const keys = useKeyOptions(orgId, open && scopeType === "key");

  const create = useMutation({
    mutationFn: async (values: CreateValues) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/budget-policies", {
            params: { path: { organization_id: orgId } },
            body: {
              layer: values.layer,
              scope_type: values.scopeType,
              scope_id: values.scopeType === "organization" ? null : values.scopeId,
              currency: currency!,
              limit_amount: values.limit.trim(),
              period: values.period,
            },
          }),
        )
      ).data,
    onSuccess: () => {
      toast.success(t("ws.budgets.created"));
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "budgets") });
      close();
    },
  });

  function close() {
    form.reset();
    create.reset();
    onClose();
  }

  const errors = form.formState.errors;

  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent className="sm:max-w-[460px]">
        <form noValidate className="grid gap-5" onSubmit={form.handleSubmit((values) => create.mutate(values))}>
          <DialogHeader>
            <DialogTitle>{t("ws.budgets.create")}</DialogTitle>
            <DialogDescription>{t("ws.budgets.createDescription")}</DialogDescription>
          </DialogHeader>
          {create.error ? <ErrorAlert error={create.error} /> : null}
          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-2">
              <Label htmlFor="budget-layer">{t("ws.budgets.fields.layer")}</Label>
              <Controller
                control={form.control}
                name="layer"
                render={({ field }) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger id="budget-layer" className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {layers.map((layer) => (
                        <SelectItem key={layer} value={layer}>
                          {t(`ws.budgets.layers.${layer}`)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
            </div>
            <div className="grid gap-2">
              <span id="budget-period" className="text-sm font-medium">
                {t("ws.budgets.fields.period")}
              </span>
              <Controller
                control={form.control}
                name="period"
                render={({ field }) => (
                  <ToggleGroup
                    type="single"
                    variant="outline"
                    aria-labelledby="budget-period"
                    value={field.value}
                    onValueChange={(value) => value && field.onChange(value)}
                    className="grid w-full grid-cols-2"
                  >
                    {PERIODS.map((period) => (
                      <ToggleGroupItem key={period} value={period}>
                        {t(`ws.budgets.periods.${period}`)}
                      </ToggleGroupItem>
                    ))}
                  </ToggleGroup>
                )}
              />
            </div>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="budget-scope">{t("ws.budgets.fields.scope")}</Label>
            <Controller
              control={form.control}
              name="scopeType"
              render={({ field }) => (
                <Select
                  value={field.value}
                  onValueChange={(value) => {
                    field.onChange(value);
                    form.setValue("scopeId", "");
                  }}
                >
                  <SelectTrigger id="budget-scope" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {SCOPES.map((scope) => (
                      <SelectItem key={scope} value={scope}>
                        {t(`ws.budgets.scopes.${scope}`)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            />
            {scopeType !== "organization" ? (
              <Controller
                control={form.control}
                name="scopeId"
                render={({ field, fieldState }) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger
                      aria-label={scopeType === "project" ? t("ws.budgets.selectProject") : t("ws.budgets.selectKey")}
                      className="w-full"
                      aria-invalid={fieldState.invalid || undefined}
                    >
                      <SelectValue
                        placeholder={scopeType === "project" ? t("ws.budgets.selectProject") : t("ws.budgets.selectKey")}
                      />
                    </SelectTrigger>
                    <SelectContent>
                      {scopeType === "project"
                        ? (projects.data ?? []).map((project) => (
                            <SelectItem key={project.id} value={project.id}>
                              {project.name}
                            </SelectItem>
                          ))
                        : (keys.data ?? [])
                            .filter((key) => !key.revoked_at)
                            .map((key) => (
                              <SelectItem key={key.id} value={key.id}>
                                {key.name} <span className="font-mono text-muted-foreground">{key.prefix}…</span>
                              </SelectItem>
                            ))}
                    </SelectContent>
                  </Select>
                )}
              />
            ) : null}
            {errors.scopeId ? <span className="text-xs text-destructive">{t("ws.budgets.scopeRequired")}</span> : null}
          </div>
          <div className="grid gap-2">
            <Label htmlFor="budget-limit">{t("ws.budgets.fields.limit", { currency: currency ?? "" })}</Label>
            <Input
              id="budget-limit"
              inputMode="decimal"
              autoComplete="off"
              className="tabular-nums"
              placeholder="1000.00"
              aria-invalid={Boolean(errors.limit) || undefined}
              {...form.register("limit")}
            />
            <AmountMessage problem={errors.limit?.message} />
          </div>
          <p className="text-xs text-muted-foreground">{t("ws.budgets.immutableHint")}</p>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={close}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={create.isPending || !currency || layers.length === 0}>
              {create.isPending ? <Spinner /> : null}
              {t("common.create")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

const editSchema = z.object({ limit: amountSchema, active: z.boolean() });
type EditValues = z.infer<typeof editSchema>;
type BudgetChanges = { limit_amount?: string; status?: "active" | "disabled" };

function EditBudgetDialog({ orgId, budget, onClose }: { orgId: string; budget: Budget; onClose: () => void }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  // The single GET gives the ETag that the PATCH sends back as If-Match.
  const detail = useQuery({
    queryKey: orgKey(orgId, "budgets", "detail", budget.id),
    queryFn: async () =>
      call(() =>
        nexus.GET("/api/v1/organizations/{organization_id}/budget-policies/{budget_policy_id}", {
          params: { path: { organization_id: orgId, budget_policy_id: budget.id } },
        }),
      ),
    staleTime: 0,
    // The form follows this data; only an explicit reload (after 412) replaces it.
    refetchOnWindowFocus: false,
  });
  const current = detail.data?.data ?? budget;
  const etag = detail.data?.etag ?? `"${current.version}"`;
  const form = useForm<EditValues>({
    resolver: zodResolver(editSchema),
    values: { limit: current.limit_amount, active: current.status === "active" },
  });

  const save = useMutation({
    mutationFn: async (body: BudgetChanges) =>
      call(() =>
        nexus.PATCH("/api/v1/organizations/{organization_id}/budget-policies/{budget_policy_id}", {
          params: { path: { organization_id: orgId, budget_policy_id: current.id }, header: { "If-Match": etag } },
          body,
        }),
      ),
    onSuccess: () => {
      toast.success(t("ws.budgets.saved"));
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "budgets") });
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "budget-periods", current.id) });
      onClose();
    },
  });

  // Only changed fields are sent; an edit that changes nothing just closes the dialog.
  const submit = (values: EditValues) => {
    const body: BudgetChanges = {};
    if (compareMoney(values.limit.trim(), current.limit_amount) !== 0) body.limit_amount = values.limit.trim();
    const status = values.active ? "active" : "disabled";
    if (status !== current.status) body.status = status;
    if (Object.keys(body).length === 0) onClose();
    else save.mutate(body);
  };

  const conflict = isConflict(save.error);
  const errors = form.formState.errors;

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-[440px]">
        <form noValidate className="grid gap-5" onSubmit={form.handleSubmit(submit)}>
          <DialogHeader>
            <DialogTitle>{t("ws.budgets.editTitle")}</DialogTitle>
            <DialogDescription>{t("ws.budgets.editDescription")}</DialogDescription>
          </DialogHeader>
          {conflict ? (
            <ConflictAlert
              onReload={() => {
                save.reset();
                void detail.refetch();
                void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "budgets") });
              }}
            />
          ) : save.error ? (
            <ErrorAlert error={save.error} />
          ) : null}
          {detail.error ? <ErrorAlert error={detail.error} onRetry={() => void detail.refetch()} /> : null}
          <dl className="grid grid-cols-[88px_1fr] gap-y-1.5 rounded-md border px-3 py-2.5 text-[13px]">
            <dt className="text-muted-foreground">{t("ws.budgets.fields.layer")}</dt>
            <dd>{t(`ws.budgets.layers.${current.layer ?? "organization"}`)}</dd>
            <dt className="text-muted-foreground">{t("ws.budgets.fields.scope")}</dt>
            <dd>{t(`ws.budgets.scopes.${current.scope_type ?? "organization"}`)}</dd>
            <dt className="text-muted-foreground">{t("ws.budgets.fields.period")}</dt>
            <dd>{t(`ws.budgets.periods.${current.period}`)}</dd>
            <dt className="text-muted-foreground">{t("ws.budgets.fields.version")}</dt>
            <dd className="tabular-nums">v{current.version}</dd>
          </dl>
          <div className="grid gap-2">
            <Label htmlFor="budget-edit-limit">{t("ws.budgets.fields.limit", { currency: current.currency })}</Label>
            <Input
              id="budget-edit-limit"
              inputMode="decimal"
              autoComplete="off"
              className="tabular-nums"
              aria-invalid={Boolean(errors.limit) || undefined}
              {...form.register("limit")}
            />
            <AmountMessage problem={errors.limit?.message} />
          </div>
          <Controller
            control={form.control}
            name="active"
            render={({ field }) => (
              <Label className="flex items-start gap-3 rounded-md border px-3 py-2.5">
                <Switch checked={field.value} onCheckedChange={field.onChange} className="mt-0.5" />
                <span className="flex flex-col gap-0.5">
                  <span>{t("ws.budgets.activeLabel")}</span>
                  <span className="text-xs font-normal text-muted-foreground">{t("ws.budgets.activeHint")}</span>
                </span>
              </Label>
            )}
          />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button
              type="submit"
              disabled={save.isPending || conflict || detail.isFetching || !form.formState.isDirty}
            >
              {save.isPending ? <Spinner /> : null}
              {t("common.save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function PeriodRow({ period, currency }: { period: BudgetPeriod; currency: string }) {
  const { t } = useI18n();
  const used = addMoney(period.spent, period.reserved);
  const spentPct = percentOf(period.spent, period.limit_amount);
  const reservedPct = Math.min(100 - spentPct, percentOf(period.reserved, period.limit_amount));
  return (
    <li className="flex flex-col gap-2 border-t px-4 py-3 first:border-t-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex-1 font-medium tabular-nums">
          {formatDate(period.period_start)} → {formatDate(period.period_end)} UTC
        </span>
        <span className="text-xs text-muted-foreground tabular-nums">
          {t("ws.budgets.policyVersion", { version: period.policy_version })}
        </span>
        {period.over_limit ? <StatusBadge tone="danger">{t("ws.budgets.overLimit")}</StatusBadge> : null}
      </div>
      <div
        className="flex h-2 gap-0.5 overflow-hidden rounded bg-muted"
        role="img"
        aria-label={t("ws.budgets.usedOfLimit", { used, limit: period.limit_amount })}
      >
        <span className={period.over_limit ? "bg-red-700" : "bg-primary"} style={{ width: `${spentPct}%` }} />
        <span
          className="bg-[repeating-linear-gradient(45deg,var(--color-zinc-500)_0_2px,var(--color-zinc-300)_2px_4px)]"
          style={{ width: `${reservedPct}%` }}
        />
      </div>
      <dl className="grid grid-cols-3 gap-2 text-xs">
        <div className="flex flex-col">
          <dt className="text-muted-foreground">{t("ws.budgets.spent")}</dt>
          <dd>
            <Money value={period.spent} />
          </dd>
        </div>
        <div className="flex flex-col">
          <dt className="text-muted-foreground">{t("ws.budgets.reserved")}</dt>
          <dd>
            <Money value={period.reserved} />
          </dd>
        </div>
        <div className="flex flex-col">
          <dt className="text-muted-foreground">{t("ws.budgets.limitOf", { currency })}</dt>
          <dd>
            <Money value={period.limit_amount} className="font-medium" />
          </dd>
        </div>
      </dl>
    </li>
  );
}

function PeriodsSheet({
  orgId,
  budget,
  scopeLabel,
  onClose,
}: {
  orgId: string;
  budget: Budget | null;
  scopeLabel: string;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const periods = useCursorList<BudgetPeriod>({
    queryKey: orgKey(orgId, "budget-periods", budget?.id, "list"),
    enabled: budget !== null,
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/budget-policies/{budget_policy_id}/periods", {
            params: { path: { organization_id: orgId, budget_policy_id: budget!.id }, query: { limit: 50, cursor } },
          }),
        )
      ).data,
  });
  return (
    <Sheet open={budget !== null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full gap-0 sm:max-w-[520px]">
        {budget ? (
          <>
            <SheetHeader className="border-b">
              <SheetTitle>{t("ws.budgets.periodsTitle")}</SheetTitle>
              <SheetDescription>
                {scopeLabel} · {t(`ws.budgets.periods.${budget.period}`)} · <Money value={budget.limit_amount} currency={budget.currency} />
              </SheetDescription>
            </SheetHeader>
            <div className="flex flex-col gap-3 overflow-y-auto p-4">
              <div className="flex gap-3.5 text-xs text-muted-foreground">
                <span className="inline-flex items-center gap-1.5">
                  <span aria-hidden className="size-2.5 rounded-sm bg-primary" />
                  {t("ws.budgets.spent")}
                </span>
                <span className="inline-flex items-center gap-1.5">
                  <span
                    aria-hidden
                    className="size-2.5 rounded-sm bg-[repeating-linear-gradient(45deg,var(--color-zinc-500)_0_2px,var(--color-zinc-300)_2px_4px)]"
                  />
                  {t("ws.budgets.reserved")}
                </span>
              </div>
              <p className="text-xs text-muted-foreground">{t("ws.budgets.periodsHint")}</p>
              {periods.error ? (
                <ErrorAlert error={periods.error} onRetry={() => void periods.refetch()} />
              ) : periods.isPending ? (
                <LoadingRows rows={3} columns={2} />
              ) : periods.items.length === 0 ? (
                <EmptyState title={t("ws.budgets.periodsEmpty")} description={t("ws.budgets.periodsEmptyBody")} className="py-8" />
              ) : (
                <ul className="rounded-xl border">
                  {periods.items.map((period) => (
                    <PeriodRow key={period.id} period={period} currency={budget.currency} />
                  ))}
                </ul>
              )}
              {periods.hasNextPage ? (
                <Button variant="outline" size="sm" disabled={periods.isFetchingNextPage} onClick={() => void periods.fetchNextPage()}>
                  {periods.isFetchingNextPage ? t("common.loading") : t("common.loadMore")}
                </Button>
              ) : null}
            </div>
          </>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

export default function BudgetsPage() {
  const { t, maybeT } = useI18n();
  const workspace = useOrgWorkspace();
  const { orgId } = workspace;
  const layers = writableLayers(workspace);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Budget | null>(null);
  const [viewing, setViewing] = useState<Budget | null>(null);
  const account = useAccount(orgId);
  const projects = useProjectOptions(orgId);
  const keyNames = useKeyNames(workspace);

  const budgets = useCursorList<Budget>({
    queryKey: orgKey(orgId, "budgets", "list"),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/budget-policies", {
            params: { path: { organization_id: orgId }, query: { limit: 50, cursor } },
          }),
        )
      ).data,
  });

  const scopeLabel = useMemo(() => {
    const projectName = new Map((projects.data ?? []).map((p) => [p.id, p.name]));
    return (budget: Budget) => {
      const scope = budget.scope_type ?? "organization";
      if (scope === "organization") return t("ws.budgets.scopes.organization");
      const id = budget.scope_id ?? "";
      if (scope === "project") return `${t("ws.budgets.scopes.project")} · ${projectName.get(id) ?? shortId(id)}`;
      const key = keyNames.get(id);
      return `${t("ws.budgets.scopes.key")} · ${key ? `${key.name} (${key.prefix}…)` : shortId(id)}`;
    };
  }, [t, projects.data, keyNames]);

  const columns = useMemo<ColumnDef<Budget, unknown>[]>(
    () => [
      {
        id: "layer",
        header: t("ws.budgets.columns.layer"),
        cell: ({ row }) => t(`ws.budgets.layers.${row.original.layer ?? "organization"}`),
      },
      {
        id: "scope",
        header: t("ws.budgets.columns.scope"),
        cell: ({ row }) => <span title={row.original.scope_id ?? undefined}>{scopeLabel(row.original)}</span>,
      },
      {
        id: "period",
        header: t("ws.budgets.columns.period"),
        cell: ({ row }) => t(`ws.budgets.periods.${row.original.period}`),
      },
      {
        id: "limit",
        header: t("ws.budgets.columns.limit"),
        meta: { align: "right" },
        cell: ({ row }) => <Money value={row.original.limit_amount} currency={row.original.currency} className="font-medium" />,
      },
      {
        id: "status",
        header: t("ws.budgets.columns.status"),
        cell: ({ row }) => (
          <StatusBadge tone={toneFor(row.original.status)}>
            {maybeT(`status.${row.original.status}`) ?? row.original.status}
          </StatusBadge>
        ),
      },
      {
        id: "version",
        header: t("ws.budgets.columns.version"),
        cell: ({ row }) => (
          <span className="flex flex-col text-muted-foreground tabular-nums">
            <span>v{row.original.version}</span>
            <span className="text-xs">{formatDateTime(row.original.created_at, { seconds: false })}</span>
          </span>
        ),
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("ws.budgets.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <span className="flex justify-end gap-1.5">
            <Button variant="ghost" size="sm" onClick={() => setViewing(row.original)}>
              <CalendarRangeIcon />
              {t("ws.budgets.viewPeriods")}
            </Button>
            {layers.includes(row.original.layer ?? "organization") ? (
              <Button variant="outline" size="sm" onClick={() => setEditing(row.original)}>
                <PencilIcon />
                {t("ws.budgets.edit")}
              </Button>
            ) : null}
          </span>
        ),
      },
    ],
    [t, maybeT, scopeLabel, layers],
  );

  const restricted = account.data && account.data.status !== "active";

  return (
    <PageContainer>
      <PageHeader
        title={t("ws.budgets.title")}
        description={t("ws.budgets.subtitle")}
        actions={
          layers.length > 0 ? (
            <Button onClick={() => setCreating(true)} disabled={!account.data}>
              <PlusIcon />
              {t("ws.budgets.create")}
            </Button>
          ) : null
        }
      />
      {layers.length === 0 ? (
        <p className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">{t("ws.budgets.readOnly")}</p>
      ) : null}
      {restricted ? (
        <p className="flex items-center gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          <CircleAlertIcon className="size-4 text-amber-700" aria-hidden />
          {t("ws.budgets.accountRestricted")}
        </p>
      ) : null}
      {account.error && layers.length > 0 ? <ErrorAlert error={account.error} onRetry={() => void account.refetch()} /> : null}
      <DataTable
        columns={columns}
        data={budgets.items}
        loading={budgets.isPending}
        error={budgets.error}
        onRetry={() => void budgets.refetch()}
        getRowId={(row) => row.id}
        minWidth={920}
        rowClassName={(row) => (row.status !== "active" ? "text-muted-foreground" : undefined)}
        empty={
          <EmptyState
            icon={<TargetIcon className="size-5" />}
            title={t("ws.budgets.emptyTitle")}
            description={t("ws.budgets.emptyBody")}
          />
        }
        footer={
          budgets.items.length > 0 ? (
            <LoadMoreFooter
              count={budgets.items.length}
              hasNext={Boolean(budgets.hasNextPage)}
              loading={budgets.isFetchingNextPage}
              onLoadMore={() => void budgets.fetchNextPage()}
              note={t("ws.budgets.footerNote")}
            />
          ) : null
        }
      />
      <CreateBudgetDialog
        orgId={orgId}
        open={creating}
        layers={layers}
        currency={account.data?.currency}
        onClose={() => setCreating(false)}
      />
      {editing ? (
        <EditBudgetDialog key={editing.id} orgId={orgId} budget={editing} onClose={() => setEditing(null)} />
      ) : null}
      <PeriodsSheet
        orgId={orgId}
        budget={viewing}
        scopeLabel={viewing ? scopeLabel(viewing) : ""}
        onClose={() => setViewing(null)}
      />
    </PageContainer>
  );
}
