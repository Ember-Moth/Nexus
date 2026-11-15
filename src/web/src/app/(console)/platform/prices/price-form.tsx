"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Big from "big.js";
import { Controller, useForm, useWatch, type Control, type FieldErrors, type UseFormRegister } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { ConflictAlert, Spinner } from "@/components/feedback";
import { Money } from "@/components/money";
import { PcErrorAlert } from "@/components/platform-catalog/errors";
import { Field } from "@/components/platform-catalog/form";
import { isTokenDimension, isToolDimension, type AnyDimension } from "@/components/platform-catalog/labels";
import { isCurrency, isUnitPrice, unitLabel, unitOptions, type UnitQuantity } from "@/components/platform-catalog/pricing";
import { PublishForm } from "@/components/platform-catalog/publish-dialog";
import type { PriceKind, PriceVersion } from "@/components/platform-catalog/types";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import { useIdempotentMutation } from "@/lib/api/hooks";
import { shortId } from "@/lib/format";
import { formatMoney } from "@/lib/money";

export const pricesKey = (kind: PriceKind, ...rest: unknown[]) => ["prices", kind, ...rest] as const;

export async function fetchPrice(kind: PriceKind, id: string) {
  const path = { price_version_id: id };
  return kind === "sale"
    ? call(() => nexus.GET("/api/v1/sale-price-versions/{price_version_id}", { params: { path } }))
    : call(() => nexus.GET("/api/v1/cost-price-versions/{price_version_id}", { params: { path } }));
}

/** Single version with its ETag; If-Match for PATCH and publish. */
export function usePriceDetail(kind: PriceKind, id: string | undefined) {
  return useQuery({
    queryKey: pricesKey(kind, "detail", id ?? null),
    queryFn: () => fetchPrice(kind, id!),
    enabled: Boolean(id),
    staleTime: 0,
  });
}

const schema = z.object({
  currency: z.string().trim().refine(isCurrency),
  enabled: z.boolean(),
  rates: z.array(z.object({ dimension: z.string(), unitPrice: z.string().refine(isUnitPrice), unitQuantity: z.string() })),
});
type Values = z.infer<typeof schema>;
type Rates = PriceVersion["rates"];
type PriceBody = { currency: string; rates: Rates; enabled: boolean };

function bodyOf(values: Values): PriceBody {
  return {
    currency: values.currency.trim().toUpperCase(),
    enabled: values.enabled,
    rates: Object.fromEntries(
      values.rates.map((rate) => [
        rate.dimension,
        { unit_price: rate.unitPrice.trim(), unit_quantity: Number(rate.unitQuantity) as UnitQuantity },
      ]),
    ),
  };
}

/** Rows follow the model's current dimensions; prices of a source version are reused where they fit. */
function defaults(dimensions: AnyDimension[], source: PriceVersion | null, currency: string): Values {
  return {
    currency: source?.currency ?? currency,
    enabled: source?.enabled ?? true,
    rates: dimensions.map((dimension) => {
      const rate = source?.rates[dimension];
      const allowed = unitOptions(dimension).map((o) => o.quantity);
      const quantity = rate && allowed.includes(rate.unit_quantity) ? rate.unit_quantity : allowed[0];
      return { dimension, unitPrice: rate?.unit_price ?? "", unitQuantity: String(quantity) };
    }),
  };
}

export function dimensionLabel(t: ReturnType<typeof useT>, dimension: string) {
  if (isTokenDimension(dimension)) return t(`pc.enums.tokenDimension.${dimension}`);
  if (isToolDimension(dimension)) return t(`pc.enums.toolDimension.${dimension}`);
  return dimension;
}

/** price − live price when both use the same unit; null when not comparable. */
function delta(price: string, quantity: number, live: PriceVersion | null | undefined, dimension: string) {
  const base = live?.rates[dimension];
  if (!base || base.unit_quantity !== quantity || !isUnitPrice(price)) return null;
  return new Big(price.trim()).minus(base.unit_price).toFixed();
}

function Delta({ value }: { value: string | null }) {
  const t = useT();
  if (value === null) return <span className="text-muted-foreground">{t("pc.prices.rates.sameAsLive")}</span>;
  const zero = new Big(value).eq(0);
  return (
    <span className={zero ? "text-muted-foreground tabular-nums" : "tabular-nums"}>
      {zero ? "0" : formatMoney(value, { signed: true })}
    </span>
  );
}

/** Editable rate grid: dimension, unit price, price unit and the difference to the live version. */
function RateRows({
  control,
  register,
  errors,
  rows,
  live,
}: {
  control: Control<Values>;
  register: UseFormRegister<Values>;
  errors: FieldErrors<Values>;
  rows: Values["rates"];
  live?: PriceVersion | null;
}) {
  const t = useT();
  const watched = useWatch({ control, name: "rates" });
  const currency = useWatch({ control, name: "currency" });
  const grid = live ? "grid-cols-[1.5fr_1.4fr_1fr_0.8fr]" : "grid-cols-[1.5fr_1.4fr_1fr]";
  return (
    <div className="overflow-x-auto">
      <div className="min-w-[600px]">
        <div className={`grid ${grid} h-9 items-center gap-3 border-b bg-muted/50 px-4 text-xs font-medium text-muted-foreground`}>
          <span>{t("pc.prices.rates.dimension")}</span>
          <span>
            {t("pc.prices.rates.unitPrice")}
            {currency ? `（${currency.toUpperCase()}）` : ""}
          </span>
          <span>{t("pc.prices.rates.unit")}</span>
          {live ? (
            <span className="text-right">
              {t("pc.prices.rates.delta")} <code className="font-mono">{shortId(live.id)}</code>
            </span>
          ) : null}
        </div>
        {rows.map((row, index) => {
          const dimension = row.dimension as AnyDimension;
          const options = unitOptions(dimension);
          const invalid = Boolean(errors.rates?.[index]?.unitPrice);
          const current = watched?.[index];
          return (
            <div key={dimension} className={`grid ${grid} items-start gap-3 border-b px-4 py-2.5 last:border-b-0`}>
              <span className="flex min-w-0 flex-col gap-0.5 pt-1.5">
                <span className="text-[13px] font-medium">{dimensionLabel(t, dimension)}</span>
                <code className="truncate font-mono text-[11px] text-muted-foreground">{dimension}</code>
              </span>
              <span className="flex flex-col gap-1">
                <Input
                  inputMode="decimal"
                  autoComplete="off"
                  placeholder="0.000000"
                  className="font-mono tabular-nums"
                  aria-label={`${dimensionLabel(t, dimension)} ${t("pc.prices.rates.unitPrice")}`}
                  aria-invalid={invalid || undefined}
                  {...register(`rates.${index}.unitPrice`)}
                />
                {invalid ? <span className="text-xs text-destructive">{t("pc.prices.form.priceInvalid")}</span> : null}
              </span>
              <Controller
                control={control}
                name={`rates.${index}.unitQuantity`}
                render={({ field }) => (
                  <Select value={field.value} onValueChange={field.onChange} disabled={options.length === 1}>
                    <SelectTrigger className="w-full" aria-label={`${dimensionLabel(t, dimension)} ${t("pc.prices.rates.unit")}`}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {options.map((option) => (
                        <SelectItem key={option.quantity} value={String(option.quantity)}>
                          {t(option.label)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
              {live ? (
                <span className="pt-2 text-right text-[13px]">
                  <Delta value={current ? delta(current.unitPrice, Number(current.unitQuantity), live, dimension) : null} />
                </span>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** Read-only rates of a published version, with the difference to the live version. */
export function RatesTable({ version, live }: { version: PriceVersion; live?: PriceVersion | null }) {
  const t = useT();
  const compare = live && live.id !== version.id ? live : null;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] text-[13px]">
        <thead className="bg-muted/50 text-xs text-muted-foreground">
          <tr className="h-9 text-left">
            <th className="px-4 font-medium">{t("pc.prices.rates.dimension")}</th>
            <th className="px-4 text-right font-medium">
              {t("pc.prices.rates.unitPrice")}（{version.currency}）
            </th>
            <th className="px-4 font-medium">{t("pc.prices.rates.unit")}</th>
            {compare ? (
              <th className="px-4 text-right font-medium">
                {t("pc.prices.rates.delta")} <code className="font-mono">{shortId(compare.id)}</code>
              </th>
            ) : null}
          </tr>
        </thead>
        <tbody className="divide-y border-t">
          {Object.entries(version.rates).map(([dimension, rate]) => {
            const unit = unitLabel(dimension, rate.unit_quantity);
            return (
              <tr key={dimension}>
                <td className="px-4 py-2">
                  <span className="flex flex-col gap-0.5">
                    <span className="font-medium">{dimensionLabel(t, dimension)}</span>
                    <code className="font-mono text-[11px] text-muted-foreground">{dimension}</code>
                  </span>
                </td>
                <td className="px-4 py-2 text-right">
                  <Money value={rate.unit_price} />
                </td>
                <td className="px-4 py-2 text-muted-foreground">{unit ? t(unit) : `/ ${rate.unit_quantity}`}</td>
                {compare ? (
                  <td className="px-4 py-2 text-right">
                    <Delta value={delta(rate.unit_price, rate.unit_quantity, compare, dimension)} />
                  </td>
                ) : null}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function CurrencyField({ register, invalid }: { register: UseFormRegister<Values>; invalid: boolean }) {
  const t = useT();
  return (
    <Field
      id="price-currency"
      label={t("pc.prices.form.currency")}
      hint={t("pc.prices.form.currencyHint")}
      error={invalid ? t("pc.prices.form.currencyInvalid") : undefined}
    >
      <Input
        id="price-currency"
        maxLength={3}
        autoComplete="off"
        className="w-28 font-mono uppercase"
        aria-invalid={invalid || undefined}
        {...register("currency", { setValueAs: (value: string) => value.toUpperCase() })}
      />
    </Field>
  );
}

function EnabledSwitch({ control }: { control: Control<Values> }) {
  const t = useT();
  return (
    <Controller
      control={control}
      name="enabled"
      render={({ field }) => (
        <Label htmlFor="price-enabled" className="gap-2 text-[13px] font-normal" title={t("pc.prices.form.enabledHint")}>
          <Switch id="price-enabled" checked={field.value} onCheckedChange={field.onChange} />
          {t("pc.prices.form.enabled")}
        </Label>
      )}
    />
  );
}

/** POST a new draft with Idempotency-Key; prefilled from `source` when copying a version. */
export function CreatePriceDialog({
  kind,
  scopeId,
  source,
  live,
  dimensions,
  defaultCurrency,
  onCreated,
  onClose,
}: {
  kind: PriceKind;
  scopeId: string;
  source: PriceVersion | null;
  live: PriceVersion | null;
  dimensions: AnyDimension[];
  defaultCurrency: string;
  onCreated: (version: PriceVersion) => void;
  onClose: () => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const initial = defaults(dimensions, source, defaultCurrency);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: initial });
  const errors = form.formState.errors;

  const create = useIdempotentMutation<PriceVersion, PriceBody>({
    keyPrefix: `create-${kind}-price`,
    mutationFn: async (body, idempotencyKey) => {
      const params = { header: { "Idempotency-Key": idempotencyKey } };
      return kind === "sale"
        ? (await call(() => nexus.POST("/api/v1/sale-price-versions", { params, body: { ...body, model_id: scopeId } }))).data
        : (await call(() => nexus.POST("/api/v1/cost-price-versions", { params, body: { ...body, deployment_id: scopeId } })))
            .data;
    },
    onSuccess: (created) => {
      toast.success(t("pc.prices.form.created"));
      void queryClient.invalidateQueries({ queryKey: pricesKey(kind) });
      onCreated(created);
      onClose();
    },
  });

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (next) return;
        create.resetOperation();
        onClose();
      }}
    >
      <DialogContent className="max-h-[92vh] overflow-y-auto sm:max-w-[760px]">
        <form noValidate onSubmit={form.handleSubmit((values) => create.mutate(bodyOf(values)))} className="grid gap-5">
          <DialogHeader>
            <DialogTitle>{t("pc.prices.form.createTitle")}</DialogTitle>
            <DialogDescription>{t("pc.prices.form.description")}</DialogDescription>
          </DialogHeader>
          {create.error ? <PcErrorAlert error={create.error} /> : null}
          {dimensions.length === 0 ? (
            <p className="text-sm text-destructive">{t("pc.prices.form.noDimensions")}</p>
          ) : (
            <>
              <div className="flex flex-wrap items-end justify-between gap-4">
                <CurrencyField register={form.register} invalid={Boolean(errors.currency)} />
                <EnabledSwitch control={form.control} />
              </div>
              <div className="overflow-hidden rounded-lg border">
                <RateRows control={form.control} register={form.register} errors={errors} rows={initial.rates} live={live} />
              </div>
              <p className="text-xs text-muted-foreground">{t("pc.prices.form.footer")}</p>
            </>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={create.isPending || dimensions.length === 0}>
              {create.isPending ? <Spinner /> : null}
              {t("pc.prices.create")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

interface PublishVariables {
  id: string;
  ifMatch: string;
  effective_at: string | null;
}

/**
 * Inline draft editor and publish form. PATCH replaces currency, rates and enabled with the
 * single GET's ETag; publish sends that ETag plus an Idempotency-Key per (version, time).
 */
export function DraftPanel({
  kind,
  draft,
  live,
  dimensions,
  notBefore,
  now,
}: {
  kind: PriceKind;
  draft: PriceVersion;
  live: PriceVersion | null;
  dimensions: AnyDimension[];
  notBefore: string | null;
  now: number;
}) {
  const detail = usePriceDetail(kind, draft.id);
  const current = detail.data?.data;
  if (detail.error && !current) return <PcErrorAlert error={detail.error} onRetry={() => void detail.refetch()} />;
  if (!current) {
    return (
      <div className="flex justify-center rounded-xl border py-10">
        <Spinner />
      </div>
    );
  }
  return (
    <DraftForm
      key={`${current.id}:${current.version}`}
      kind={kind}
      draft={current}
      etag={detail.data?.etag ?? `"${current.version}"`}
      live={live}
      dimensions={dimensions}
      notBefore={notBefore}
      now={now}
      reloading={detail.isFetching}
      onReload={() => void detail.refetch()}
    />
  );
}

function DraftForm({
  kind,
  draft,
  etag,
  live,
  dimensions,
  notBefore,
  now,
  reloading,
  onReload,
}: {
  kind: PriceKind;
  draft: PriceVersion;
  etag: string;
  live: PriceVersion | null;
  dimensions: AnyDimension[];
  notBefore: string | null;
  now: number;
  reloading: boolean;
  onReload: () => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const initial = defaults(dimensions, draft, draft.currency);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: initial });
  const errors = form.formState.errors;
  // The model may have gained or lost dimensions since the draft was saved.
  const stale =
    dimensions.length !== Object.keys(draft.rates).length || dimensions.some((d) => !(d in draft.rates));
  const dirty = form.formState.isDirty || stale;
  const refresh = () => void queryClient.invalidateQueries({ queryKey: pricesKey(kind) });

  const save = useMutation({
    mutationFn: async (body: PriceBody) => {
      const params = { path: { price_version_id: draft.id }, header: { "If-Match": etag } };
      return kind === "sale"
        ? (await call(() => nexus.PATCH("/api/v1/sale-price-versions/{price_version_id}", { params, body }))).data
        : (await call(() => nexus.PATCH("/api/v1/cost-price-versions/{price_version_id}", { params, body }))).data;
    },
    onSuccess: () => {
      toast.success(t("pc.prices.form.saved"));
      refresh();
    },
  });

  const publish = useIdempotentMutation<PriceVersion, PublishVariables>({
    keyPrefix: `publish-${kind}-price`,
    mutationFn: async ({ id, ifMatch, effective_at }, idempotencyKey) => {
      const params = { path: { price_version_id: id }, header: { "If-Match": ifMatch, "Idempotency-Key": idempotencyKey } };
      const body = effective_at ? { effective_at } : {};
      return kind === "sale"
        ? (await call(() => nexus.POST("/api/v1/sale-price-versions/{price_version_id}/publish", { params, body }))).data
        : (await call(() => nexus.POST("/api/v1/cost-price-versions/{price_version_id}/publish", { params, body }))).data;
    },
    onSuccess: () => {
      toast.success(t("pc.publish.done"));
      refresh();
    },
  });

  const conflict = isConflict(save.error);

  return (
    <div className="flex flex-col gap-4">
      <form
        noValidate
        onSubmit={form.handleSubmit((values) => save.mutate(bodyOf(values)))}
        className="overflow-hidden rounded-xl border"
        aria-labelledby="draft-title"
      >
        <div className="flex flex-wrap items-center gap-3 border-b px-4 py-3">
          <h2 id="draft-title" className="text-[15px] font-semibold">
            {t("pc.prices.draftTitle")} <code className="font-mono text-[13px] font-normal text-muted-foreground">{shortId(draft.id)}</code>
          </h2>
          {dirty ? (
            <span className="inline-flex h-5 items-center rounded-md border border-dashed border-muted-foreground/60 px-1.5 text-[11.5px]">
              {t("pc.prices.unsaved")}
            </span>
          ) : null}
          <span className="flex-1" />
          <EnabledSwitch control={form.control} />
        </div>
        {conflict ? (
          <div className="border-b p-4">
            <ConflictAlert
              onReload={() => {
                save.reset();
                onReload();
              }}
            />
          </div>
        ) : save.error ? (
          <div className="border-b p-4">
            <PcErrorAlert error={save.error} />
          </div>
        ) : null}
        <div className="border-b px-4 py-3">
          <CurrencyField register={form.register} invalid={Boolean(errors.currency)} />
        </div>
        <RateRows control={form.control} register={form.register} errors={errors} rows={initial.rates} live={live} />
        <div className="flex flex-wrap items-center justify-end gap-2 border-t px-4 py-3">
          <span className="flex-1 text-xs text-muted-foreground">{t("pc.prices.form.footer")}</span>
          <Button type="submit" variant="outline" disabled={save.isPending || conflict || reloading || !dirty}>
            {save.isPending ? <Spinner /> : null}
            {t("pc.prices.form.save")}
          </Button>
        </div>
      </form>
      <section aria-labelledby="publish-title" className="flex flex-col gap-3 rounded-xl border p-4">
        <h2 id="publish-title" className="text-[15px] font-semibold">
          {t("pc.publish.panelTitle")}
        </h2>
        <PublishForm
          key={`${draft.id}:${draft.version}`}
          notBefore={notBefore}
          now={now}
          pending={publish.isPending}
          error={publish.error}
          blocked={dirty ? t("pc.publish.saveFirst") : undefined}
          onSubmit={(effectiveAt) => publish.mutate({ id: draft.id, ifMatch: etag, effective_at: effectiveAt })}
          onReload={() => {
            publish.resetOperation();
            onReload();
            refresh();
          }}
        />
      </section>
    </div>
  );
}
