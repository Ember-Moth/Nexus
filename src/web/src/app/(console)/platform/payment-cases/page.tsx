"use client";

import { useMutation, useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CheckIcon, FileSearchIcon, FlagIcon, ReceiptIcon, RefreshCwIcon } from "lucide-react";
import { Suspense, useMemo, useState } from "react";

import { CopyButton } from "@/components/copy-button";
import { DataTable } from "@/components/data-table";
import { EmptyState, ErrorAlert, Spinner } from "@/components/feedback";
import { Money } from "@/components/money";
import { OrgPicker, useOrgParam } from "@/components/org-picker";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { DetailList } from "@/components/platform-finance/field";
import { JsonDisclosure } from "@/components/platform-finance/json-view";
import { PickOrganization } from "@/components/platform-finance/scope";
import { StatusBadge, toneFor } from "@/components/status-badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { pollUntil, useCursorList } from "@/lib/api/hooks";
import { orgKey } from "@/lib/api/queries";
import type { components } from "@/lib/api/schema";
import { formatDateTime, shortId } from "@/lib/format";

type PaymentCase = components["schemas"]["PaymentCaseView"];
type PaymentOrder = components["schemas"]["PaymentOrderView"];
type Observation = components["schemas"]["PaymentObservationView"];
type Receipt = components["schemas"]["PaymentReceiptView"];

/** How long to keep polling after "请求查单" when the order does not change. */
const MAX_WAIT_MS = 90_000;

function useChannelNames(orgId: string | undefined) {
  // Platform finance may read the organization's active payment methods; disabled channels
  // fall back to a short id.
  const methods = useQuery({
    queryKey: orgKey(orgId ?? "", "payment-methods"),
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/payment-methods", {
            params: { path: { organization_id: orgId! }, query: { limit: 100 } },
          }),
        )
      ).data.items,
    enabled: Boolean(orgId),
    retry: false,
  });
  return useMemo(() => {
    const names = new Map((methods.data ?? []).map((method) => [method.channel_id, method.name] as const));
    return (id: string) => names.get(id) ?? shortId(id);
  }, [methods.data]);
}

function PaymentStateBadge({ order }: { order: PaymentOrder }) {
  const { maybeT } = useI18n();
  return (
    <StatusBadge tone={toneFor(order.payment_state)}>
      {maybeT(`recharge.paymentStates.${order.payment_state}`) ?? order.payment_state}
    </StatusBadge>
  );
}

function CreditStateBadge({ order }: { order: PaymentOrder }) {
  const { t } = useI18n();
  return (
    <StatusBadge tone={order.review_required ? "warning" : toneFor(order.credit_state)}>
      {order.review_required ? t("pf.paymentCases.reviewRequired") : t(`recharge.creditStates.${order.credit_state}`)}
    </StatusBadge>
  );
}

function Observations({ orgId, orderId, refetchInterval }: { orgId: string; orderId: string; refetchInterval: number | false }) {
  const { t, maybeT } = useI18n();
  const list = useCursorList<Observation>({
    queryKey: orgKey(orgId, "payment-orders", orderId, "observations"),
    refetchInterval,
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/payment-orders/{order_id}/observations", {
            params: { path: { organization_id: orgId, order_id: orderId }, query: { limit: 50, cursor } },
          }),
        )
      ).data,
  });
  if (list.error && list.items.length === 0) return <ErrorAlert error={list.error} onRetry={() => void list.refetch()} />;
  if (list.isPending) return <Skeleton className="h-20 w-full" />;
  if (list.items.length === 0) return <p className="text-sm text-muted-foreground">{t("pf.paymentCases.noObservations")}</p>;
  return (
    <div className="flex flex-col gap-2">
      <ul className="flex flex-col gap-2">
        {list.items.map((item) => (
          <li key={item.id} className="flex flex-col gap-2 rounded-md border p-3">
            <span className="flex flex-wrap items-center gap-2 text-[13px]">
              <span className="font-medium">
                {maybeT(`pf.paymentCases.observationSources.${item.source}`) ?? item.source}
              </span>
              <StatusBadge tone={item.disposition === "current" ? "success" : "neutral"}>
                {maybeT(`pf.paymentCases.dispositions.${item.disposition}`) ?? item.disposition}
              </StatusBadge>
              <span className="ml-auto text-xs text-muted-foreground tabular-nums">
                {formatDateTime(item.observed_at)} {t("common.utc")}
              </span>
            </span>
            <span className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <span className="inline-flex items-center gap-1">
                ID <code className="font-mono">{shortId(item.id)}</code>
                <CopyButton value={item.id} />
              </span>
              <span>
                {t("pf.paymentCases.configVersion")} <code className="font-mono">{shortId(item.config_version_id)}</code>
              </span>
            </span>
            <JsonDisclosure title={t("pf.paymentCases.facts")} value={item.facts} />
          </li>
        ))}
      </ul>
      {list.hasNextPage ? (
        <Button variant="outline" size="sm" className="self-start" disabled={list.isFetchingNextPage} onClick={() => void list.fetchNextPage()}>
          {list.isFetchingNextPage ? t("common.loading") : t("common.loadMore")}
        </Button>
      ) : null}
    </div>
  );
}

function Receipts({ orgId, orderId, refetchInterval }: { orgId: string; orderId: string; refetchInterval: number | false }) {
  const { t } = useI18n();
  const list = useCursorList<Receipt>({
    queryKey: orgKey(orgId, "payment-orders", orderId, "receipts"),
    refetchInterval,
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/payment-orders/{order_id}/receipts", {
            params: { path: { organization_id: orgId, order_id: orderId }, query: { limit: 50, cursor } },
          }),
        )
      ).data,
  });
  if (list.error && list.items.length === 0) return <ErrorAlert error={list.error} onRetry={() => void list.refetch()} />;
  if (list.isPending) return <Skeleton className="h-16 w-full" />;
  if (list.items.length === 0) return <p className="text-sm text-muted-foreground">{t("pf.paymentCases.noReceipts")}</p>;
  return (
    <div className="flex flex-col gap-2">
      <ul className="flex flex-col gap-2">
        {list.items.map((item) => (
          <li key={item.id} className="rounded-md border p-3">
            <DetailList
              items={[
                [t("pf.common.amount"), <Money key="a" value={item.amount} currency={item.currency} className="font-medium" />],
                [
                  t("pf.paymentCases.transactionId"),
                  <span key="tx" className="inline-flex items-center gap-1">
                    <code className="font-mono text-xs break-all">{item.transaction_id}</code>
                    <CopyButton value={item.transaction_id} />
                  </span>,
                ],
                [t("pf.paymentCases.providerRef"), <code key="ref" className="font-mono text-xs break-all">{item.provider_payment_ref}</code>],
                [t("pf.paymentCases.confirmedAt"), `${formatDateTime(item.confirmed_at)} ${t("common.utc")}`],
                [t("pf.paymentCases.observation"), <code key="o" className="font-mono text-xs" title={item.observation_id}>{shortId(item.observation_id)}</code>],
              ]}
            />
          </li>
        ))}
      </ul>
      {list.hasNextPage ? (
        <Button variant="outline" size="sm" className="self-start" disabled={list.isFetchingNextPage} onClick={() => void list.fetchNextPage()}>
          {list.isFetchingNextPage ? t("common.loading") : t("common.loadMore")}
        </Button>
      ) : null}
    </div>
  );
}

function idCell(value: string | null | undefined) {
  if (!value) return "—";
  return (
    <span className="inline-flex items-center gap-1">
      <code className="font-mono text-xs break-all">{value}</code>
      <CopyButton value={value} />
    </span>
  );
}

function OrderSheet({
  orgId,
  orderId,
  cases,
  channelName,
  onClose,
}: {
  orgId: string;
  orderId: string;
  cases: PaymentCase[];
  channelName: (id: string) => string;
  onClose: () => void;
}) {
  const { t, maybeT } = useI18n();
  const queryClient = useQueryClient();
  // Set after "请求查单" (202): the order version returned then, and when the request was made.
  const [watch, setWatch] = useState<{ baseline: number; since: number } | null>(null);
  const orderKey: QueryKey = orgKey(orgId, "payment-orders", "detail", orderId);

  const order = useQuery({
    queryKey: orderKey,
    queryFn: async () => {
      const result = (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/payment-orders/{order_id}", {
            params: { path: { organization_id: orgId, order_id: orderId } },
          }),
        )
      ).data;
      if (watch && result.version !== watch.baseline) {
        // The background query changed the order: refresh its evidence and the lists.
        void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "payment-orders", orderId) });
        void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "payment-orders", "list") });
        void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "payment-cases") });
        void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "account") });
      }
      return result;
    },
    // Database read only; polling stops when the order changes or after MAX_WAIT_MS.
    refetchInterval: (query) => {
      if (!watch) return false;
      if (Math.max(query.state.dataUpdatedAt, query.state.errorUpdatedAt) - watch.since > MAX_WAIT_MS) return false;
      return pollUntil<PaymentOrder>((data) => data !== undefined && data.version !== watch.baseline)(query);
    },
  });

  const reconcile = useMutation({
    mutationFn: async () =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/payment-orders/{order_id}/reconcile", {
            params: { path: { organization_id: orgId, order_id: orderId } },
          }),
        )
      ).data,
    onSuccess: (queued) => {
      setWatch({ baseline: queued.version, since: Date.now() });
      void order.refetch();
    },
  });

  const data = order.data;
  const changed = Boolean(watch && data && data.version !== watch.baseline);
  const exhausted = Boolean(
    watch && !changed && Math.max(order.dataUpdatedAt, order.errorUpdatedAt) - watch.since > MAX_WAIT_MS,
  );
  const polling = Boolean(watch && !changed && !exhausted);
  const related = cases.filter((item) => item.order_id === orderId);

  return (
    <Sheet open onOpenChange={(next) => !next && onClose()}>
      <SheetContent className="w-full gap-0 overflow-y-auto sm:max-w-[640px]" closeLabel={t("common.close")}>
        <SheetHeader className="border-b pr-12">
          <SheetTitle>{t("pf.paymentCases.orderTitle")}</SheetTitle>
          <SheetDescription className="flex flex-wrap items-center gap-2">
            {data ? (
              <>
                <code className="font-mono text-xs">{data.merchant_order_ref}</code>
                <CopyButton value={data.merchant_order_ref} />
              </>
            ) : null}
          </SheetDescription>
        </SheetHeader>
        <div className="flex flex-col gap-5 p-4">
          {order.error ? <ErrorAlert error={order.error} onRetry={() => void order.refetch()} /> : null}
          {!data ? (
            <Skeleton className="h-40 w-full" />
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <PaymentStateBadge order={data} />
                <CreditStateBadge order={data} />
                <StatusBadge tone="neutral">
                  {t("pf.paymentCases.checkout")}: {maybeT(`recharge.checkoutStates.${data.checkout_state}`) ?? data.checkout_state}
                </StatusBadge>
                <Button
                  size="sm"
                  className="ml-auto"
                  onClick={() => reconcile.mutate()}
                  disabled={reconcile.isPending || polling}
                >
                  {reconcile.isPending || polling ? <Spinner /> : <RefreshCwIcon />}
                  {t("pf.paymentCases.reconcile")}
                </Button>
              </div>
              {reconcile.error ? <ErrorAlert error={reconcile.error} /> : null}
              {polling ? (
                <p className="flex items-center gap-2 rounded-md border border-dashed bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
                  <Spinner />
                  {t("pf.paymentCases.polling")}
                </p>
              ) : changed ? (
                <Alert className="border-emerald-200 bg-emerald-50 text-emerald-900">
                  <CheckIcon className="text-emerald-700" />
                  <AlertDescription className="text-emerald-900">{t("pf.paymentCases.changed")}</AlertDescription>
                </Alert>
              ) : exhausted ? (
                <p className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">{t("pf.paymentCases.unchanged")}</p>
              ) : null}
              {data.review_required ? (
                <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
                  {t("pf.paymentCases.reviewReason", { reason: data.review_reason ?? "—" })}
                </p>
              ) : null}
              <DetailList
                items={[
                  [t("pf.paymentCases.creditAmount"), <Money key="c" value={data.credit_amount} currency={data.currency} className="font-semibold" />],
                  [
                    t("pf.paymentCases.collection"),
                    <span key="p" className="inline-flex flex-wrap items-center gap-2">
                      <Money value={data.collection_amount} currency={data.collection_currency} />
                      {data.collection_currency !== data.currency ? (
                        <span className="text-xs text-muted-foreground">
                          {t("pf.paymentCases.rate", { rate: data.exchange_rate })}
                        </span>
                      ) : null}
                    </span>,
                  ],
                  [t("pf.paymentCases.channel"), channelName(data.channel_id)],
                  [t("pf.common.createdAt"), `${formatDateTime(data.created_at)} ${t("common.utc")}`],
                  [t("pf.paymentCases.expiresAt"), `${formatDateTime(data.expires_at)} ${t("common.utc")}`],
                  [t("pf.paymentCases.creditedAt"), data.credited_at ? `${formatDateTime(data.credited_at)} ${t("common.utc")}` : "—"],
                  [t("pf.paymentCases.fundingRecord"), idCell(data.funding_record_id)],
                  [t("pf.paymentCases.ledgerEntry"), idCell(data.ledger_entry_id)],
                  [t("pf.paymentCases.orderId"), idCell(data.id)],
                  [t("pf.common.version"), `v${data.version}`],
                ]}
              />
            </>
          )}
          {related.length > 0 ? (
            <section className="flex flex-col gap-2">
              <h3 className="text-[13px] font-semibold text-muted-foreground">{t("pf.paymentCases.relatedCases")}</h3>
              {related.map((item) => (
                <div key={item.id} className="flex flex-col gap-2 rounded-md border p-3">
                  <span className="flex flex-wrap items-center gap-2 text-[13px]">
                    <code className="font-mono">{item.reason}</code>
                    <StatusBadge tone={item.status === "open" ? "warning" : "success"}>
                      {maybeT(`pf.common.states.${item.status}`) ?? item.status}
                    </StatusBadge>
                    <Money value={item.amount_at_risk} className="ml-auto font-medium" />
                  </span>
                  <JsonDisclosure title={t("pf.paymentCases.evidence")} value={item.evidence} defaultOpen />
                </div>
              ))}
            </section>
          ) : null}
          <section className="flex flex-col gap-2">
            <h3 className="text-[13px] font-semibold text-muted-foreground">{t("pf.paymentCases.observations")}</h3>
            <Observations orgId={orgId} orderId={orderId} refetchInterval={polling ? 5_000 : false} />
          </section>
          <section className="flex flex-col gap-2">
            <h3 className="text-[13px] font-semibold text-muted-foreground">{t("pf.paymentCases.receipts")}</h3>
            <Receipts orgId={orgId} orderId={orderId} refetchInterval={polling ? 5_000 : false} />
          </section>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function PaymentCasesContent() {
  const { t, maybeT } = useI18n();
  const [orgId, setOrgId] = useOrgParam();
  const [tab, setTab] = useState("cases");
  const [orderId, setOrderId] = useState<string | null>(null);
  const channelName = useChannelNames(orgId);

  const cases = useCursorList<PaymentCase>({
    queryKey: orgKey(orgId ?? "", "payment-cases", "list"),
    enabled: Boolean(orgId),
    refetchInterval: 30_000,
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/payment-cases", {
            params: { path: { organization_id: orgId! }, query: { limit: 50, cursor } },
          }),
        )
      ).data,
  });

  const orders = useCursorList<PaymentOrder>({
    queryKey: orgKey(orgId ?? "", "payment-orders", "list"),
    enabled: Boolean(orgId),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/payment-orders", {
            params: { path: { organization_id: orgId! }, query: { limit: 50, cursor } },
          }),
        )
      ).data,
  });

  const caseColumns = useMemo<ColumnDef<PaymentCase, unknown>[]>(
    () => [
      {
        header: t("pf.common.createdAt"),
        accessorKey: "created_at",
        cell: ({ row }) => <span className="tabular-nums">{formatDateTime(row.original.created_at, { seconds: false })}</span>,
      },
      {
        header: t("pf.common.reason"),
        accessorKey: "reason",
        cell: ({ row }) => (
          <span title={row.original.reason}>
            {maybeT(`pf.reconciliation.reasons.${row.original.reason}`) ?? (
              <code className="font-mono text-[12.5px]">{row.original.reason}</code>
            )}
          </span>
        ),
      },
      {
        header: t("pf.common.status"),
        accessorKey: "status",
        cell: ({ row }) => (
          <StatusBadge tone={row.original.status === "open" ? "warning" : "success"}>
            {maybeT(`pf.common.states.${row.original.status}`) ?? row.original.status}
          </StatusBadge>
        ),
      },
      {
        header: t("pf.reconciliation.amountAtRisk"),
        accessorKey: "amount_at_risk",
        meta: { align: "right" },
        cell: ({ row }) => <Money value={row.original.amount_at_risk} className="font-medium" />,
      },
      {
        header: t("pf.paymentCases.order"),
        accessorKey: "order_id",
        cell: ({ row }) => (
          <code className="font-mono text-xs" title={row.original.order_id}>
            {shortId(row.original.order_id)}
          </code>
        ),
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("pf.common.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <Button size="sm" variant="outline" onClick={() => setOrderId(row.original.order_id)}>
            <FileSearchIcon />
            {t("pf.paymentCases.viewOrder")}
          </Button>
        ),
      },
    ],
    [t, maybeT],
  );

  const orderColumns = useMemo<ColumnDef<PaymentOrder, unknown>[]>(
    () => [
      {
        header: t("recharge.columns.ref"),
        accessorKey: "merchant_order_ref",
        cell: ({ row }) => <code className="font-mono text-[12.5px]">{row.original.merchant_order_ref}</code>,
      },
      {
        header: t("recharge.columns.channel"),
        accessorKey: "channel_id",
        cell: ({ row }) => channelName(row.original.channel_id),
      },
      {
        header: t("recharge.columns.credit"),
        accessorKey: "credit_amount",
        meta: { align: "right" },
        cell: ({ row }) => <Money value={row.original.credit_amount} currency={row.original.currency} className="font-medium" />,
      },
      {
        header: t("recharge.columns.paid"),
        accessorKey: "collection_amount",
        meta: { align: "right" },
        cell: ({ row }) => (
          <Money value={row.original.collection_amount} currency={row.original.collection_currency} className="text-muted-foreground" />
        ),
      },
      { header: t("recharge.columns.payment"), accessorKey: "payment_state", cell: ({ row }) => <PaymentStateBadge order={row.original} /> },
      { header: t("recharge.columns.credit_state"), accessorKey: "credit_state", cell: ({ row }) => <CreditStateBadge order={row.original} /> },
      {
        header: t("recharge.columns.created"),
        accessorKey: "created_at",
        cell: ({ row }) => <span className="text-muted-foreground tabular-nums">{formatDateTime(row.original.created_at, { seconds: false })}</span>,
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("pf.common.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <Button size="icon-sm" variant="ghost" aria-label={t("pf.common.details")} onClick={() => setOrderId(row.original.id)}>
            <FileSearchIcon />
          </Button>
        ),
      },
    ],
    [t, channelName],
  );

  return (
    <PageContainer>
      <PageHeader
        title={t("pf.paymentCases.title")}
        description={t("pf.paymentCases.subtitle")}
        actions={<OrgPicker value={orgId} onChange={setOrgId} />}
      />
      {!orgId ? (
        <PickOrganization />
      ) : (
        <Tabs value={tab} onValueChange={setTab} className="gap-3">
          <TabsList>
            <TabsTrigger value="cases">
              {t("pf.paymentCases.casesTab")}
              {cases.items.length > 0 ? <span className="text-xs text-muted-foreground tabular-nums">{cases.items.length}{cases.hasNextPage ? "+" : ""}</span> : null}
            </TabsTrigger>
            <TabsTrigger value="orders">{t("pf.paymentCases.ordersTab")}</TabsTrigger>
          </TabsList>
          <TabsContent value="cases">
            <DataTable
              columns={caseColumns}
              data={cases.items}
              loading={cases.isPending}
              error={cases.error}
              onRetry={() => void cases.refetch()}
              getRowId={(row) => row.id}
              minWidth={820}
              empty={
                <EmptyState
                  icon={<FlagIcon className="size-5" />}
                  title={t("pf.paymentCases.casesEmpty")}
                  description={t("pf.paymentCases.casesEmptyBody")}
                />
              }
              footer={
                cases.items.length > 0 ? (
                  <LoadMoreFooter
                    count={cases.items.length}
                    hasNext={Boolean(cases.hasNextPage)}
                    loading={cases.isFetchingNextPage}
                    onLoadMore={() => void cases.fetchNextPage()}
                  />
                ) : null
              }
            />
          </TabsContent>
          <TabsContent value="orders">
            <DataTable
              columns={orderColumns}
              data={orders.items}
              loading={orders.isPending}
              error={orders.error}
              onRetry={() => void orders.refetch()}
              getRowId={(row) => row.id}
              minWidth={900}
              rowClassName={(row) => (row.review_required ? "bg-amber-50/50" : undefined)}
              empty={<EmptyState icon={<ReceiptIcon className="size-5" />} title={t("pf.paymentCases.ordersEmpty")} />}
              footer={
                orders.items.length > 0 ? (
                  <LoadMoreFooter
                    count={orders.items.length}
                    hasNext={Boolean(orders.hasNextPage)}
                    loading={orders.isFetchingNextPage}
                    onLoadMore={() => void orders.fetchNextPage()}
                  />
                ) : null
              }
            />
          </TabsContent>
        </Tabs>
      )}
      {orgId && orderId ? (
        <OrderSheet
          key={orderId}
          orgId={orgId}
          orderId={orderId}
          cases={cases.items}
          channelName={channelName}
          onClose={() => setOrderId(null)}
        />
      ) : null}
    </PageContainer>
  );
}

export default function PaymentCasesPage() {
  return (
    <Suspense>
      <PaymentCasesContent />
    </Suspense>
  );
}
