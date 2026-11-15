"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Controller, useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { ConflictAlert, Spinner } from "@/components/feedback";
import { PcErrorAlert } from "@/components/platform-catalog/errors";
import { CheckboxGroup, Field, SwitchRow } from "@/components/platform-catalog/form";
import {
  inEnumOrder,
  PROTOCOLS,
  REQUIRED_TOKEN_DIMENSIONS,
  STATUSES,
  TOKEN_DIMENSIONS,
  TOOL_DIMENSIONS,
} from "@/components/platform-catalog/labels";
import { etagOf, invalidateCatalog, useModelDetail } from "@/components/platform-catalog/queries";
import type {
  CatalogModel,
  CatalogStatus,
  ModelCreate,
  ModelPatch,
  ProtocolFamily,
  TokenDimension,
  ToolDimension,
} from "@/components/platform-catalog/types";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";

const limit = z
  .string()
  .trim()
  .regex(/^\d{1,10}$/)
  .refine((value) => Number(value) >= 1 && Number(value) <= 1_000_000_000);

const schema = z.object({
  publicName: z.string().trim().regex(/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/),
  displayName: z.string().trim().min(1).max(200),
  protocols: z.array(z.enum(PROTOCOLS as [ProtocolFamily, ...ProtocolFamily[]])).min(1),
  tokenDimensions: z.array(z.enum(TOKEN_DIMENSIONS as [TokenDimension, ...TokenDimension[]])),
  toolDimensions: z.array(z.enum(TOOL_DIMENSIONS as [ToolDimension, ...ToolDimension[]])),
  streaming: z.boolean(),
  tools: z.boolean(),
  maxInput: limit,
  maxOutput: limit,
  status: z.enum(STATUSES as [CatalogStatus, ...CatalogStatus[]]),
});
type Values = z.infer<typeof schema>;

function defaults(model: CatalogModel | null): Values {
  return {
    publicName: model?.public_name ?? "",
    displayName: model?.display_name ?? "",
    protocols: model ? inEnumOrder(model.protocols, PROTOCOLS) : [],
    tokenDimensions: model?.token_dimensions
      ? inEnumOrder(model.token_dimensions, TOKEN_DIMENSIONS)
      : [...REQUIRED_TOKEN_DIMENSIONS],
    toolDimensions: model?.tool_dimensions ? inEnumOrder(model.tool_dimensions, TOOL_DIMENSIONS) : [],
    streaming: model?.capabilities?.streaming ?? false,
    tools: model?.capabilities?.tools ?? false,
    maxInput: model ? String(model.max_input_tokens) : "",
    maxOutput: model ? String(model.max_output_tokens) : "",
    status: model?.status ?? "active",
  };
}

const withRequired = (dimensions: TokenDimension[]) =>
  TOKEN_DIMENSIONS.filter((d) => dimensions.includes(d) || REQUIRED_TOKEN_DIMENSIONS.includes(d));

const sameSet = (a: readonly string[], b: readonly string[]) => a.length === b.length && a.every((v) => b.includes(v));

/** Only changed fields are sent; archiving is a standalone status change. */
function patchFor(model: CatalogModel, values: Values): ModelPatch {
  if (values.status === "archived" && model.status !== "archived") return { status: "archived" };
  const patch: ModelPatch = {};
  const display = values.displayName.trim();
  if (display !== model.display_name) patch.display_name = display;
  if (!sameSet(values.protocols, model.protocols)) patch.protocols = values.protocols;
  const tokens = withRequired(values.tokenDimensions);
  if (!sameSet(tokens, model.token_dimensions ?? [])) patch.token_dimensions = tokens;
  if (!sameSet(values.toolDimensions, model.tool_dimensions ?? [])) patch.tool_dimensions = values.toolDimensions;
  if (values.streaming !== (model.capabilities?.streaming ?? false) || values.tools !== (model.capabilities?.tools ?? false)) {
    patch.capabilities = { streaming: values.streaming, tools: values.tools };
  }
  if (Number(values.maxInput) !== model.max_input_tokens) patch.max_input_tokens = Number(values.maxInput);
  if (Number(values.maxOutput) !== model.max_output_tokens) patch.max_output_tokens = Number(values.maxOutput);
  if (values.status !== model.status) patch.status = values.status;
  return patch;
}

export function ModelDialog({ model, onClose }: { model: CatalogModel | null; onClose: () => void }) {
  const t = useT();
  const detail = useModelDetail(model?.id);
  const current = detail.data?.data;

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-h-[92vh] overflow-y-auto sm:max-w-[680px]">
        <DialogHeader>
          <DialogTitle>
            {model ? t("pc.models.form.editTitle", { name: model.public_name }) : t("pc.models.form.createTitle")}
          </DialogTitle>
          <DialogDescription>{t("pc.models.form.description")}</DialogDescription>
        </DialogHeader>
        {model && detail.error ? <PcErrorAlert error={detail.error} onRetry={() => void detail.refetch()} /> : null}
        {!model ? (
          <ModelForm model={null} etag={null} onClose={onClose} />
        ) : current ? (
          <ModelForm
            key={current.version}
            model={current}
            etag={etagOf(detail.data, current.version)}
            reloading={detail.isFetching}
            onReload={() => void detail.refetch()}
            onClose={onClose}
          />
        ) : detail.isPending ? (
          <div className="flex justify-center py-8">
            <Spinner />
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function ModelForm({
  model,
  etag,
  reloading,
  onReload,
  onClose,
}: {
  model: CatalogModel | null;
  etag: string | null;
  reloading?: boolean;
  onReload?: () => void;
  onClose: () => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: defaults(model) });
  const errors = form.formState.errors;
  const status = useWatch({ control: form.control, name: "status" });
  const archiving = Boolean(model) && status === "archived" && model?.status !== "archived";
  const archived = model?.status === "archived";

  const save = useMutation({
    mutationFn: async (values: Values) => {
      if (!model) {
        const body: ModelCreate = {
          public_name: values.publicName.trim(),
          display_name: values.displayName.trim(),
          protocols: values.protocols,
          token_dimensions: withRequired(values.tokenDimensions),
          tool_dimensions: values.toolDimensions,
          capabilities: { streaming: values.streaming, tools: values.tools },
          max_input_tokens: Number(values.maxInput),
          max_output_tokens: Number(values.maxOutput),
          usage_schema: "tokens.v1",
        };
        return (await call(() => nexus.POST("/api/v1/models", { body }))).data;
      }
      return (
        await call(() =>
          nexus.PATCH("/api/v1/models/{model_id}", {
            params: { path: { model_id: model.id }, header: { "If-Match": etag ?? `"${model.version}"` } },
            body: patchFor(model, values),
          }),
        )
      ).data;
    },
    onSuccess: () => {
      toast.success(model ? t("pc.models.form.saved") : t("pc.models.form.created"));
      void invalidateCatalog(queryClient);
      onClose();
    },
  });

  const conflict = isConflict(save.error);
  const submit = form.handleSubmit((values) => {
    if (model && Object.keys(patchFor(model, values)).length === 0) {
      toast.info(t("pc.common.noChanges"));
      return;
    }
    save.mutate(values);
  });
  const limitError = t("pc.models.form.limitInvalid");

  return (
    <form noValidate onSubmit={submit} className="grid gap-5">
      {conflict ? (
        <ConflictAlert
          onReload={() => {
            save.reset();
            onReload?.();
          }}
        />
      ) : save.error ? (
        <PcErrorAlert error={save.error} />
      ) : null}
      {archived ? (
        <p className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">{t("pc.errorCodes.resource_archived")}</p>
      ) : null}
      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          id="model-public-name"
          label={t("pc.models.form.publicName")}
          hint={t("pc.models.form.publicNameHint")}
          error={errors.publicName ? t("pc.models.form.publicNameInvalid") : undefined}
        >
          <Input
            id="model-public-name"
            autoComplete="off"
            maxLength={200}
            readOnly={Boolean(model)}
            className={model ? "bg-muted font-mono text-muted-foreground" : "font-mono"}
            aria-invalid={Boolean(errors.publicName) || undefined}
            {...form.register("publicName")}
          />
        </Field>
        <Field
          id="model-display-name"
          label={t("pc.models.form.displayName")}
          error={errors.displayName ? t("pc.common.required") : undefined}
        >
          <Input
            id="model-display-name"
            maxLength={200}
            aria-invalid={Boolean(errors.displayName) || undefined}
            {...form.register("displayName")}
          />
        </Field>
      </div>
      <Field
        label={t("pc.models.form.protocols")}
        hint={t("pc.models.form.protocolsHint")}
        error={errors.protocols ? t("pc.models.form.protocolsRequired") : undefined}
      >
        <Controller
          control={form.control}
          name="protocols"
          render={({ field, fieldState }) => (
            <CheckboxGroup
              idPrefix="model-protocol"
              options={PROTOCOLS}
              value={field.value}
              onChange={field.onChange}
              label={(p) => t(`pc.enums.protocol.${p}`)}
              invalid={fieldState.invalid}
              columns={2}
            />
          )}
        />
      </Field>
      <Field label={t("pc.models.form.tokenDimensions")} hint={t("pc.models.form.tokenDimensionsHint")}>
        <Controller
          control={form.control}
          name="tokenDimensions"
          render={({ field }) => (
            <CheckboxGroup
              idPrefix="model-token-dim"
              options={TOKEN_DIMENSIONS}
              value={field.value}
              onChange={field.onChange}
              locked={REQUIRED_TOKEN_DIMENSIONS}
              label={(d) => (
                <span className="flex flex-col">
                  <span>{t(`pc.enums.tokenDimension.${d}`)}</span>
                  <code className="font-mono text-[11px] text-muted-foreground">{d}</code>
                </span>
              )}
            />
          )}
        />
      </Field>
      <Field label={t("pc.models.form.toolDimensions")} hint={t("pc.models.form.toolDimensionsHint")}>
        <Controller
          control={form.control}
          name="toolDimensions"
          render={({ field }) => (
            <CheckboxGroup
              idPrefix="model-tool-dim"
              options={TOOL_DIMENSIONS}
              value={field.value}
              onChange={field.onChange}
              label={(d) => (
                <span className="flex flex-col">
                  <span>{t(`pc.enums.toolDimension.${d}`)}</span>
                  <code className="font-mono text-[11px] text-muted-foreground">{d}</code>
                </span>
              )}
            />
          )}
        />
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        <Controller
          control={form.control}
          name="streaming"
          render={({ field }) => (
            <SwitchRow id="model-streaming" label={t("pc.models.form.streaming")} checked={field.value} onChange={field.onChange} />
          )}
        />
        <Controller
          control={form.control}
          name="tools"
          render={({ field }) => (
            <SwitchRow id="model-tools" label={t("pc.models.form.tools")} checked={field.value} onChange={field.onChange} />
          )}
        />
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field id="model-max-input" label={t("pc.models.form.maxInput")} error={errors.maxInput ? limitError : undefined}>
          <Input
            id="model-max-input"
            inputMode="numeric"
            className="tabular-nums"
            aria-invalid={Boolean(errors.maxInput) || undefined}
            {...form.register("maxInput")}
          />
        </Field>
        <Field id="model-max-output" label={t("pc.models.form.maxOutput")} error={errors.maxOutput ? limitError : undefined}>
          <Input
            id="model-max-output"
            inputMode="numeric"
            className="tabular-nums"
            aria-invalid={Boolean(errors.maxOutput) || undefined}
            {...form.register("maxOutput")}
          />
        </Field>
      </div>
      {model ? (
        <Field id="model-status" label={t("pc.models.form.status")}>
          <Controller
            control={form.control}
            name="status"
            render={({ field }) => (
              <Select value={field.value} onValueChange={field.onChange}>
                <SelectTrigger id="model-status" className="w-full sm:w-60">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {STATUSES.map((s) => (
                    <SelectItem key={s} value={s}>
                      {t(`pc.enums.status.${s}`)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          />
          {archiving ? (
            <p role="alert" className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
              {t("pc.common.archiveWarning")}
            </p>
          ) : null}
        </Field>
      ) : null}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" disabled={save.isPending || conflict || reloading || archived}>
          {save.isPending ? <Spinner /> : null}
          {model ? t("common.save") : t("common.create")}
        </Button>
      </DialogFooter>
    </form>
  );
}
