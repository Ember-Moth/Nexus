"use client";

import { CheckIcon, CopyIcon, InfoIcon, TriangleAlertIcon } from "lucide-react";
import { useState } from "react";

import { useCopy } from "@/components/copy-button";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { useT } from "@/i18n/provider";
import type { ApiKeyIssued } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";

/**
 * Shows a newly issued secret exactly once. "Done" stays disabled until the user confirms that
 * the key is stored; the dialog blocks Escape and outside clicks meanwhile (see callers).
 */
export function SecretPanel({
  issued,
  serviceAccountName,
  rotated,
  onDone,
}: {
  issued: ApiKeyIssued & { secret: string };
  serviceAccountName?: string;
  rotated?: boolean;
  onDone: () => void;
}) {
  const t = useT();
  const { copied, copy } = useCopy(4000);
  const [saved, setSaved] = useState(false);
  const prefix = issued.key.prefix;
  const rest = issued.secret.startsWith(prefix) ? issued.secret.slice(prefix.length) : issued.secret;

  return (
    <>
      <DialogHeader className="flex-row items-start gap-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-full bg-emerald-50">
          <CheckIcon className="size-[18px] text-emerald-700" aria-hidden />
        </span>
        <div className="flex flex-col gap-1">
          <DialogTitle>{rotated ? t("keys.secretDialog.rotatedTitle") : t("keys.secretDialog.title")}</DialogTitle>
          <DialogDescription>{t("keys.secretDialog.description")}</DialogDescription>
        </div>
      </DialogHeader>
      <div className="flex flex-col gap-2">
        <span id="secret-label" className="text-sm font-medium">
          {t("keys.secretDialog.fullKey")}
        </span>
        <div className="flex items-stretch gap-2">
          <div
            aria-labelledby="secret-label"
            data-testid="issued-secret"
            className="min-w-0 flex-1 rounded-md border bg-muted/50 px-3 py-2.5 font-mono text-[13px] leading-relaxed break-all select-all"
          >
            <strong className="font-semibold">{prefix}</strong>
            {rest}
          </div>
          <Button
            type="button"
            variant="outline"
            className={copied ? "h-auto w-[88px] border-emerald-200 bg-emerald-50 text-emerald-800" : "h-auto w-[88px]"}
            onClick={() => void copy(issued.secret)}
          >
            {copied ? <CheckIcon /> : <CopyIcon />}
            {copied ? t("common.copied") : t("common.copy")}
          </Button>
        </div>
      </div>
      <dl className="grid grid-cols-[88px_1fr] gap-y-1.5 rounded-md border px-3 py-2.5 text-[13px]">
        <dt className="text-muted-foreground">{t("keys.secretDialog.name")}</dt>
        <dd>{issued.key.name}</dd>
        {serviceAccountName ? (
          <>
            <dt className="text-muted-foreground">{t("keys.secretDialog.serviceAccount")}</dt>
            <dd>{serviceAccountName}</dd>
          </>
        ) : null}
        <dt className="text-muted-foreground">{t("keys.secretDialog.expires")}</dt>
        <dd className="tabular-nums">
          {issued.key.expires_at ? `${formatDateTime(issued.key.expires_at, { seconds: false })} UTC` : t("common.neverExpires")}
        </dd>
      </dl>
      <div className="flex gap-2.5 rounded-md border border-amber-200 bg-amber-50 px-3 py-2.5 text-[13px] text-amber-900">
        <TriangleAlertIcon className="mt-0.5 size-4 shrink-0 text-amber-700" aria-hidden />
        <span>{t("keys.secretDialog.warning")}</span>
      </div>
      <Label
        htmlFor="secret-saved"
        className={
          saved
            ? "cursor-pointer rounded-md border border-foreground bg-muted/40 px-3 py-2.5"
            : "cursor-pointer rounded-md border px-3 py-2.5"
        }
      >
        <Checkbox id="secret-saved" checked={saved} onCheckedChange={(value) => setSaved(value === true)} />
        {t("keys.secretDialog.confirm")}
      </Label>
      <DialogFooter className="items-center sm:justify-between">
        <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <InfoIcon className="size-3.5" aria-hidden />
          {t("keys.secretDialog.closeHint")}
        </span>
        <Button type="button" disabled={!saved} onClick={onDone}>
          {t("common.done")}
        </Button>
      </DialogFooter>
    </>
  );
}

/** 200 replay of an issue/rotate request: the secret cannot be shown again. */
export function ReplayPanel({
  issued,
  onClose,
  onRotate,
}: {
  issued: ApiKeyIssued;
  onClose: () => void;
  onRotate?: () => void;
}) {
  const t = useT();
  return (
    <>
      <DialogHeader className="flex-row items-start gap-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-full bg-blue-50">
          <InfoIcon className="size-[18px] text-blue-700" aria-hidden />
        </span>
        <div className="flex flex-col gap-1">
          <DialogTitle>{t("keys.replayDialog.title")}</DialogTitle>
          <DialogDescription>{t("keys.replayDialog.description")}</DialogDescription>
        </div>
      </DialogHeader>
      <dl className="grid grid-cols-[88px_1fr] gap-y-1.5 rounded-md border px-3 py-2.5 text-[13px]">
        <dt className="text-muted-foreground">{t("keys.secretDialog.name")}</dt>
        <dd>{issued.key.name}</dd>
        <dt className="text-muted-foreground">{t("keys.replayDialog.prefix")}</dt>
        <dd className="font-mono">{issued.key.prefix}…</dd>
        <dt className="text-muted-foreground">{t("keys.replayDialog.issuedAt")}</dt>
        <dd className="tabular-nums">{formatDateTime(issued.key.created_at)} UTC</dd>
      </dl>
      <p className="text-sm text-foreground/80">{t("keys.replayDialog.body")}</p>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>
          {t("common.close")}
        </Button>
        {onRotate ? (
          <Button type="button" onClick={onRotate}>
            {t("keys.replayDialog.rotate")}
          </Button>
        ) : null}
      </DialogFooter>
    </>
  );
}

export function hasSecret(issued: ApiKeyIssued): issued is ApiKeyIssued & { secret: string } {
  return issued.secret_status === "available" && typeof issued.secret === "string" && issued.secret.length > 0;
}
