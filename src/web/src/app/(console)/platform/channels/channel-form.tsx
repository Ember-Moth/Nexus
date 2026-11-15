"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { Controller, useForm, useWatch, type UseFormReturn } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { ConflictAlert, ErrorAlert, Spinner } from "@/components/feedback";
import {
  buildConfiguration,
  ConfigFields,
  describeConfigSchema,
  initialConfigValues,
  type ConfigSpec,
  type ConfigValues,
} from "@/components/platform-finance/config-form";
import { Field, ReadOnlyValue } from "@/components/platform-finance/field";
import { currencyString, nonblank, positiveMoney } from "@/components/platform-finance/validation";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import { useIdempotentMutation } from "@/lib/api/hooks";
import type { components } from "@/lib/api/schema";
import { compareMoney } from "@/lib/money";

type Channel = components["schemas"]["ChannelView"];
type ChannelCreate = components["schemas"]["ChannelCreate"];
type ChannelPatch = components["schemas"]["ChannelPatch"];
export type GatewayType = components["schemas"]["GatewayType"];

export const CHANNELS_KEY = ["payment-channels"] as const;

type ConfigMode = "form" | "json";
type ConfigFormValues = { configMode: ConfigMode; config: ConfigValues; configJson: string };
/** Both channel forms embed the configuration part; the editor only sees that part. */
type ConfigForm = UseFormReturn<ConfigFormValues>;

/** Shared configuration part of both forms: generated fields or raw JSON. */
const configShape = {
  configMode: z.enum(["form", "json"]),
  config: z.record(z.string(), z.union([z.string(), z.boolean()])),
  configJson: z.string(),
};

/** Validates the configuration part against the adapter spec and reports issues on its paths. */
function refineConfiguration(
  value: ConfigFormValues,
  spec: ConfigSpec | null,
  ctx: z.RefinementCtx,
) {
  if (value.configMode === "json" || !spec?.supported) {
    const parsed = parseJsonObject(value.configJson);
    if (!parsed) ctx.addIssue({ code: "custom", path: ["configJson"], message: "json" });
    return;
  }
  const { errors } = buildConfiguration(spec, value.config);
  for (const [name, message] of Object.entries(errors)) ctx.addIssue({ code: "custom", path: ["config", name], message });
}

function parseJsonObject(text: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(text);
    return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function configurationOf(value: ConfigFormValues, spec: ConfigSpec | null) {
  if (value.configMode === "json" || !spec?.supported) return parseJsonObject(value.configJson) ?? {};
  return buildConfiguration(spec, value.config).configuration;
}

function boundsIssue(min: string, max: string, ctx: z.RefinementCtx) {
  try {
    if (compareMoney(min.trim(), max.trim()) > 0) ctx.addIssue({ code: "custom", path: ["max_amount"], message: "minMax" });
  } catch {
    // Malformed amounts are reported by their own validators.
  }
}

/** Configuration editor: generated fields, with a switch to raw JSON. */
function ConfigEditor({
  spec,
  form,
  idPrefix,
}: {
  spec: ConfigSpec | null;
  form: ConfigForm;
  idPrefix: string;
}) {
  const { t } = useI18n();
  const mode = useWatch({ control: form.control, name: "configMode" });
  const errors = form.formState.errors;
  const generated = Boolean(spec?.supported) && mode === "form";
  return (
    <fieldset className="grid gap-3 rounded-lg border bg-muted/20 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <legend className="flex-1 text-[13px] font-medium">{t("pf.channels.configuration")}</legend>
        {spec?.supported ? (
          <ToggleGroup
            type="single"
            size="sm"
            variant="outline"
            value={mode}
            onValueChange={(value) => {
              if (!value) return;
              if (value === "json" && spec) {
                // Carry the entered values over, secrets included, so switching loses nothing.
                const { configuration } = buildConfiguration(spec, form.getValues("config"));
                form.setValue("configJson", JSON.stringify(configuration, null, 2));
              }
              form.setValue("configMode", value as ConfigMode);
            }}
            aria-label={t("pf.channels.configMode")}
          >
            <ToggleGroupItem value="form">{t("pf.channels.modeForm")}</ToggleGroupItem>
            <ToggleGroupItem value="json">{t("pf.channels.modeJson")}</ToggleGroupItem>
          </ToggleGroup>
        ) : null}
      </div>
      {spec && !spec.supported ? <p className="text-xs text-muted-foreground">{t("pf.channels.schemaUnsupported")}</p> : null}
      {generated && spec ? (
        <ConfigFields spec={spec} control={form.control} register={form.register} errors={form.formState.errors} idPrefix={idPrefix} />
      ) : (
        <Field id={`${idPrefix}-json`} label={t("pf.channels.configJson")} hint={t("pf.channels.configJsonHint")} error={errors.configJson?.message}>
          <Textarea
            id={`${idPrefix}-json`}
            rows={10}
            spellCheck={false}
            className="font-mono text-xs"
            {...form.register("configJson")}
            aria-invalid={Boolean(errors.configJson) || undefined}
          />
        </Field>
      )}
      <p className="text-xs text-muted-foreground">{t("pf.channels.secretNote")}</p>
    </fieldset>
  );
}

const createBase = z.object({
  name: nonblank(200),
  adapter_type: z.string().min(1, "required"),
  stable_merchant_id: nonblank(100),
  environment: z.enum(["test", "live"]),
  currency: currencyString(),
  min_amount: positiveMoney(),
  max_amount: positiveMoney(),
  ...configShape,
});
type CreateValues = z.input<typeof createBase>;

export function CreateChannelDialog({
  open,
  onOpenChange,
  types,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  types: GatewayType[];
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const specs = useMemo(
    () => new Map(types.map((type) => [type.adapter_type, describeConfigSchema(type.configuration_schema)] as const)),
    [types],
  );
  const schema = useMemo(
    () =>
      createBase.superRefine((value, ctx) => {
        boundsIssue(value.min_amount, value.max_amount, ctx);
        refineConfiguration(value, specs.get(value.adapter_type) ?? null, ctx);
      }),
    [specs],
  );
  const form = useForm<CreateValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      name: "",
      adapter_type: "",
      stable_merchant_id: "",
      environment: "test",
      currency: "USD",
      min_amount: "",
      max_amount: "",
      configMode: "form",
      config: {},
      configJson: "{}",
    },
  });
  const adapterType = useWatch({ control: form.control, name: "adapter_type" });
  const spec = specs.get(adapterType) ?? null;
  const selectedType = types.find((type) => type.adapter_type === adapterType);

  const create = useIdempotentMutation<Channel, ChannelCreate>({
    keyPrefix: "payment-channel",
    mutationFn: async (body, idempotencyKey) =>
      (
        await call(() =>
          nexus.POST("/api/v1/payment-channels", { params: { header: { "Idempotency-Key": idempotencyKey } }, body }),
        )
      ).data,
    onSuccess: (channel) => {
      toast.success(t("pf.channels.created", { name: channel.name }));
      void queryClient.invalidateQueries({ queryKey: CHANNELS_KEY });
      close();
    },
  });
  function close() {
    onOpenChange(false);
    form.reset();
    create.resetOperation();
  }

  const errors = form.formState.errors;
  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-[680px]">
        <form
          noValidate
          className="grid gap-4"
          onSubmit={form.handleSubmit((values) =>
            create.mutate({
              name: values.name.trim(),
              adapter_type: values.adapter_type,
              stable_merchant_id: values.stable_merchant_id.trim(),
              environment: values.environment,
              currency: values.currency.trim(),
              min_amount: values.min_amount.trim(),
              max_amount: values.max_amount.trim(),
              configuration: configurationOf(values, spec),
            }),
          )}
        >
          <DialogHeader>
            <DialogTitle>{t("pf.channels.createTitle")}</DialogTitle>
            <DialogDescription>{t("pf.channels.createDescription")}</DialogDescription>
          </DialogHeader>
          {create.error ? <ErrorAlert error={create.error} /> : null}
          <div className="grid gap-3 sm:grid-cols-2">
            <Field id="ch-name" label={t("pf.channels.name")} error={errors.name?.message}>
              <Input id="ch-name" maxLength={200} {...form.register("name")} aria-invalid={Boolean(errors.name) || undefined} />
            </Field>
            <Field id="ch-adapter" label={t("pf.channels.adapterType")} error={errors.adapter_type?.message}>
              <Controller
                control={form.control}
                name="adapter_type"
                render={({ field, fieldState }) => (
                  <Select
                    value={field.value}
                    onValueChange={(value) => {
                      field.onChange(value);
                      const next = specs.get(value);
                      const config = next ? initialConfigValues(next) : {};
                      // Adapters with their own environment field start from the channel's.
                      if (next?.fields.some((item) => item.name === "environment")) config.environment = form.getValues("environment");
                      form.setValue("config", config);
                      form.setValue("configJson", "{}");
                      form.setValue("configMode", next?.supported ? "form" : "json");
                    }}
                  >
                    <SelectTrigger id="ch-adapter" className="w-full" aria-invalid={fieldState.invalid || undefined}>
                      <SelectValue placeholder={types.length ? t("pf.channels.selectAdapter") : t("pf.channels.noAdapters")} />
                    </SelectTrigger>
                    <SelectContent>
                      {types.map((type) => (
                        <SelectItem key={type.adapter_type} value={type.adapter_type}>
                          <span className="font-mono">{type.adapter_type}</span>
                          {type.test_only ? <span className="text-xs text-amber-700">{t("pf.channels.testOnly")}</span> : null}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
            </Field>
            <Field id="ch-merchant" label={t("pf.channels.merchantId")} hint={t("pf.channels.merchantIdHint")} error={errors.stable_merchant_id?.message}>
              <Input
                id="ch-merchant"
                maxLength={100}
                autoComplete="off"
                className="font-mono"
                {...form.register("stable_merchant_id")}
                aria-invalid={Boolean(errors.stable_merchant_id) || undefined}
              />
            </Field>
            <Field id="ch-env" label={t("pf.channels.environment")} hint={selectedType?.test_only ? t("pf.channels.testOnlyHint") : undefined}>
              <Controller
                control={form.control}
                name="environment"
                render={({ field }) => (
                  <ToggleGroup
                    id="ch-env"
                    type="single"
                    variant="outline"
                    value={field.value}
                    onValueChange={(value) => value && field.onChange(value)}
                    className="grid w-full grid-cols-2"
                  >
                    <ToggleGroupItem value="test">{t("pf.channels.environments.test")}</ToggleGroupItem>
                    <ToggleGroupItem value="live">{t("pf.channels.environments.live")}</ToggleGroupItem>
                  </ToggleGroup>
                )}
              />
            </Field>
            <Field id="ch-currency" label={t("pf.common.currency")} error={errors.currency?.message}>
              <Input
                id="ch-currency"
                maxLength={3}
                className="font-mono uppercase"
                {...form.register("currency", { setValueAs: (value: string) => value.toUpperCase() })}
                aria-invalid={Boolean(errors.currency) || undefined}
              />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field id="ch-min" label={t("pf.channels.minAmount")} error={errors.min_amount?.message}>
                <Input id="ch-min" inputMode="decimal" className="tabular-nums" {...form.register("min_amount")} aria-invalid={Boolean(errors.min_amount) || undefined} />
              </Field>
              <Field id="ch-max" label={t("pf.channels.maxAmount")} error={errors.max_amount?.message}>
                <Input id="ch-max" inputMode="decimal" className="tabular-nums" {...form.register("max_amount")} aria-invalid={Boolean(errors.max_amount) || undefined} />
              </Field>
            </div>
          </div>
          {adapterType ? <ConfigEditor spec={spec} form={form as unknown as ConfigForm} idPrefix="ch-config" /> : null}
          <p className="text-xs text-muted-foreground">{t("pf.channels.immutableNote")}</p>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={close}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? <Spinner /> : null}
              {t("common.create")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

const editBase = z.object({
  name: nonblank(200),
  status: z.enum(["active", "disabled"]),
  min_amount: positiveMoney(),
  max_amount: positiveMoney(),
  replaceConfig: z.boolean(),
  verification_grace_seconds: z.string(),
  ...configShape,
});
type EditValues = z.input<typeof editBase>;

/** GET /payment-channels/{id} with its ETag. */
export function useChannelDetail(channelId: string, enabled = true) {
  return useQuery({
    queryKey: [...CHANNELS_KEY, "detail", channelId],
    queryFn: async () => call(() => nexus.GET("/api/v1/payment-channels/{channel_id}", { params: { path: { channel_id: channelId } } })),
    enabled,
    staleTime: 0,
  });
}

/** Non-secret values of the active configuration, to prefill a replacement. */
function useActivePublicConfiguration(channel: Channel | undefined) {
  return useQuery({
    queryKey: [...CHANNELS_KEY, "versions", channel?.id, "active", channel?.active_config_id],
    queryFn: async () => {
      // Versions are listed newest first; the active one is normally on the first page.
      const page = (
        await call(() =>
          nexus.GET("/api/v1/payment-channels/{channel_id}/versions", {
            params: { path: { channel_id: channel!.id }, query: { limit: 100 } },
          }),
        )
      ).data;
      return page.items.find((version) => version.id === channel!.active_config_id)?.public_configuration ?? null;
    },
    enabled: Boolean(channel),
  });
}

function EditChannelForm({
  channel,
  ifMatch,
  spec,
  publicConfiguration,
  onReload,
  onClose,
}: {
  channel: Channel;
  ifMatch: string;
  spec: ConfigSpec | null;
  publicConfiguration: Record<string, unknown> | null;
  onReload: () => void;
  onClose: () => void;
}) {
  const { t, maybeT } = useI18n();
  const queryClient = useQueryClient();
  const schema = useMemo(
    () =>
      editBase.superRefine((value, ctx) => {
        boundsIssue(value.min_amount, value.max_amount, ctx);
        if (value.replaceConfig) {
          refineConfiguration(value, spec, ctx);
          const grace = value.verification_grace_seconds.trim();
          if (!/^\d+$/.test(grace) || Number(grace) > 86400)
            ctx.addIssue({ code: "custom", path: ["verification_grace_seconds"], message: "grace" });
        }
      }),
    [spec],
  );
  const form = useForm<EditValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      name: channel.name,
      status: channel.status === "disabled" ? "disabled" : "active",
      min_amount: channel.min_amount,
      max_amount: channel.max_amount,
      replaceConfig: false,
      verification_grace_seconds: "3600",
      configMode: spec?.supported ? "form" : "json",
      config: spec ? initialConfigValues(spec, publicConfiguration ?? undefined) : {},
      configJson: "{}",
    },
  });
  const replaceConfig = useWatch({ control: form.control, name: "replaceConfig" });

  // PATCH requires If-Match and Idempotency-Key; the version is part of the operation.
  const update = useIdempotentMutation<Channel, { ifMatch: string; body: ChannelPatch }>({
    keyPrefix: "update-channel",
    mutationFn: async ({ ifMatch: version, body }, idempotencyKey) =>
      (
        await call(() =>
          nexus.PATCH("/api/v1/payment-channels/{channel_id}", {
            params: {
              path: { channel_id: channel.id },
              header: { "If-Match": version, "Idempotency-Key": idempotencyKey },
            },
            body,
          }),
        )
      ).data,
    onSuccess: (updated) => {
      toast.success(t("pf.channels.saved", { name: updated.name }));
      void queryClient.invalidateQueries({ queryKey: CHANNELS_KEY });
      onClose();
    },
  });

  const errors = form.formState.errors;
  const conflict = isConflict(update.error);
  return (
    <form
      noValidate
      className="grid gap-4"
      onSubmit={form.handleSubmit((values) => {
        // The generated type marks verification_grace_seconds as required (it has a default),
        // but the API rejects it unless the configuration is replaced, so it is optional here.
        const body: Omit<ChannelPatch, "verification_grace_seconds"> & { verification_grace_seconds?: number } = {};
        if (values.name.trim() !== channel.name) body.name = values.name.trim();
        if (values.status !== channel.status) body.status = values.status;
        if (values.min_amount.trim() !== channel.min_amount) body.min_amount = values.min_amount.trim();
        if (values.max_amount.trim() !== channel.max_amount) body.max_amount = values.max_amount.trim();
        if (values.replaceConfig) {
          // Full replacement: secrets must be supplied again; omitted configuration keeps the old one.
          body.configuration = configurationOf(values, spec);
          body.verification_grace_seconds = Number(values.verification_grace_seconds.trim());
        }
        if (Object.keys(body).length === 0) return onClose();
        update.mutate({ ifMatch, body: body as ChannelPatch });
      })}
    >
      {conflict ? (
        <ConflictAlert
          onReload={() => {
            update.resetOperation();
            onReload();
          }}
        />
      ) : update.error ? (
        <ErrorAlert error={update.error} />
      ) : null}
      <div className="grid gap-3 sm:grid-cols-4">
        <ReadOnlyValue label={t("pf.channels.adapterType")} mono>
          {channel.adapter_type}
        </ReadOnlyValue>
        <ReadOnlyValue label={t("pf.channels.merchantId")} mono>
          {channel.stable_merchant_id}
        </ReadOnlyValue>
        <ReadOnlyValue label={t("pf.channels.environment")}>
          {maybeT(`pf.channels.environments.${channel.environment}`) ?? channel.environment}
        </ReadOnlyValue>
        <ReadOnlyValue label={t("pf.common.currency")} mono>
          {channel.currency}
        </ReadOnlyValue>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field id="ce-name" label={t("pf.channels.name")} error={errors.name?.message}>
          <Input id="ce-name" maxLength={200} {...form.register("name")} aria-invalid={Boolean(errors.name) || undefined} />
        </Field>
        <Field id="ce-status" label={t("pf.common.status")}>
          <Controller
            control={form.control}
            name="status"
            render={({ field }) => (
              <ToggleGroup
                id="ce-status"
                type="single"
                variant="outline"
                value={field.value}
                onValueChange={(value) => value && field.onChange(value)}
                className="grid w-full grid-cols-2"
              >
                <ToggleGroupItem value="active">{t("pf.common.states.active")}</ToggleGroupItem>
                <ToggleGroupItem value="disabled">{t("pf.common.states.disabled")}</ToggleGroupItem>
              </ToggleGroup>
            )}
          />
        </Field>
        <Field id="ce-min" label={t("pf.channels.minAmount")} error={errors.min_amount?.message}>
          <Input id="ce-min" inputMode="decimal" className="tabular-nums" {...form.register("min_amount")} aria-invalid={Boolean(errors.min_amount) || undefined} />
        </Field>
        <Field id="ce-max" label={t("pf.channels.maxAmount")} error={errors.max_amount?.message}>
          <Input id="ce-max" inputMode="decimal" className="tabular-nums" {...form.register("max_amount")} aria-invalid={Boolean(errors.max_amount) || undefined} />
        </Field>
      </div>
      <div className="flex items-start gap-3 rounded-lg border px-3 py-2.5">
        <Controller
          control={form.control}
          name="replaceConfig"
          render={({ field }) => <Switch id="ce-replace" checked={field.value} onCheckedChange={field.onChange} className="mt-0.5" />}
        />
        <Label htmlFor="ce-replace" className="flex flex-col items-start gap-0.5 font-normal">
          <span className="font-medium">{t("pf.channels.replaceConfig")}</span>
          <span className="text-xs text-muted-foreground">{t("pf.channels.replaceConfigHint")}</span>
        </Label>
      </div>
      {replaceConfig ? (
        <>
          <ConfigEditor spec={spec} form={form as unknown as ConfigForm} idPrefix="ce-config" />
          <Field
            id="ce-grace"
            label={t("pf.channels.graceSeconds")}
            hint={t("pf.channels.graceHint")}
            error={errors.verification_grace_seconds?.message}
          >
            <Input
              id="ce-grace"
              inputMode="numeric"
              className="w-40 tabular-nums"
              {...form.register("verification_grace_seconds")}
              aria-invalid={Boolean(errors.verification_grace_seconds) || undefined}
            />
          </Field>
        </>
      ) : null}
      <DialogFooter>
        <span className="mr-auto self-center text-xs text-muted-foreground">
          {t("pf.channels.versionNote", { version: channel.version })}
        </span>
        <Button type="button" variant="outline" onClick={onClose}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" disabled={update.isPending || conflict}>
          {update.isPending ? <Spinner /> : null}
          {t("common.save")}
        </Button>
      </DialogFooter>
    </form>
  );
}

export function EditChannelDialog({
  channelId,
  types,
  onClose,
}: {
  channelId: string;
  types: GatewayType[];
  onClose: () => void;
}) {
  const { t } = useI18n();
  const detail = useChannelDetail(channelId);
  const channel = detail.data?.data;
  const active = useActivePublicConfiguration(channel);
  const type = types.find((item) => item.adapter_type === channel?.adapter_type);
  const spec = useMemo(() => (type ? describeConfigSchema(type.configuration_schema) : null), [type]);
  const ready = channel && !active.isPending;
  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-[680px]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {t("pf.channels.editTitle")}
            {channel?.environment === "test" ? <StatusBadge tone="warning">{t("pf.channels.environments.test")}</StatusBadge> : null}
          </DialogTitle>
          <DialogDescription>{t("pf.channels.editDescription")}</DialogDescription>
        </DialogHeader>
        {detail.error ? <ErrorAlert error={detail.error} onRetry={() => void detail.refetch()} /> : null}
        {ready ? (
          <EditChannelForm
            key={channel.version}
            channel={channel}
            ifMatch={detail.data?.etag ?? `"${channel.version}"`}
            spec={spec}
            publicConfiguration={active.data ?? null}
            onReload={() => void detail.refetch()}
            onClose={onClose}
          />
        ) : detail.error ? null : (
          <Skeleton className="h-64 w-full" />
        )}
      </DialogContent>
    </Dialog>
  );
}
