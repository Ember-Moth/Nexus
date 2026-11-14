"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { CheckIcon, ExternalLinkIcon } from "lucide-react";
import { useMemo, useState } from "react";

import { CopyButton } from "@/components/copy-button";
import { DataTable } from "@/components/data-table";
import { EmptyState, ErrorAlert, Spinner } from "@/components/feedback";
import { Money } from "@/components/money";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { useOrgWorkspace } from "@/components/shell/workspace";
import { StatusBadge, toneFor } from "@/components/status-badge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { pollUntil, useCursorList, useIdempotentMutation } from "@/lib/api/hooks";
import { orgKey } from "@/lib/api/queries";
import type { PaymentMethod, PaymentOrder } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { checkAmount, formatMoney } from "@/lib/money";
import { cn } from "@/lib/utils";

const terminalPayment = new Set(["failed", "cancelled", "reversed", "partially_reversed"]);

/** An order needs no more polling once it is credited, failed, closed or waiting for finance. */
function isSettled(order: PaymentOrder | undefined) {
  if (!order) return false;
  return (
    order.credit_state === "credited" ||
    order.review_required ||
    terminalPayment.has(order.payment_state) ||
    order.checkout_state === "closed"
  );
}

function OrderProgress({ orgId, orderId }: { orgId: string; orderId: string }) {
  const { t, maybeT } = useI18n();
  const queryClient = useQueryClient();
  // The create call returned 202: poll the order (database read only) until it settles.
  const order = useQuery({
    queryKey: orgKey(orgId, "payment-orders", "detail", orderId),
    queryFn: async () => {
      const result = (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/payment-orders/{order_id}", {
            params: { path: { organization_id: orgId, order_id: orderId } },
          }),
        )
      ).data;
      if (result.credit_state === "credited") {
        void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "account") });
        void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "ledger") });
      }
      return result;
    },
    refetchInterval: pollUntil<PaymentOrder>(isSettled),
  });

  if (order.error) return <ErrorAlert error={order.error} onRetry={() => void order.refetch()} />;
  const data = order.data;
  if (!data) return <Skeleton className="h-32 w-full" />;
  const checkout = data.checkout;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <span className="text-xs text-muted-foreground">{t("recharge.orderRef")}</span>
        <span className="flex items-center gap-1">
          <code className="font-mono text-[13px]">{data.merchant_order_ref}</code>
          <CopyButton value={data.merchant_order_ref} />
        </span>
      </div>
      {data.checkout_state === "creating" ? (
        <div className="flex flex-col gap-2 rounded-lg border border-dashed bg-muted/40 p-3.5">
          <span className="flex items-center gap-2 font-medium">
            <Spinner />
            {t("recharge.creatingCheckout")}
          </span>
          <span className="text-xs leading-relaxed text-muted-foreground">{t("recharge.creatingCheckoutBody")}</span>
        </div>
      ) : data.checkout_state === "ready" && checkout ? (
        <div className="flex flex-col gap-2.5 rounded-lg border p-3.5">
          <span className="flex items-center gap-2 font-medium">
            <CheckIcon className="size-4 text-emerald-700" aria-hidden />
            {t("recharge.checkoutReady")}
          </span>
          {checkout.kind === "redirect" ? (
            <Button asChild>
              <a href={checkout.destination} target="_blank" rel="noopener noreferrer">
                {t("recharge.goPay")}
                <ExternalLinkIcon />
              </a>
            </Button>
          ) : (
            <code className="rounded bg-muted p-2 font-mono text-xs break-all">{checkout.destination}</code>
          )}
          <span className="text-xs text-muted-foreground">
            {t("recharge.checkoutExpires", { time: `${formatDateTime(checkout.expires_at, { seconds: false })} UTC` })}
          </span>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">
          {maybeT(`recharge.checkoutStates.${data.checkout_state}`) ?? data.checkout_state}
        </p>
      )}
      <div className="grid grid-cols-2 gap-2">
        <div className="flex flex-col gap-1 rounded-md border px-3 py-2.5">
          <span className="text-xs text-muted-foreground">{t("recharge.paymentState")}</span>
          <StatusBadge tone={toneFor(data.payment_state)}>
            {maybeT(`recharge.paymentStates.${data.payment_state}`) ?? data.payment_state}
          </StatusBadge>
        </div>
        <div className="flex flex-col gap-1 rounded-md border px-3 py-2.5">
          <span className="text-xs text-muted-foreground">{t("recharge.creditState")}</span>
          <StatusBadge tone={toneFor(data.credit_state)}>{t(`recharge.creditStates.${data.credit_state}`)}</StatusBadge>
        </div>
      </div>
      {data.credit_state === "credited" ? (
        <Alert className="border-emerald-200 bg-emerald-50 text-emerald-900">
          <AlertDescription className="text-emerald-900">
            {t("recharge.credited", { amount: `${data.currency} ${formatMoney(data.credit_amount)}` })}
          </AlertDescription>
        </Alert>
      ) : data.review_required ? (
        <Alert className="border-amber-200 bg-amber-50 text-amber-900">
          <AlertDescription className="text-amber-900">
            {t("recharge.reviewRequired", { reason: data.review_reason ?? "—" })}
          </AlertDescription>
        </Alert>
      ) : !isSettled(data) ? (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner />
          {t("recharge.waitingPayment")}
        </p>
      ) : null}
    </div>
  );
}

export default function RechargePage() {
  const { t, maybeT } = useI18n();
  const { orgId } = useOrgWorkspace();
  const queryClient = useQueryClient();
  const [channelId, setChannelId] = useState<string>("");
  const [amount, setAmount] = useState("");
  const [touched, setTouched] = useState(false);
  const [activeOrder, setActiveOrder] = useState<string | null>(null);

  const methods = useQuery({
    queryKey: orgKey(orgId, "payment-methods"),
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/payment-methods", {
            params: { path: { organization_id: orgId }, query: { limit: 100 } },
          }),
        )
      ).data.items,
  });
  const method: PaymentMethod | undefined =
    methods.data?.find((m) => m.channel_id === channelId) ?? methods.data?.[0];
  const problem = method
    ? checkAmount(amount, { decimals: method.decimal_places, min: method.min_amount, max: method.max_amount })
    : "required";

  // POST /payment-orders returns 202 and requires Idempotency-Key; retries reuse the key so a
  // lost response never creates a second order.
  const create = useIdempotentMutation<PaymentOrder, { channel_id: string; amount: string; currency: string }>({
    keyPrefix: "payment-order",
    mutationFn: async (body, idempotencyKey) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/payment-orders", {
            params: { path: { organization_id: orgId }, header: { "Idempotency-Key": idempotencyKey } },
            body,
          }),
        )
      ).data,
    onSuccess: (order) => {
      setActiveOrder(order.id);
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "payment-orders") });
    },
  });

  const orders = useCursorList<PaymentOrder>({
    queryKey: orgKey(orgId, "payment-orders", "list"),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/payment-orders", {
            params: { path: { organization_id: orgId }, query: { limit: 20, cursor } },
          }),
        )
      ).data,
    refetchInterval: 10_000,
  });

  const columns = useMemo<ColumnDef<PaymentOrder, unknown>[]>(
    () => [
      {
        header: t("recharge.columns.ref"),
        accessorKey: "merchant_order_ref",
        cell: ({ row }) => <code className="font-mono text-[12.5px]">{row.original.merchant_order_ref}</code>,
      },
      {
        header: t("recharge.columns.channel"),
        accessorKey: "channel_id",
        cell: ({ row }) =>
          methods.data?.find((m) => m.channel_id === row.original.channel_id)?.name ?? (
            <code className="font-mono text-xs">{row.original.channel_id.slice(0, 8)}</code>
          ),
      },
      {
        header: t("recharge.columns.credit"),
        accessorKey: "credit_amount",
        meta: { align: "right" },
        cell: ({ row }) => <Money value={row.original.credit_amount} className="font-medium" />,
      },
      {
        header: t("recharge.columns.paid"),
        accessorKey: "collection_amount",
        meta: { align: "right" },
        cell: ({ row }) => (
          <Money value={row.original.collection_amount} currency={row.original.collection_currency} className="text-muted-foreground" />
        ),
      },
      {
        header: t("recharge.columns.payment"),
        accessorKey: "payment_state",
        cell: ({ row }) => (
          <StatusBadge tone={toneFor(row.original.payment_state)}>
            {maybeT(`recharge.paymentStates.${row.original.payment_state}`) ?? row.original.payment_state}
          </StatusBadge>
        ),
      },
      {
        header: t("recharge.columns.credit_state"),
        accessorKey: "credit_state",
        cell: ({ row }) => (
          <StatusBadge tone={row.original.review_required ? "warning" : toneFor(row.original.credit_state)}>
            {t(`recharge.creditStates.${row.original.credit_state}`)}
          </StatusBadge>
        ),
      },
      {
        header: t("recharge.columns.created"),
        accessorKey: "created_at",
        cell: ({ row }) => <span className="text-muted-foreground tabular-nums">{formatDateTime(row.original.created_at, { seconds: false })}</span>,
      },
    ],
    [t, maybeT, methods.data],
  );

  const problemText =
    problem && method
      ? t(`recharge.amountProblems.${problem}`, {
          decimals: method.decimal_places,
          min: formatMoney(method.min_amount),
          max: formatMoney(method.max_amount),
        })
      : null;

  return (
    <PageContainer>
      <PageHeader title={t("recharge.title")} description={t("recharge.subtitle")} />
      <div className="flex flex-wrap items-stretch gap-4">
        <form
          noValidate
          aria-labelledby="step-1"
          className="flex min-w-0 flex-[1_1_340px] flex-col gap-4 rounded-xl border p-5"
          onSubmit={(event) => {
            event.preventDefault();
            setTouched(true);
            if (!method || problem) return;
            create.mutate({ channel_id: method.channel_id, amount: amount.trim(), currency: method.currency });
          }}
        >
          <h2 id="step-1" className="text-base font-semibold">
            1 · {t("recharge.step1")}
          </h2>
          {methods.error ? <ErrorAlert error={methods.error} onRetry={() => void methods.refetch()} /> : null}
          {methods.isPending ? (
            <Skeleton className="h-28 w-full" />
          ) : methods.data && methods.data.length === 0 ? (
            <p className="rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">{t("recharge.noMethods")}</p>
          ) : (
            <fieldset className="flex flex-col gap-2">
              <legend className="mb-2 text-sm font-medium">{t("recharge.method")}</legend>
              <RadioGroup value={method?.channel_id ?? ""} onValueChange={setChannelId} className="gap-2">
                {(methods.data ?? []).map((item) => (
                  <Label
                    key={item.channel_id}
                    className={cn(
                      "items-start rounded-lg border px-3 py-2.5 font-normal",
                      item.channel_id === method?.channel_id && "border-foreground bg-muted/40",
                    )}
                  >
                    <RadioGroupItem value={item.channel_id} className="mt-0.5" />
                    <span className="flex flex-col gap-0.5">
                      <span className="flex items-center gap-1.5 font-medium">
                        {item.name}
                        {item.environment !== "live" ? (
                          <StatusBadge tone="warning">{t("recharge.testEnvironment")}</StatusBadge>
                        ) : null}
                      </span>
                      <span className="text-xs text-muted-foreground tabular-nums">
                        {t("recharge.methodMeta", {
                          currency: item.currency,
                          decimals: item.decimal_places,
                          min: formatMoney(item.min_amount),
                          max: formatMoney(item.max_amount),
                        })}
                      </span>
                    </span>
                  </Label>
                ))}
              </RadioGroup>
            </fieldset>
          )}
          <div className="grid gap-2">
            <Label htmlFor="amount">{t("recharge.amount", { currency: method?.currency ?? "USD" })}</Label>
            <Input
              id="amount"
              inputMode="decimal"
              autoComplete="off"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
              onBlur={() => setTouched(true)}
              aria-invalid={(touched && Boolean(problem)) || undefined}
              aria-describedby="amount-hint"
              className="h-10 text-base font-medium tabular-nums"
            />
            <span id="amount-hint" className={cn("text-xs", touched && problemText ? "text-destructive" : "text-muted-foreground")}>
              {touched && problemText ? problemText : t("recharge.amountHint")}
            </span>
          </div>
          {create.error ? <ErrorAlert error={create.error} /> : null}
          <Button type="submit" size="lg" disabled={!method || create.isPending}>
            {create.isPending ? <Spinner /> : null}
            {create.isPending ? t("recharge.creating") : t("recharge.create")}
          </Button>
        </form>
        <section aria-labelledby="step-2" className="flex min-w-0 flex-[1_1_320px] flex-col gap-4 rounded-xl border p-5">
          <h2 id="step-2" className="text-base font-semibold">
            2 · {t("recharge.step2")} / 3 · {t("recharge.step3")}
          </h2>
          {activeOrder ? (
            <OrderProgress orgId={orgId} orderId={activeOrder} />
          ) : (
            <p className="text-sm text-muted-foreground">{t("recharge.noOrderYet")}</p>
          )}
        </section>
      </div>
      <section aria-labelledby="orders" className="flex flex-col gap-3">
        <div className="flex items-center gap-2">
          <h2 id="orders" className="flex-1 text-lg font-semibold">
            {t("recharge.ordersTitle")}
          </h2>
          <span className="text-xs text-muted-foreground">{t("recharge.ordersRefresh")}</span>
        </div>
        <DataTable
          columns={columns}
          data={orders.items}
          loading={orders.isPending}
          error={orders.error}
          onRetry={() => void orders.refetch()}
          getRowId={(row) => row.id}
          minWidth={860}
          empty={<EmptyState title={t("recharge.ordersEmpty")} />}
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
      </section>
    </PageContainer>
  );
}
