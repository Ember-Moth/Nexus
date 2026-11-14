"use client";

import { useQuery } from "@tanstack/react-query";
import { CircleAlertIcon, InfoIcon, KeyRoundIcon, PlusIcon, RefreshCwIcon } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { EmptyState, ErrorAlert } from "@/components/feedback";
import { Money } from "@/components/money";
import { PageContainer, PageHeader } from "@/components/page";
import { hasAnyRole, useOrgWorkspace } from "@/components/shell/workspace";
import { StatusBadge, toneFor } from "@/components/status-badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { orgKey, useAccount } from "@/lib/api/queries";
import type { Budget, BudgetPeriod } from "@/lib/api/types";
import { formatDate, formatDateTime, formatTime, startOfUtcDay, startOfUtcMonth } from "@/lib/format";
import { addMoney, compareMoney, percentOf } from "@/lib/money";

function useUsage(orgId: string, groupBy: "day" | "organization", start: string) {
  return useQuery({
    queryKey: orgKey(orgId, "usage", groupBy, start),
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/usage", {
            params: { path: { organization_id: orgId }, query: { group_by: groupBy, start } },
          }),
        )
      ).data,
  });
}

function KpiCard({ label, value, hint, currency, loading, badge }: {
  label: string;
  value?: string;
  hint: string;
  currency?: string;
  loading: boolean;
  badge?: React.ReactNode;
}) {
  return (
    <Card size="sm" className="gap-2 px-5">
      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span>{label}</span>
        {badge ?? <span className="text-xs font-medium">{currency}</span>}
      </div>
      {loading ? (
        <Skeleton className="h-8 w-40" />
      ) : (
        <Money value={value} className="text-[28px] leading-9 font-semibold tracking-tight" extraClassName="text-lg" />
      )}
      <p className="text-xs text-muted-foreground">{hint}</p>
    </Card>
  );
}

function UsageChart({ orgId, currency }: { orgId: string; currency: string }) {
  const { t } = useI18n();
  const start = useMemo(() => startOfUtcDay(13), []);
  const usage = useUsage(orgId, "day", start);
  const days = useMemo(() => {
    const byDay = new Map((usage.data?.items ?? []).map((row) => [row.group.slice(0, 10), row]));
    return Array.from({ length: 14 }, (_, index) => {
      const iso = startOfUtcDay(13 - index);
      const key = iso.slice(0, 10);
      const row = byDay.get(key);
      return { key, label: key.slice(8, 10), row, net: row?.net_charge ?? "0" };
    });
  }, [usage.data]);
  const max = days.reduce((top, day) => (compareMoney(day.net, top) > 0 ? day.net : top), "0");
  const watermark = usage.data?.watermark;

  return (
    <Card className="min-w-0 flex-[2_1_560px]">
      <CardHeader>
        <CardTitle>{t("overview.usageTitle")}</CardTitle>
        <CardDescription>{t("overview.usageSubtitle", { currency })}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {usage.error ? (
          <ErrorAlert error={usage.error} onRetry={() => void usage.refetch()} />
        ) : usage.isPending ? (
          <Skeleton className="h-[180px] w-full" />
        ) : compareMoney(max, "0") === 0 ? (
          <EmptyState title={t("overview.usageEmpty")} className="py-10" />
        ) : (
          <div>
            <div className="flex h-[180px] items-end gap-1.5 border-b border-border">
              {days.map((day, index) => {
                const today = index === days.length - 1;
                return (
                  <Tooltip key={day.key}>
                    <TooltipTrigger asChild>
                      <button
                        type="button"
                        className="flex h-full flex-1 items-end justify-center rounded-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
                        aria-label={`${day.key} ${day.net}`}
                      >
                        <span
                          className={
                            today
                              ? "block w-full max-w-[30px] rounded-t bg-[repeating-linear-gradient(45deg,var(--color-zinc-400)_0_2px,var(--color-zinc-200)_2px_5px)]"
                              : "block w-full max-w-[30px] rounded-t bg-primary hover:bg-zinc-600"
                          }
                          style={{ height: `${Math.max(percentOf(day.net, max), compareMoney(day.net, "0") > 0 ? 1 : 0)}%` }}
                        />
                      </button>
                    </TooltipTrigger>
                    <TooltipContent className="flex flex-col gap-0.5">
                      <span className="font-semibold">{day.key} UTC</span>
                      <Money value={day.net} currency={currency} />
                      {day.row ? <span>{day.row.requests} requests</span> : null}
                    </TooltipContent>
                  </Tooltip>
                );
              })}
            </div>
            <div aria-hidden className="mt-1.5 flex gap-1.5 text-[11px] text-muted-foreground">
              {days.map((day) => (
                <span key={day.key} className="flex-1 text-center">
                  {day.label}
                </span>
              ))}
            </div>
          </div>
        )}
        {watermark ? (
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
            <span>
              {t("overview.watermark", {
                source: formatTime(watermark.latest_source_at),
                projected: formatTime(watermark.projected_at),
                pending: watermark.pending_requests,
              })}
            </span>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function BudgetRow({ orgId, budget }: { orgId: string; budget: Budget }) {
  const { t } = useI18n();
  const periods = useQuery({
    queryKey: orgKey(orgId, "budget-periods", budget.id),
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/budget-policies/{budget_policy_id}/periods", {
            params: { path: { organization_id: orgId, budget_policy_id: budget.id }, query: { limit: 100 } },
          }),
        )
      ).data.items,
  });
  const [now] = useState(() => Date.now());
  const period: BudgetPeriod | undefined =
    periods.data?.find((p) => Date.parse(p.period_start) <= now && now < Date.parse(p.period_end)) ?? undefined;
  const spent = period?.spent ?? "0";
  const reserved = period?.reserved ?? "0";
  const used = addMoney(spent, reserved);
  const spentPct = percentOf(spent, budget.limit_amount);
  const reservedPct = Math.min(100 - spentPct, percentOf(reserved, budget.limit_amount));
  const scope = t(`overview.scopes.${budget.scope_type}` as "overview.scopes.organization");

  return (
    <div className="flex flex-col gap-2 border-t px-6 py-3.5">
      <div className="flex items-center gap-2">
        <span className="flex-1 font-medium">{scope}</span>
        <span className="inline-flex h-5 items-center rounded-md border px-1.5 text-xs text-muted-foreground">
          {t("overview.budgetLayer", {
            layer: t(`overview.layers.${budget.layer}` as "overview.layers.platform"),
            period: t(`overview.periods.${budget.period}` as "overview.periods.day"),
          })}
        </span>
      </div>
      <div className="flex h-2 gap-0.5 overflow-hidden rounded bg-muted" role="img" aria-label={`${used} / ${budget.limit_amount}`}>
        <span className={period?.over_limit ? "bg-red-700" : "bg-primary"} style={{ width: `${spentPct}%` }} />
        <span
          className="bg-[repeating-linear-gradient(45deg,var(--color-zinc-500)_0_2px,var(--color-zinc-300)_2px_4px)]"
          style={{ width: `${reservedPct}%` }}
        />
      </div>
      <div className="flex justify-between gap-2 text-xs text-muted-foreground tabular-nums">
        <span>
          <Money value={used} /> / <Money value={budget.limit_amount} />
        </span>
        <span>{period ? `${formatDate(period.period_start)} → ${formatDate(period.period_end)} UTC` : "—"}</span>
      </div>
      {period?.over_limit ? (
        <span className="flex items-center gap-1.5 text-xs font-medium text-red-700">
          <CircleAlertIcon className="size-3.5" aria-hidden />
          {t("overview.overLimit")}
        </span>
      ) : null}
    </div>
  );
}

function Budgets({ orgId }: { orgId: string }) {
  const { t } = useI18n();
  const budgets = useQuery({
    queryKey: orgKey(orgId, "budgets"),
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/budget-policies", {
            params: { path: { organization_id: orgId }, query: { limit: 20 } },
          }),
        )
      ).data.items.filter((budget) => budget.status === "active"),
  });
  return (
    <Card className="min-w-0 flex-[1_1_320px] gap-3 pb-2">
      <CardHeader>
        <CardTitle>{t("overview.budgetsTitle")}</CardTitle>
        <CardDescription>{t("overview.budgetsSubtitle")}</CardDescription>
      </CardHeader>
      <div className="flex gap-3.5 px-6 text-xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden className="size-2.5 rounded-sm bg-primary" />
          {t("overview.spent")}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden className="size-2.5 rounded-sm bg-[repeating-linear-gradient(45deg,var(--color-zinc-500)_0_2px,var(--color-zinc-300)_2px_4px)]" />
          {t("overview.reserved")}
        </span>
      </div>
      {budgets.error ? (
        <div className="px-6 pb-4">
          <ErrorAlert error={budgets.error} onRetry={() => void budgets.refetch()} />
        </div>
      ) : budgets.isPending ? (
        <div className="px-6 pb-4">
          <Skeleton className="h-16 w-full" />
        </div>
      ) : budgets.data.length === 0 ? (
        <p className="border-t px-6 py-6 text-sm text-muted-foreground">{t("overview.budgetsEmpty")}</p>
      ) : (
        budgets.data.map((budget) => <BudgetRow key={budget.id} orgId={orgId} budget={budget} />)
      )}
    </Card>
  );
}

function RecentOrders({ orgId }: { orgId: string }) {
  const { t, maybeT } = useI18n();
  const orders = useQuery({
    queryKey: orgKey(orgId, "payment-orders", "recent"),
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/payment-orders", {
            params: { path: { organization_id: orgId }, query: { limit: 3 } },
          }),
        )
      ).data.items,
  });
  return (
    <Card className="min-w-0 flex-[1_1_320px] gap-2 pb-2">
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>{t("overview.ordersTitle")}</CardTitle>
        <Link href={`/o/${orgId}/recharge`} className="text-sm underline-offset-3 hover:underline">
          {t("overview.ordersAll")}
        </Link>
      </CardHeader>
      {orders.error ? (
        <div className="px-6 pb-4">
          <ErrorAlert error={orders.error} />
        </div>
      ) : orders.isPending ? (
        <div className="px-6 pb-4">
          <Skeleton className="h-12 w-full" />
        </div>
      ) : orders.data.length === 0 ? (
        <p className="border-t px-6 py-6 text-sm text-muted-foreground">{t("overview.ordersEmpty")}</p>
      ) : (
        orders.data.map((order) => {
          const state = order.credit_state === "credited" ? "credited" : order.payment_state;
          return (
            <div key={order.id} className="flex items-center gap-3 border-t px-6 py-3">
              <div className="flex flex-1 flex-col">
                <Money value={order.amount} currency={order.currency} className="font-medium" />
                <span className="text-xs text-muted-foreground">{formatDateTime(order.created_at, { seconds: false })}</span>
              </div>
              <StatusBadge tone={order.review_required ? "warning" : toneFor(state)}>
                {order.credit_state === "credited"
                  ? t("recharge.creditStates.credited")
                  : (maybeT(`recharge.paymentStates.${order.payment_state}`) ?? order.payment_state)}
              </StatusBadge>
            </div>
          );
        })
      )}
    </Card>
  );
}

export default function OverviewPage() {
  const { t } = useI18n();
  const workspace = useOrgWorkspace();
  const { orgId } = workspace;
  const canWallet = hasAnyRole(workspace, "org_owner", "org_finance", "platform_admin", "platform_finance");
  const canRecharge = hasAnyRole(workspace, "org_owner", "org_finance");
  // Payment orders need organization billing access or platform finance (payments.md).
  const canOrders = hasAnyRole(workspace, "org_owner", "org_finance", "platform_finance");
  const canKeys = hasAnyRole(workspace, "org_owner", "org_developer", "platform_admin");
  const account = useAccount(orgId, canWallet);
  const monthStart = useMemo(() => startOfUtcMonth(), []);
  const month = useUsage(orgId, "organization", monthStart);
  const currency = account.data?.currency ?? month.data?.currency ?? "USD";
  const restricted = account.data && account.data.status !== "active";

  return (
    <PageContainer>
      <PageHeader
        title={t("overview.title")}
        badge={
          workspace.organization?.status === "active" ? (
            <StatusBadge tone="success">{t("overview.orgActive")}</StatusBadge>
          ) : null
        }
        description={t("overview.subtitle", { currency })}
        actions={
          <>
            {account.data ? (
              <span className="self-center text-xs text-muted-foreground tabular-nums">
                {t("common.dataTime", { time: `${formatDateTime(account.data.as_of)} UTC` })}
              </span>
            ) : null}
            <Button variant="outline" size="icon" aria-label={t("common.refresh")} onClick={() => void account.refetch()}>
              <RefreshCwIcon />
            </Button>
            {canKeys ? (
              <Button variant="outline" asChild>
                <Link href={`/o/${orgId}/keys`}>
                  <KeyRoundIcon />
                  {t("overview.issueKey")}
                </Link>
              </Button>
            ) : null}
            {canRecharge ? (
              <Button asChild>
                <Link href={`/o/${orgId}/recharge`}>
                  <PlusIcon />
                  {t("overview.recharge")}
                </Link>
              </Button>
            ) : null}
          </>
        }
      />

      {restricted ? (
        <Alert variant="destructive">
          <CircleAlertIcon />
          <AlertTitle>{t("overview.restrictedTitle")}</AlertTitle>
          <AlertDescription>{t("overview.restrictedBody")}</AlertDescription>
        </Alert>
      ) : null}

      {canWallet ? (
        account.error ? (
          <ErrorAlert error={account.error} onRetry={() => void account.refetch()} />
        ) : (
          <section aria-label={t("nav.items.wallet")} className="grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-4">
            <KpiCard
              label={t("overview.available")}
              value={account.data?.available_balance}
              hint={t("overview.availableHint")}
              currency={currency}
              loading={account.isPending}
            />
            <KpiCard
              label={t("overview.balance")}
              value={account.data?.balance}
              hint={t("overview.balanceHint", { version: account.data?.version ?? "…" })}
              currency={currency}
              loading={account.isPending}
            />
            <KpiCard
              label={t("overview.held")}
              value={account.data?.held_amount}
              hint={t("overview.heldHint")}
              currency={currency}
              loading={account.isPending}
            />
            <KpiCard
              label={t("overview.monthNet")}
              value={month.data ? (month.data.items[0]?.net_charge ?? "0") : undefined}
              hint={t("overview.monthNetHint", { start: formatDate(monthStart) })}
              loading={month.isPending}
              badge={
                month.data && month.data.watermark.pending_requests > 0 ? (
                  <span className="inline-flex h-5 items-center rounded-md border px-1.5 text-xs">{t("overview.provisional")}</span>
                ) : undefined
              }
            />
          </section>
        )
      ) : (
        <Alert>
          <InfoIcon />
          <AlertDescription>{t("overview.developerNotice")}</AlertDescription>
        </Alert>
      )}

      <div className="flex flex-wrap items-start gap-4">
        <UsageChart orgId={orgId} currency={currency} />
        {canWallet ? <Budgets orgId={orgId} /> : null}
      </div>
      {canOrders ? (
        <div className="flex flex-wrap items-start gap-4">
          <RecentOrders orgId={orgId} />
        </div>
      ) : null}
    </PageContainer>
  );
}
