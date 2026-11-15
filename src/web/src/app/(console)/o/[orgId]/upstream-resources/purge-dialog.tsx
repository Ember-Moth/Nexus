"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { toast } from "sonner";

import { ErrorAlert, Spinner } from "@/components/feedback";
import { StatusBadge, type Tone } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { pollUntil, useIdempotentMutation } from "@/lib/api/hooks";
import { orgKey } from "@/lib/api/queries";
import type { PurgeRequest, UpstreamResource } from "@/lib/api/types";

/** What to delete: one resource, or every upstream copy a key holds. */
export type PurgeTarget =
  | { scope: "resource"; resource: UpstreamResource; keyLabel: string }
  | { scope: "key"; apiKeyId: string; keyLabel: string };

const REQUEST_TONES: Record<PurgeRequest["state"], Tone> = {
  pending: "info",
  purged: "success",
  failed: "danger",
};

function usePurgeRequest(orgId: string, requestId: string | undefined) {
  return useQuery({
    queryKey: orgKey(orgId, "upstream-purges", requestId),
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/upstream-purges/{purge_request_id}", {
            params: { path: { organization_id: orgId, purge_request_id: requestId! } },
          }),
        )
      ).data,
    enabled: Boolean(requestId),
    refetchInterval: pollUntil<PurgeRequest>((data) => data !== undefined && data.state !== "pending"),
  });
}

export function PurgeDialog({
  orgId,
  target,
  onOpenChange,
}: {
  orgId: string;
  target: PurgeTarget | null;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const open = target !== null;

  const purge = useIdempotentMutation({
    keyPrefix: "purge-upstream",
    mutationFn: async (body: { api_key_id: string; kind?: UpstreamResource["kind"]; resource_id?: string }, key) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/upstream-purges", {
            params: { path: { organization_id: orgId }, header: { "Idempotency-Key": key } },
            body,
          }),
        )
      ).data,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "upstream-resources") }),
  });
  const progress = usePurgeRequest(orgId, purge.data?.id);
  const request = progress.data ?? purge.data;

  // Refresh the list and announce the outcome once the request reaches a final state.
  const announced = useRef<string | null>(null);
  useEffect(() => {
    if (!request || request.state === "pending" || announced.current === request.id) return;
    announced.current = request.id;
    void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "upstream-resources") });
    if (request.total === 0) return;
    if (request.state === "purged") toast.success(t("ws.upstream.toastDone"));
    else toast.error(t("ws.upstream.toastFailed"));
  }, [request, orgId, queryClient, t]);

  function close() {
    onOpenChange(false);
    purge.resetOperation();
  }

  function submit() {
    if (!target) return;
    purge.mutate(
      target.scope === "resource"
        ? {
            api_key_id: target.resource.api_key_id,
            kind: target.resource.kind,
            resource_id: target.resource.id,
          }
        : { api_key_id: target.apiKeyId },
    );
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent role="alertdialog" className="sm:max-w-[460px]">
        {target ? (
          <>
            <DialogHeader>
              <DialogTitle>
                {target.scope === "resource"
                  ? t("ws.upstream.purgeDialog.titleOne")
                  : t("ws.upstream.purgeDialog.titleKey")}
              </DialogTitle>
              <DialogDescription>
                {target.scope === "resource"
                  ? t("ws.upstream.purgeDialog.descriptionOne")
                  : t("ws.upstream.purgeDialog.descriptionKey")}
              </DialogDescription>
            </DialogHeader>
            {purge.error ? <ErrorAlert error={purge.error} /> : null}
            <dl className="grid grid-cols-[88px_1fr] gap-y-1.5 rounded-md border px-3 py-2.5 text-[13px]">
              {target.scope === "resource" ? (
                <>
                  <dt className="text-muted-foreground">{t("ws.upstream.purgeDialog.handle")}</dt>
                  <dd className="font-mono break-all">{target.resource.handle ?? t("ws.upstream.unsaved")}</dd>
                  <dt className="text-muted-foreground">{t("ws.upstream.purgeDialog.kind")}</dt>
                  <dd>{t(`ws.upstream.kinds.${target.resource.kind}`)}</dd>
                </>
              ) : null}
              <dt className="text-muted-foreground">{t("ws.upstream.purgeDialog.key")}</dt>
              <dd>{target.keyLabel}</dd>
            </dl>
            {request ? (
              <div className="grid gap-2 rounded-md border bg-muted/40 px-3 py-2.5 text-[13px]" aria-live="polite">
                <div className="flex items-center gap-2">
                  <StatusBadge tone={REQUEST_TONES[request.state]}>
                    {t(`ws.upstream.purgeDialog.states.${request.state}`)}
                  </StatusBadge>
                  <span className="tabular-nums">
                    {request.total === 0
                      ? t("ws.upstream.purgeDialog.progressNone")
                      : t("ws.upstream.purgeDialog.progress", { purged: request.purged, total: request.total })}
                  </span>
                  {request.failed > 0 ? (
                    <span className="text-destructive tabular-nums">
                      {t("ws.upstream.purgeDialog.progressFailed", { failed: request.failed })}
                    </span>
                  ) : null}
                  {request.state === "pending" ? <Spinner className="ml-auto" /> : null}
                </div>
                {request.state === "pending" ? (
                  <p className="text-muted-foreground">{t("ws.upstream.purgeDialog.accepted")}</p>
                ) : request.state === "failed" ? (
                  <p className="text-muted-foreground">{t("ws.upstream.purgeDialog.failedHint")}</p>
                ) : null}
              </div>
            ) : null}
            <DialogFooter>
              {request ? (
                <Button type="button" onClick={close}>
                  {t("common.close")}
                </Button>
              ) : (
                <>
                  <Button type="button" variant="outline" onClick={close}>
                    {t("common.cancel")}
                  </Button>
                  <Button
                    type="button"
                    className="bg-destructive text-white hover:bg-destructive/90"
                    disabled={purge.isPending}
                    onClick={submit}
                  >
                    {purge.isPending ? <Spinner /> : null}
                    {purge.isPending ? t("ws.upstream.purgeDialog.submitting") : t("ws.upstream.purgeDialog.submit")}
                  </Button>
                </>
              )}
            </DialogFooter>
          </>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
