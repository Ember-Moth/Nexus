"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { ConflictAlert, Spinner } from "@/components/feedback";
import { PcErrorAlert } from "@/components/platform-catalog/errors";
import { Field, MoreOptions, SwitchRow } from "@/components/platform-catalog/form";
import { STATUSES } from "@/components/platform-catalog/labels";
import {
  etagOf,
  invalidateCatalog,
  useCredentials,
  useDeploymentDetail,
  useModels,
  useUpstreams,
} from "@/components/platform-catalog/queries";
import {
  extraFields,
  fromDraft,
  suggestedProfile,
  toDraft,
  type RelayDraft,
} from "@/components/platform-catalog/relay-profile";
import { RelayProfileEditor } from "@/components/platform-catalog/relay-profile-editor";
import type {
  CatalogDeployment,
  CatalogStatus,
  DeploymentCreate,
  DeploymentPatch,
  RelayProfile,
} from "@/components/platform-catalog/types";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import { shortId } from "@/lib/format";

const draftValue = z.union([z.string(), z.boolean(), z.array(z.string())]);

const schema = z
  .object({
    name: z.string().trim().min(1).max(200),
    modelId: z.string().min(1),
    upstreamId: z.string().min(1),
    credentialId: z.string().min(1),
    upstreamModel: z.string().trim().regex(/^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$/),
    region: z.string().trim().min(1).max(100),
    status: z.enum(STATUSES as [CatalogStatus, ...CatalogStatus[]]),
    relayEnabled: z.boolean(),
    relay: z.record(z.string(), draftValue),
  })
  .superRefine((value, ctx) => {
    // Per-field messages are rendered by the editor; this only blocks submission.
    if (value.relayEnabled && Object.keys(fromDraft(value.relay).problems).length > 0) {
      ctx.addIssue({ code: "custom", path: ["relay"], message: "relay" });
    }
  });
type Values = z.infer<typeof schema>;

/** Comparable form of a profile: sorted keys, without nulls and empty objects/arrays. */
function canonical(value: unknown): string {
  const clean = (input: unknown): unknown => {
    if (Array.isArray(input)) return input.map(clean);
    if (input && typeof input === "object") {
      const entries = Object.entries(input as Record<string, unknown>)
        .filter(([, v]) => v !== null && v !== undefined)
        .filter(([, v]) => !(Array.isArray(v) && v.length === 0))
        .filter(([, v]) => !(typeof v === "object" && v !== null && !Array.isArray(v) && Object.keys(v).length === 0))
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([k, v]) => [k, clean(v)]);
      return Object.fromEntries(entries);
    }
    return input;
  };
  return JSON.stringify(clean(value ?? null));
}

function profileOf(values: Values, extra: Record<string, unknown>): RelayProfile | null {
  return values.relayEnabled ? fromDraft(values.relay, extra).profile : null;
}

/** Only changed fields; archiving is standalone; a new upstream model re-submits the profile. */
function patchFor(deployment: CatalogDeployment, values: Values, extra: Record<string, unknown>): DeploymentPatch {
  if (values.status === "archived" && deployment.status !== "archived") return { status: "archived" };
  const patch: DeploymentPatch = {};
  if (values.name.trim() !== deployment.name) patch.name = values.name.trim();
  if (values.credentialId !== deployment.credential_id) patch.credential_id = values.credentialId;
  if (values.upstreamModel.trim() !== deployment.upstream_model) patch.upstream_model = values.upstreamModel.trim();
  if (values.region.trim() !== deployment.region) patch.region = values.region.trim();
  if (values.status !== deployment.status) patch.status = values.status;
  const profile = profileOf(values, extra);
  if (canonical(profile) !== canonical(deployment.relay_profile ?? null) || (patch.upstream_model && profile)) {
    patch.relay_profile = profile;
  }
  return patch;
}

function defaults(deployment: CatalogDeployment | null, filters: { modelId?: string; upstreamId?: string }): Values {
  return {
    name: deployment?.name ?? "",
    modelId: deployment?.model_id ?? filters.modelId ?? "",
    upstreamId: deployment?.upstream_id ?? filters.upstreamId ?? "",
    credentialId: deployment?.credential_id ?? "",
    upstreamModel: deployment?.upstream_model ?? "",
    region: deployment?.region ?? "global",
    status: deployment?.status ?? "active",
    relayEnabled: Boolean(deployment?.relay_profile),
    relay: toDraft(deployment?.relay_profile),
  };
}

export function DeploymentDialog({
  deployment,
  filters,
  onClose,
}: {
  deployment: CatalogDeployment | null;
  filters: { modelId?: string; upstreamId?: string };
  onClose: () => void;
}) {
  const t = useT();
  const detail = useDeploymentDetail(deployment?.id);
  const current = detail.data?.data;
  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-h-[92vh] overflow-y-auto sm:max-w-[820px]">
        <DialogHeader>
          <DialogTitle>
            {deployment ? t("pc.deployments.form.editTitle", { name: deployment.name }) : t("pc.deployments.form.createTitle")}
          </DialogTitle>
          <DialogDescription>{t("pc.deployments.form.description")}</DialogDescription>
        </DialogHeader>
        {deployment && detail.error ? <PcErrorAlert error={detail.error} onRetry={() => void detail.refetch()} /> : null}
        {!deployment ? (
          <DeploymentForm deployment={null} etag={null} filters={filters} onClose={onClose} />
        ) : current ? (
          <DeploymentForm
            key={current.version}
            deployment={current}
            etag={etagOf(detail.data, current.version)}
            filters={filters}
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

function DeploymentForm({
  deployment,
  etag,
  filters,
  reloading,
  onReload,
  onClose,
}: {
  deployment: CatalogDeployment | null;
  etag: string | null;
  filters: { modelId?: string; upstreamId?: string };
  reloading?: boolean;
  onReload?: () => void;
  onClose: () => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const models = useModels();
  const upstreams = useUpstreams();
  const extra = useMemo(() => extraFields(deployment?.relay_profile), [deployment]);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: defaults(deployment, filters) });
  const errors = form.formState.errors;
  const [modelId, upstreamId, status, relayEnabled, region] = useWatch({
    control: form.control,
    name: ["modelId", "upstreamId", "status", "relayEnabled", "region"],
  });
  const credentials = useCredentials(upstreamId || undefined);
  const model = models.items.find((m) => m.id === modelId);
  const upstream = upstreams.items.find((u) => u.id === upstreamId);
  const protocolMismatch = Boolean(model && upstream && !model.protocols.includes(upstream.protocol_family));
  const usableCredentials = credentials.items.filter(
    (c) => c.status === "active" || c.id === deployment?.credential_id,
  );
  const archived = deployment?.status === "archived";
  const archiving = Boolean(deployment) && status === "archived" && !archived;
  const statusOptions = deployment ? STATUSES : STATUSES.filter((s) => s !== "archived");
  const relayContext = useMemo(() => ({ model, upstream, region }), [model, upstream, region]);

  const save = useMutation({
    mutationFn: async (values: Values) => {
      if (!deployment) {
        const body: DeploymentCreate = {
          name: values.name.trim(),
          model_id: values.modelId,
          upstream_id: values.upstreamId,
          credential_id: values.credentialId,
          upstream_model: values.upstreamModel.trim(),
          region: values.region.trim(),
          status: values.status === "disabled" ? "disabled" : "active",
          relay_profile: profileOf(values, {}),
        };
        return (await call(() => nexus.POST("/api/v1/deployments", { body }))).data;
      }
      return (
        await call(() =>
          nexus.PATCH("/api/v1/deployments/{deployment_id}", {
            params: {
              path: { deployment_id: deployment.id },
              header: { "If-Match": etag ?? `"${deployment.version}"` },
            },
            body: patchFor(deployment, values, extra),
          }),
        )
      ).data;
    },
    onSuccess: () => {
      toast.success(deployment ? t("pc.deployments.form.saved") : t("pc.deployments.form.created"));
      void invalidateCatalog(queryClient);
      onClose();
    },
  });

  const conflict = isConflict(save.error);
  const submit = form.handleSubmit((values) => {
    if (deployment && Object.keys(patchFor(deployment, values, extra)).length === 0) {
      toast.info(t("pc.common.noChanges"));
      return;
    }
    save.mutate(values);
  });

  const suggest = (draft: RelayDraft): RelayDraft => {
    const suggestion = suggestedProfile(upstream, model);
    const filled = toDraft(suggestion);
    const next = { ...draft };
    for (const [key, value] of Object.entries(suggestion)) if (value !== undefined) next[key] = filled[key];
    return next;
  };

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
        <Field id="deployment-name" label={t("pc.deployments.form.name")} error={errors.name ? t("pc.common.required") : undefined}>
          <Input id="deployment-name" maxLength={200} aria-invalid={Boolean(errors.name) || undefined} {...form.register("name")} />
        </Field>
        <Field id="deployment-status" label={t("pc.deployments.form.status")}>
          <Controller
            control={form.control}
            name="status"
            render={({ field }) => (
              <Select value={field.value} onValueChange={field.onChange}>
                <SelectTrigger id="deployment-status" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {statusOptions.map((s) => (
                    <SelectItem key={s} value={s}>
                      {t(`pc.enums.status.${s}`)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          />
        </Field>
        <Field
          id="deployment-model"
          label={t("pc.deployments.form.model")}
          hint={deployment ? t("pc.deployments.form.ownershipLocked") : undefined}
          error={errors.modelId ? t("pc.common.required") : undefined}
        >
          <Controller
            control={form.control}
            name="modelId"
            render={({ field, fieldState }) => (
              <Select value={field.value} onValueChange={field.onChange} disabled={Boolean(deployment)}>
                <SelectTrigger id="deployment-model" className="w-full" aria-invalid={fieldState.invalid || undefined}>
                  <SelectValue placeholder={models.isPending ? t("common.loading") : t("pc.common.select")}>
                    {field.value ? (model?.public_name ?? shortId(field.value)) : undefined}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {models.items
                    .filter((m) => m.status !== "archived" || m.id === field.value)
                    .map((m) => (
                      <SelectItem key={m.id} value={m.id}>
                        <span className="font-mono">{m.public_name}</span>
                        {m.status !== "active" ? <span className="text-muted-foreground">· {t(`pc.enums.status.${m.status}`)}</span> : null}
                      </SelectItem>
                    ))}
                </SelectContent>
              </Select>
            )}
          />
          {!deployment ? <MoreOptions query={models} /> : null}
        </Field>
        <Field
          id="deployment-upstream"
          label={t("pc.deployments.form.upstream")}
          hint={deployment ? t("pc.deployments.form.ownershipLocked") : undefined}
          error={errors.upstreamId ? t("pc.common.required") : undefined}
        >
          <Controller
            control={form.control}
            name="upstreamId"
            render={({ field, fieldState }) => (
              <Select
                value={field.value}
                onValueChange={(value) => {
                  if (value !== field.value) form.setValue("credentialId", "");
                  field.onChange(value);
                }}
                disabled={Boolean(deployment)}
              >
                <SelectTrigger id="deployment-upstream" className="w-full" aria-invalid={fieldState.invalid || undefined}>
                  <SelectValue placeholder={upstreams.isPending ? t("common.loading") : t("pc.common.select")}>
                    {field.value ? (upstream?.name ?? shortId(field.value)) : undefined}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {upstreams.items
                    .filter((u) => u.status !== "archived" || u.id === field.value)
                    .map((u) => (
                      <SelectItem key={u.id} value={u.id}>
                        {u.name}
                        <span className="text-muted-foreground">· {t(`pc.enums.protocol.${u.protocol_family}`)}</span>
                      </SelectItem>
                    ))}
                </SelectContent>
              </Select>
            )}
          />
          {!deployment ? <MoreOptions query={upstreams} /> : null}
        </Field>
      </div>
      {protocolMismatch && upstream ? (
        <p role="alert" className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          {t("pc.deployments.form.protocolMismatch", { protocol: t(`pc.enums.protocol.${upstream.protocol_family}`) })}
        </p>
      ) : null}
      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          id="deployment-credential"
          label={t("pc.deployments.form.credential")}
          hint={
            upstreamId && credentials.items.length > 0 && usableCredentials.length === 0
              ? t("pc.deployments.form.noCredentials")
              : t("pc.deployments.form.credentialHint")
          }
          error={errors.credentialId ? t("pc.common.required") : undefined}
          className="sm:col-span-2"
        >
          <Controller
            control={form.control}
            name="credentialId"
            render={({ field, fieldState }) => (
              <Select value={field.value} onValueChange={field.onChange} disabled={!upstreamId}>
                <SelectTrigger id="deployment-credential" className="w-full" aria-invalid={fieldState.invalid || undefined}>
                  <SelectValue
                    placeholder={
                      !upstreamId
                        ? t("pc.deployments.form.selectUpstreamFirst")
                        : credentials.isPending
                          ? t("common.loading")
                          : usableCredentials.length
                            ? t("pc.common.select")
                            : t("pc.deployments.form.noCredentials")
                    }
                  >
                    {field.value
                      ? (credentials.items.find((c) => c.id === field.value)?.name ?? shortId(field.value))
                      : undefined}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {usableCredentials.map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.name}
                      <span className="text-muted-foreground">
                        · v{c.key_version}
                        {c.status !== "active" ? ` · ${t(`pc.credentials.status.${c.status}`)}` : ""}
                      </span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          />
          {upstreamId ? <MoreOptions query={credentials} /> : null}
        </Field>
        <Field
          id="deployment-upstream-model"
          label={t("pc.deployments.form.upstreamModel")}
          hint={t("pc.deployments.form.upstreamModelHint")}
          error={errors.upstreamModel ? t("pc.deployments.form.upstreamModelInvalid") : undefined}
        >
          <Input
            id="deployment-upstream-model"
            className="font-mono"
            autoComplete="off"
            spellCheck={false}
            maxLength={200}
            aria-invalid={Boolean(errors.upstreamModel) || undefined}
            {...form.register("upstreamModel")}
          />
        </Field>
        <Field
          id="deployment-region"
          label={t("pc.deployments.form.region")}
          hint={t("pc.deployments.form.regionHint")}
          error={errors.region ? t("pc.common.required") : undefined}
        >
          <Input
            id="deployment-region"
            className="font-mono"
            autoComplete="off"
            maxLength={100}
            aria-invalid={Boolean(errors.region) || undefined}
            {...form.register("region")}
          />
        </Field>
      </div>
      {archiving ? (
        <p role="alert" className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          {t("pc.common.archiveWarning")}
        </p>
      ) : deployment && status === "disabled" && deployment.status !== "disabled" ? (
        <p className="text-xs text-muted-foreground">{t("pc.deployments.form.disableOnly")}</p>
      ) : null}
      <div className="grid gap-4 border-t pt-5">
        <Controller
          control={form.control}
          name="relayEnabled"
          render={({ field }) => (
            <SwitchRow
              id="deployment-relay-enabled"
              label={t("pc.deployments.form.relayEnabled")}
              hint={t("pc.deployments.form.relayEnabledHint")}
              checked={field.value}
              onChange={(next) => {
                // A fresh profile starts from values that fit the selected upstream and model.
                if (next && !deployment?.relay_profile && !form.getValues("relay.endpoint")) {
                  form.setValue("relay", suggest(form.getValues("relay")));
                }
                field.onChange(next);
              }}
            />
          )}
        />
        {deployment?.relay_profile && !relayEnabled ? (
          <p role="alert" className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
            {t("pc.deployments.form.relayRemoveWarning")}
          </p>
        ) : null}
        {relayEnabled ? (
          <Controller
            control={form.control}
            name="relay"
            render={({ field }) => (
              <RelayProfileEditor
                value={field.value}
                onChange={field.onChange}
                errors={form.formState.isSubmitted ? fromDraft(field.value).problems : undefined}
                context={relayContext}
                extraKeys={Object.keys(extra)}
                onSuggest={() => field.onChange(suggest(field.value))}
              />
            )}
          />
        ) : null}
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" disabled={save.isPending || conflict || reloading || archived}>
          {save.isPending ? <Spinner /> : null}
          {deployment ? t("common.save") : t("common.create")}
        </Button>
      </DialogFooter>
    </form>
  );
}
