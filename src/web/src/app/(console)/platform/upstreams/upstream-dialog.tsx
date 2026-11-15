"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Controller, useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { ConflictAlert, Spinner } from "@/components/feedback";
import { PcErrorAlert } from "@/components/platform-catalog/errors";
import { Field } from "@/components/platform-catalog/form";
import { PROTOCOLS, STATUSES } from "@/components/platform-catalog/labels";
import { etagOf, invalidateCatalog, useUpstreamDetail } from "@/components/platform-catalog/queries";
import type { CatalogStatus, ProtocolFamily, Upstream, UpstreamPatch } from "@/components/platform-catalog/types";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";

/** Mirrors the API: HTTPS only, no credentials, query or fragment. */
function isHttpsEndpoint(value: string) {
  try {
    const url = new URL(value.trim());
    return url.protocol === "https:" && !url.username && !url.password && !url.search && !url.hash && Boolean(url.hostname);
  } catch {
    return false;
  }
}

const schema = z.object({
  name: z.string().trim().min(1).max(200),
  baseUrl: z.string().trim().min(1).max(2048).refine(isHttpsEndpoint),
  protocol: z.enum(PROTOCOLS as [ProtocolFamily, ...ProtocolFamily[]], { message: "required" }),
  status: z.enum(STATUSES as [CatalogStatus, ...CatalogStatus[]]),
});
type Values = z.input<typeof schema>;

/** The API stores the address without a trailing slash. */
const normalizeUrl = (value: string) => value.trim().replace(/\/+$/, "");

function patchFor(upstream: Upstream, values: Values): UpstreamPatch {
  if (values.status === "archived" && upstream.status !== "archived") return { status: "archived" };
  const patch: UpstreamPatch = {};
  if (values.name.trim() !== upstream.name) patch.name = values.name.trim();
  if (normalizeUrl(values.baseUrl) !== normalizeUrl(upstream.base_url)) patch.base_url = values.baseUrl.trim();
  if (values.status !== upstream.status) patch.status = values.status;
  return patch;
}

export function UpstreamDialog({
  upstream,
  onClose,
  onCreated,
}: {
  upstream: Upstream | null;
  onClose: () => void;
  onCreated?: (upstream: Upstream) => void;
}) {
  const t = useT();
  const detail = useUpstreamDetail(upstream?.id);
  const current = detail.data?.data;
  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-[520px]">
        <DialogHeader>
          <DialogTitle>
            {upstream ? t("pc.upstreams.form.editTitle", { name: upstream.name }) : t("pc.upstreams.form.createTitle")}
          </DialogTitle>
          <DialogDescription>{t("pc.upstreams.form.description")}</DialogDescription>
        </DialogHeader>
        {upstream && detail.error ? <PcErrorAlert error={detail.error} onRetry={() => void detail.refetch()} /> : null}
        {!upstream ? (
          <UpstreamForm upstream={null} etag={null} onClose={onClose} onCreated={onCreated} />
        ) : current ? (
          <UpstreamForm
            key={current.version}
            upstream={current}
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

function UpstreamForm({
  upstream,
  etag,
  reloading,
  onReload,
  onClose,
  onCreated,
}: {
  upstream: Upstream | null;
  etag: string | null;
  reloading?: boolean;
  onReload?: () => void;
  onClose: () => void;
  onCreated?: (upstream: Upstream) => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: {
      name: upstream?.name ?? "",
      baseUrl: upstream?.base_url ?? "",
      protocol: upstream?.protocol_family,
      status: upstream?.status ?? "active",
    },
  });
  const errors = form.formState.errors;
  const [status, baseUrl] = useWatch({ control: form.control, name: ["status", "baseUrl"] });
  const archived = upstream?.status === "archived";
  const archiving = Boolean(upstream) && status === "archived" && !archived;
  const addressChanged = Boolean(upstream) && normalizeUrl(baseUrl ?? "") !== normalizeUrl(upstream?.base_url ?? "");

  const save = useMutation({
    mutationFn: async (values: Values) => {
      if (!upstream) {
        return (
          await call(() =>
            nexus.POST("/api/v1/upstreams", {
              body: { name: values.name.trim(), base_url: values.baseUrl.trim(), protocol_family: values.protocol },
            }),
          )
        ).data;
      }
      return (
        await call(() =>
          nexus.PATCH("/api/v1/upstreams/{upstream_id}", {
            params: { path: { upstream_id: upstream.id }, header: { "If-Match": etag ?? `"${upstream.version}"` } },
            body: patchFor(upstream, values),
          }),
        )
      ).data;
    },
    onSuccess: (result) => {
      toast.success(upstream ? t("pc.upstreams.form.saved") : t("pc.upstreams.form.created"));
      void invalidateCatalog(queryClient);
      if (!upstream) onCreated?.(result);
      onClose();
    },
  });

  const conflict = isConflict(save.error);
  const submit = form.handleSubmit((values) => {
    if (upstream && Object.keys(patchFor(upstream, values)).length === 0) {
      toast.info(t("pc.common.noChanges"));
      return;
    }
    save.mutate(values);
  });

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
      <Field id="upstream-name" label={t("pc.upstreams.form.name")} error={errors.name ? t("pc.common.required") : undefined}>
        <Input id="upstream-name" maxLength={200} aria-invalid={Boolean(errors.name) || undefined} {...form.register("name")} />
      </Field>
      <Field
        id="upstream-base-url"
        label={t("pc.upstreams.form.baseUrl")}
        hint={
          addressChanged && upstream?.status !== "disabled"
            ? undefined
            : t("pc.upstreams.form.baseUrlHint")
        }
        error={errors.baseUrl ? t("pc.upstreams.form.baseUrlInvalid") : undefined}
      >
        <Input
          id="upstream-base-url"
          type="url"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          className="font-mono text-[13px]"
          placeholder="https://"
          maxLength={2048}
          aria-invalid={Boolean(errors.baseUrl) || undefined}
          {...form.register("baseUrl")}
        />
        {addressChanged && upstream?.status !== "disabled" && !errors.baseUrl ? (
          <p role="alert" className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
            {t("pc.upstreams.form.baseUrlLocked")}
          </p>
        ) : null}
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          id="upstream-protocol"
          label={t("pc.upstreams.form.protocol")}
          hint={upstream ? t("pc.upstreams.form.protocolLocked") : undefined}
          error={errors.protocol ? t("pc.common.required") : undefined}
        >
          <Controller
            control={form.control}
            name="protocol"
            render={({ field, fieldState }) => (
              <Select value={field.value ?? ""} onValueChange={field.onChange} disabled={Boolean(upstream)}>
                <SelectTrigger id="upstream-protocol" className="w-full" aria-invalid={fieldState.invalid || undefined}>
                  <SelectValue placeholder={t("pc.common.select")} />
                </SelectTrigger>
                <SelectContent>
                  {PROTOCOLS.map((p) => (
                    <SelectItem key={p} value={p}>
                      {t(`pc.enums.protocol.${p}`)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          />
        </Field>
        {upstream ? (
          <Field id="upstream-status" label={t("pc.upstreams.form.status")}>
            <Controller
              control={form.control}
              name="status"
              render={({ field }) => (
                <Select value={field.value} onValueChange={field.onChange}>
                  <SelectTrigger id="upstream-status" className="w-full">
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
          </Field>
        ) : null}
      </div>
      {archiving ? (
        <p role="alert" className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          {t("pc.common.archiveWarning")}
        </p>
      ) : null}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" disabled={save.isPending || conflict || reloading || archived}>
          {save.isPending ? <Spinner /> : null}
          {upstream ? t("common.save") : t("common.create")}
        </Button>
      </DialogFooter>
    </form>
  );
}
