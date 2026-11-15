"use client";

import { CheckIcon, CopyIcon, InfoIcon, TriangleAlertIcon } from "lucide-react";
import { useState } from "react";

import { useCopy } from "@/components/copy-button";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { useI18n } from "@/i18n/provider";
import type { components } from "@/lib/api/schema";
import { formatDateTime } from "@/lib/format";

type InvitationIssued = components["schemas"]["InvitationIssued"];

function CopyField({ id, label, value, mono = true }: { id: string; label: string; value: string; mono?: boolean }) {
  const { t } = useI18n();
  const { copied, copy } = useCopy(4000);
  return (
    <div className="flex flex-col gap-2">
      <span id={id} className="text-sm font-medium">
        {label}
      </span>
      <div className="flex items-stretch gap-2">
        <div
          aria-labelledby={id}
          className={
            mono
              ? "min-w-0 flex-1 rounded-md border bg-muted/50 px-3 py-2.5 font-mono text-[13px] leading-relaxed break-all select-all"
              : "min-w-0 flex-1 rounded-md border bg-muted/50 px-3 py-2.5 text-[13px] leading-relaxed break-all select-all"
          }
        >
          {value}
        </div>
        <Button
          type="button"
          variant="outline"
          className={copied ? "h-auto w-[88px] border-emerald-200 bg-emerald-50 text-emerald-800" : "h-auto w-[88px]"}
          onClick={() => void copy(value)}
        >
          {copied ? <CheckIcon /> : <CopyIcon />}
          {copied ? t("common.copied") : t("common.copy")}
        </Button>
      </div>
    </div>
  );
}

/**
 * The invitation token is returned exactly once. Like a new API key, the dialog can only be
 * closed after the owner confirms that the token (or accept link) has been handed over.
 */
export function InvitationSecretPanel({ issued, onDone }: { issued: InvitationIssued; onDone: () => void }) {
  const { t, maybeT } = useI18n();
  const [saved, setSaved] = useState(false);
  const acceptUrl = `${window.location.origin}/invitations/accept?token=${encodeURIComponent(issued.token)}`;

  return (
    <>
      <DialogHeader className="flex-row items-start gap-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-full bg-emerald-50">
          <CheckIcon className="size-[18px] text-emerald-700" aria-hidden />
        </span>
        <div className="flex flex-col gap-1">
          <DialogTitle>{t("ws.invitations.issuedTitle")}</DialogTitle>
          <DialogDescription>{t("ws.invitations.issuedDescription")}</DialogDescription>
        </div>
      </DialogHeader>
      <CopyField id="invitation-link" label={t("ws.invitations.acceptLink")} value={acceptUrl} />
      <CopyField id="invitation-token" label={t("ws.invitations.token")} value={issued.token} />
      <dl className="grid grid-cols-[88px_1fr] gap-y-1.5 rounded-md border px-3 py-2.5 text-[13px]">
        <dt className="text-muted-foreground">{t("ws.invitations.login")}</dt>
        <dd>{issued.login}</dd>
        <dt className="text-muted-foreground">{t("ws.members.role")}</dt>
        <dd>{maybeT(`roles.${issued.role}`) ?? issued.role}</dd>
        <dt className="text-muted-foreground">{t("ws.invitations.columns.expires")}</dt>
        <dd className="tabular-nums">{formatDateTime(issued.expires_at, { seconds: false })} UTC</dd>
      </dl>
      <div className="flex gap-2.5 rounded-md border border-amber-200 bg-amber-50 px-3 py-2.5 text-[13px] text-amber-900">
        <TriangleAlertIcon className="mt-0.5 size-4 shrink-0 text-amber-700" aria-hidden />
        <span>{t("ws.invitations.warning")}</span>
      </div>
      <Label
        htmlFor="invitation-saved"
        className={
          saved
            ? "cursor-pointer rounded-md border border-foreground bg-muted/40 px-3 py-2.5"
            : "cursor-pointer rounded-md border px-3 py-2.5"
        }
      >
        <Checkbox id="invitation-saved" checked={saved} onCheckedChange={(value) => setSaved(value === true)} />
        {t("ws.invitations.confirm")}
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
