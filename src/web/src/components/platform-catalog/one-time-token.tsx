"use client";

import { CheckIcon, CopyIcon, InfoIcon, TriangleAlertIcon } from "lucide-react";
import { useState } from "react";

import { CopyButton, useCopy } from "@/components/copy-button";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { useT } from "@/i18n/provider";
import { formatDateTime } from "@/lib/format";

import type { SetupIssued } from "./types";

/** Where the user redeems the token. The token is never put into the URL. */
export function setupPasswordUrl() {
  return typeof window === "undefined" ? "/setup-password" : `${window.location.origin}/setup-password`;
}

/**
 * Shows a password setup token exactly once (POST /users, POST /users/{id}/password-reset-tokens).
 * "Done" stays disabled until the admin confirms the token is stored; callers block Escape and
 * outside clicks while this panel is shown.
 */
export function OneTimeTokenPanel({
  issued,
  reset,
  onDone,
}: {
  issued: SetupIssued;
  reset?: boolean;
  onDone: () => void;
}) {
  const t = useT();
  const { copied, copy } = useCopy(4000);
  const [saved, setSaved] = useState(false);
  const url = setupPasswordUrl();

  return (
    <>
      <DialogHeader className="flex-row items-start gap-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-full bg-emerald-50">
          <CheckIcon className="size-[18px] text-emerald-700" aria-hidden />
        </span>
        <div className="flex flex-col gap-1">
          <DialogTitle>{reset ? t("pc.oneTime.resetTitle") : t("pc.oneTime.title")}</DialogTitle>
          <DialogDescription>{t("pc.oneTime.description")}</DialogDescription>
        </div>
      </DialogHeader>
      <div className="flex flex-col gap-2">
        <span id="setup-token-label" className="text-sm font-medium">
          {t("pc.oneTime.token")}
        </span>
        <div className="flex items-stretch gap-2">
          <div
            aria-labelledby="setup-token-label"
            data-testid="setup-token"
            className="min-w-0 flex-1 rounded-md border bg-muted/50 px-3 py-2.5 font-mono text-[13px] leading-relaxed break-all select-all"
          >
            {issued.setup_token}
          </div>
          <Button
            type="button"
            variant="outline"
            className={copied ? "h-auto w-[88px] border-emerald-200 bg-emerald-50 text-emerald-800" : "h-auto w-[88px]"}
            onClick={() => void copy(issued.setup_token)}
          >
            {copied ? <CheckIcon /> : <CopyIcon />}
            {copied ? t("common.copied") : t("common.copy")}
          </Button>
        </div>
      </div>
      <dl className="grid grid-cols-[88px_1fr] items-center gap-y-1.5 rounded-md border px-3 py-2.5 text-[13px]">
        <dt className="text-muted-foreground">{t("pc.oneTime.user")}</dt>
        <dd className="font-mono">{issued.user.login}</dd>
        <dt className="text-muted-foreground">{t("pc.oneTime.setupUrl")}</dt>
        <dd className="flex min-w-0 items-center gap-1">
          <code className="min-w-0 truncate font-mono text-[12.5px]" title={url}>
            {url}
          </code>
          <CopyButton value={url} />
        </dd>
        <dt className="text-muted-foreground">{t("pc.oneTime.expires")}</dt>
        <dd className="tabular-nums">
          {formatDateTime(issued.expires_at, { seconds: false })} {t("common.utc")}
        </dd>
      </dl>
      <div className="flex gap-2.5 rounded-md border border-amber-200 bg-amber-50 px-3 py-2.5 text-[13px] text-amber-900">
        <TriangleAlertIcon className="mt-0.5 size-4 shrink-0 text-amber-700" aria-hidden />
        <span>{t("pc.oneTime.warning")}</span>
      </div>
      <Label
        htmlFor="setup-token-saved"
        className={
          saved
            ? "cursor-pointer rounded-md border border-foreground bg-muted/40 px-3 py-2.5"
            : "cursor-pointer rounded-md border px-3 py-2.5"
        }
      >
        <Checkbox id="setup-token-saved" checked={saved} onCheckedChange={(value) => setSaved(value === true)} />
        {t("pc.oneTime.confirm")}
      </Label>
      <DialogFooter className="items-center sm:justify-between">
        <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <InfoIcon className="size-3.5" aria-hidden />
          {t("pc.oneTime.closeHint")}
        </span>
        <Button type="button" disabled={!saved} onClick={onDone}>
          {t("common.done")}
        </Button>
      </DialogFooter>
    </>
  );
}
