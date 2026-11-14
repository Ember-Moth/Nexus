"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";

import { ConflictAlert, ErrorAlert, Spinner } from "@/components/feedback";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import { useIdempotentMutation } from "@/lib/api/hooks";
import { orgKey } from "@/lib/api/queries";
import type { ApiKey, ApiKeyIssued } from "@/lib/api/types";

import { canRevoke, canRotate, keyStatus } from "./key-status";
import { hasSecret, ReplayPanel, SecretPanel } from "./secret-panel";

/**
 * Reads the key with its ETag when a dialog opens. Writes send that ETag as If-Match; a 412
 * means somebody changed the key in the meantime and the dialog reloads it.
 */
function useKeyWithEtag(orgId: string, keyId: string | undefined, open: boolean) {
  return useQuery({
    queryKey: orgKey(orgId, "api-keys", "detail", keyId),
    queryFn: async () =>
      call(() =>
        nexus.GET("/api/v1/organizations/{organization_id}/api-keys/{key_id}", {
          params: { path: { organization_id: orgId, key_id: keyId! } },
        }),
      ),
    enabled: open && Boolean(keyId),
    staleTime: 0,
  });
}

const GRACE_OPTIONS = [
  { seconds: 0, label: "keys.rotateDialog.graceNow" },
  { seconds: 300, label: "keys.rotateDialog.grace5" },
  { seconds: 600, label: "keys.rotateDialog.grace10" },
  { seconds: 3600, label: "keys.rotateDialog.grace60" },
] as const;

interface RotateVariables {
  keyId: string;
  ifMatch: string;
  name: string | undefined;
  grace_seconds: number;
}

export function RotateKeyDialog({
  orgId,
  apiKey,
  onOpenChange,
}: {
  orgId: string;
  apiKey: ApiKey | null;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const open = apiKey !== null;
  const detail = useKeyWithEtag(orgId, apiKey?.id, open);
  const [name, setName] = useState("");
  const [grace, setGrace] = useState(600);
  const [result, setResult] = useState<ApiKeyIssued | null>(null);

  const rotate = useIdempotentMutation<ApiKeyIssued, RotateVariables>({
    keyPrefix: "rotate-key",
    mutationFn: async ({ keyId, ifMatch, name: newName, grace_seconds }, idempotencyKey) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/api-keys/{key_id}/rotate", {
            params: {
              path: { organization_id: orgId, key_id: keyId },
              header: { "if-match": ifMatch, "Idempotency-Key": idempotencyKey },
            },
            body: { name: newName, grace_seconds },
          }),
        )
      ).data,
    onSuccess: (issued) => {
      setResult(issued);
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "api-keys") });
    },
  });

  const showingSecret = result !== null && hasSecret(result);
  const close = () => {
    onOpenChange(false);
    setResult(null);
    setName("");
    setGrace(600);
    rotate.resetOperation();
  };

  const current = detail.data?.data ?? apiKey;
  const etag = detail.data?.etag ?? (current ? `"${current.version}"` : null);
  const rotatable = current ? canRotate(keyStatus(current)) : false;
  const conflict = isConflict(rotate.error);

  const submit = () => {
    if (!current || !etag) return;
    rotate.mutate({ keyId: current.id, ifMatch: etag, name: name.trim() || undefined, grace_seconds: grace });
  };

  const reload = async () => {
    rotate.resetOperation();
    await detail.refetch();
    void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "api-keys") });
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next && showingSecret) return;
        if (!next) close();
      }}
    >
      <DialogContent
        className="sm:max-w-[460px]"
        showCloseButton={!showingSecret}
        onEscapeKeyDown={(event) => showingSecret && event.preventDefault()}
        onInteractOutside={(event) => showingSecret && event.preventDefault()}
      >
        {result && hasSecret(result) ? (
          <SecretPanel issued={result} rotated onDone={close} />
        ) : result ? (
          <ReplayPanel issued={result} onClose={close} />
        ) : current ? (
          <>
            <DialogHeader>
              <DialogTitle>{t("keys.rotateDialog.title", { prefix: current.prefix })}</DialogTitle>
              <DialogDescription>{t("keys.rotateDialog.description")}</DialogDescription>
            </DialogHeader>
            {conflict ? (
              <ConflictAlert onReload={() => void reload()} />
            ) : rotate.error ? (
              <ErrorAlert error={rotate.error} />
            ) : null}
            {detail.error ? <ErrorAlert error={detail.error} onRetry={() => void detail.refetch()} /> : null}
            {!rotatable && !detail.isFetching ? (
              <p className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
                {t(`keys.status${keyStatus(current).state === "grace" ? "Rotated" : "Revoked"}` as "keys.statusRevoked")}
              </p>
            ) : null}
            <div className="grid gap-2">
              <Label htmlFor="rotate-name">{t("keys.rotateDialog.name")}</Label>
              <Input
                id="rotate-name"
                value={name}
                maxLength={200}
                placeholder={t("keys.rotateDialog.namePlaceholder", { name: current.name })}
                onChange={(event) => setName(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <span id="grace-label" className="text-sm font-medium">
                {t("keys.rotateDialog.grace")}
              </span>
              <ToggleGroup
                type="single"
                variant="outline"
                value={String(grace)}
                onValueChange={(value) => value && setGrace(Number(value))}
                aria-labelledby="grace-label"
                className="grid w-full grid-cols-4"
              >
                {GRACE_OPTIONS.map((option) => (
                  <ToggleGroupItem key={option.seconds} value={String(option.seconds)}>
                    {t(option.label)}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
              <span className="text-xs text-muted-foreground">
                {grace === 0
                  ? t("keys.rotateDialog.graceHelpNow")
                  : t("keys.rotateDialog.graceHelp", { minutes: grace / 60 })}
              </span>
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={close}>
                {t("common.cancel")}
              </Button>
              <Button type="button" onClick={submit} disabled={rotate.isPending || !rotatable || conflict || detail.isFetching}>
                {rotate.isPending ? <Spinner /> : null}
                {rotate.isPending ? t("keys.rotateDialog.submitting") : t("keys.rotateDialog.submit")}
              </Button>
            </DialogFooter>
          </>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

export function RevokeKeyDialog({
  orgId,
  apiKey,
  onOpenChange,
}: {
  orgId: string;
  apiKey: ApiKey | null;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const open = apiKey !== null;
  const detail = useKeyWithEtag(orgId, apiKey?.id, open);
  const [confirmation, setConfirmation] = useState("");
  const current = detail.data?.data ?? apiKey;
  const etag = detail.data?.etag ?? (current ? `"${current.version}"` : null);

  const revoke = useIdempotentMutation<ApiKey, { keyId: string; ifMatch: string }>({
    // Revoke takes If-Match only; the hook still gives safe automatic retries.
    keyPrefix: "revoke-key",
    mutationFn: async ({ keyId, ifMatch }) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/api-keys/{key_id}/revoke", {
            params: { path: { organization_id: orgId, key_id: keyId }, header: { "if-match": ifMatch } },
          }),
        )
      ).data,
    onSuccess: (key) => {
      toast.success(t("keys.revokedToast"), { description: t("keys.revokedToastBody", { prefix: key.prefix }) });
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "api-keys") });
      close();
    },
  });

  function close() {
    onOpenChange(false);
    setConfirmation("");
    revoke.resetOperation();
  }

  const conflict = isConflict(revoke.error);
  const revocable = current ? canRevoke(keyStatus(current)) : false;
  const matches = current ? confirmation.trim() === current.prefix : false;

  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent role="alertdialog" className="sm:max-w-[420px]">
        {current ? (
          <>
            <DialogHeader>
              <DialogTitle>{t("keys.revokeDialog.title")}</DialogTitle>
              <DialogDescription>{t("keys.revokeDialog.description")}</DialogDescription>
            </DialogHeader>
            {conflict ? (
              <ConflictAlert
                onReload={() => {
                  revoke.resetOperation();
                  void detail.refetch();
                }}
              />
            ) : revoke.error ? (
              <ErrorAlert error={revoke.error} />
            ) : null}
            <dl className="grid grid-cols-[88px_1fr] gap-y-1.5 rounded-md border px-3 py-2.5 text-[13px]">
              <dt className="text-muted-foreground">{t("keys.secretDialog.name")}</dt>
              <dd>{current.name}</dd>
              <dt className="text-muted-foreground">{t("keys.replayDialog.prefix")}</dt>
              <dd className="font-mono">{current.prefix}…</dd>
            </dl>
            <div className="grid gap-2">
              <Label htmlFor="revoke-confirm">
                {t("keys.revokeDialog.confirmLabel", { prefix: current.prefix })}
              </Label>
              <Input
                id="revoke-confirm"
                autoComplete="off"
                className="font-mono"
                value={confirmation}
                onChange={(event) => setConfirmation(event.target.value)}
              />
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={close}>
                {t("common.cancel")}
              </Button>
              <Button
                type="button"
                className="bg-destructive text-white hover:bg-destructive/90"
                disabled={!matches || !revocable || revoke.isPending || conflict || !etag}
                onClick={() => etag && revoke.mutate({ keyId: current.id, ifMatch: etag })}
              >
                {revoke.isPending ? <Spinner /> : null}
                {revoke.isPending ? t("keys.revokeDialog.submitting") : t("keys.revokeDialog.submit")}
              </Button>
            </DialogFooter>
          </>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
