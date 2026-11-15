"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { EyeIcon, EyeOffIcon } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { ConflictAlert, Spinner } from "@/components/feedback";
import { PcErrorAlert } from "@/components/platform-catalog/errors";
import { Facts, Field } from "@/components/platform-catalog/form";
import { catalogKey, etagOf, invalidateCatalog } from "@/components/platform-catalog/queries";
import type { CatalogDeployment, Credential, Upstream } from "@/components/platform-catalog/types";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";

/** Visible ASCII only (no whitespace), like the API; Bedrock JSON must carry the signing keys. */
function validSecret(value: string, protocol: Upstream["protocol_family"]) {
  if (!/^[\x21-\x7E]{1,4096}$/.test(value)) return false;
  if (protocol !== "aws_bedrock" || !value.startsWith("{")) return true;
  try {
    const material = JSON.parse(value) as Record<string, unknown>;
    const keys = Object.keys(material);
    return (
      typeof material === "object" &&
      material !== null &&
      ["access_key_id", "secret_access_key"].every((k) => keys.includes(k)) &&
      keys.every((k) => ["access_key_id", "secret_access_key", "session_token"].includes(k)) &&
      Object.values(material).every((v) => typeof v === "string" && v.length > 0)
    );
  } catch {
    return false;
  }
}

/** The secret is write-only: it lives in this form until submit and is never shown back. */
export function CreateCredentialDialog({ upstream, onClose }: { upstream: Upstream; onClose: () => void }) {
  const t = useT();
  const queryClient = useQueryClient();
  const [reveal, setReveal] = useState(false);
  const schema = z.object({
    name: z.string().trim().min(1).max(200),
    secret: z.string().refine((value) => validSecret(value, upstream.protocol_family)),
  });
  type Values = z.infer<typeof schema>;
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { name: "", secret: "" } });
  const errors = form.formState.errors;

  const create = useMutation({
    mutationFn: async (values: Values) =>
      (
        await call(() =>
          nexus.POST("/api/v1/upstreams/{upstream_id}/credentials", {
            params: { path: { upstream_id: upstream.id } },
            body: { name: values.name.trim(), secret: values.secret },
          }),
        )
      ).data,
    onSuccess: () => {
      toast.success(t("pc.credentials.form.created"));
      void invalidateCatalog(queryClient);
      form.reset({ name: "", secret: "" });
      onClose();
    },
  });

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-[480px]">
        <form noValidate className="grid gap-5" onSubmit={form.handleSubmit((values) => create.mutate(values))}>
          <DialogHeader>
            <DialogTitle>{t("pc.credentials.form.title")}</DialogTitle>
            <DialogDescription>{t("pc.credentials.form.description")}</DialogDescription>
          </DialogHeader>
          {create.error ? <PcErrorAlert error={create.error} /> : null}
          <Field id="credential-name" label={t("pc.credentials.form.name")} error={errors.name ? t("pc.common.required") : undefined}>
            <Input
              id="credential-name"
              maxLength={200}
              autoComplete="off"
              aria-invalid={Boolean(errors.name) || undefined}
              {...form.register("name")}
            />
          </Field>
          <Field
            id="credential-secret"
            label={t("pc.credentials.form.secret")}
            hint={
              upstream.protocol_family === "aws_bedrock"
                ? t("pc.credentials.form.secretHintBedrock")
                : t("pc.credentials.form.secretHint")
            }
            error={errors.secret ? t("pc.credentials.form.secretInvalid") : undefined}
          >
            <div className="flex gap-2">
              <Input
                id="credential-secret"
                type={reveal ? "text" : "password"}
                autoComplete="new-password"
                spellCheck={false}
                maxLength={4096}
                className="font-mono"
                aria-invalid={Boolean(errors.secret) || undefined}
                {...form.register("secret")}
              />
              <Button
                type="button"
                variant="outline"
                size="icon"
                aria-label={reveal ? t("pc.credentials.form.hide") : t("pc.credentials.form.show")}
                title={reveal ? t("pc.credentials.form.hide") : t("pc.credentials.form.show")}
                onClick={() => setReveal((value) => !value)}
              >
                {reveal ? <EyeOffIcon /> : <EyeIcon />}
              </Button>
            </div>
          </Field>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
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

/** Reads the credential for its ETag, then POST …/revoke with If-Match. */
export function RevokeCredentialDialog({
  upstreamId,
  credential,
  deployments,
  onClose,
}: {
  upstreamId: string;
  credential: Credential;
  deployments: CatalogDeployment[];
  onClose: () => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const [confirmation, setConfirmation] = useState("");
  const detail = useQuery({
    queryKey: catalogKey("credential", credential.id),
    queryFn: async () =>
      call(() =>
        nexus.GET("/api/v1/upstreams/{upstream_id}/credentials/{credential_id}", {
          params: { path: { upstream_id: upstreamId, credential_id: credential.id } },
        }),
      ),
    staleTime: 0,
  });
  const current = detail.data?.data ?? credential;
  const etag = etagOf(detail.data, current.version);

  const revoke = useMutation({
    mutationFn: async (ifMatch: string) =>
      (
        await call(() =>
          nexus.POST("/api/v1/upstreams/{upstream_id}/credentials/{credential_id}/revoke", {
            params: { path: { upstream_id: upstreamId, credential_id: credential.id }, header: { "If-Match": ifMatch } },
          }),
        )
      ).data,
    onSuccess: () => {
      toast.success(t("pc.credentials.revokeDialog.done"));
      void invalidateCatalog(queryClient);
      onClose();
    },
  });
  const conflict = isConflict(revoke.error);
  const matches = confirmation.trim() === current.name;

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent role="alertdialog" className="sm:max-w-[460px]">
        <DialogHeader>
          <DialogTitle>{t("pc.credentials.revokeDialog.title")}</DialogTitle>
          <DialogDescription>{t("pc.credentials.revokeDialog.description")}</DialogDescription>
        </DialogHeader>
        {conflict ? (
          <ConflictAlert
            onReload={() => {
              revoke.reset();
              void detail.refetch();
            }}
          />
        ) : revoke.error ? (
          <PcErrorAlert error={revoke.error} />
        ) : detail.error ? (
          <PcErrorAlert error={detail.error} onRetry={() => void detail.refetch()} />
        ) : null}
        <Facts
          items={[
            [t("pc.credentials.columns.name"), current.name],
            [t("pc.credentials.columns.keyVersion"), <span key="v" className="tabular-nums">{current.key_version}</span>],
          ]}
        />
        {deployments.length > 0 ? (
          <div className="flex flex-col gap-1 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[13px] text-amber-900">
            <span>{t("pc.credentials.revokeDialog.inUse")}</span>
            <ul className="list-disc pl-5">
              {deployments.map((d) => (
                <li key={d.id}>
                  {d.name} <span className="text-amber-800/80">· {d.upstream_model}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        <Field id="credential-revoke-confirm" label={t("pc.credentials.revokeDialog.confirmLabel", { name: current.name })}>
          <Input
            id="credential-revoke-confirm"
            autoComplete="off"
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value)}
          />
        </Field>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            type="button"
            className="bg-destructive text-white hover:bg-destructive/90"
            disabled={!matches || !etag || revoke.isPending || conflict || detail.isFetching || current.status !== "active"}
            onClick={() => etag && revoke.mutate(etag)}
          >
            {revoke.isPending ? <Spinner /> : null}
            {t("pc.credentials.revokeDialog.submit")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
