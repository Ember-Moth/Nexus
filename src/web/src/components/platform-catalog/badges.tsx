"use client";

import { CheckCircle2Icon, CircleDashedIcon } from "lucide-react";

import { StatusBadge, toneFor } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { useT } from "@/i18n/provider";

import type { CatalogStatus, ProtocolFamily } from "./types";

/** "configured" from the API: activation, protocol match and loaded encryption key versions. */
export function ConfiguredBadge({ configured }: { configured: boolean }) {
  const t = useT();
  return (
    <span
      className={
        configured
          ? "inline-flex items-center gap-1 whitespace-nowrap text-emerald-700"
          : "inline-flex items-center gap-1 whitespace-nowrap text-amber-700"
      }
      title={t("pc.common.configuredHint")}
    >
      {configured ? <CheckCircle2Icon className="size-3.5" aria-hidden /> : <CircleDashedIcon className="size-3.5" aria-hidden />}
      {configured ? t("pc.common.configured") : t("pc.common.notConfigured")}
    </span>
  );
}

export function CatalogStatusBadge({ status }: { status: CatalogStatus }) {
  const t = useT();
  return <StatusBadge tone={toneFor(status)}>{t(`pc.enums.status.${status}`)}</StatusBadge>;
}

export function ProtocolBadge({ protocol }: { protocol: ProtocolFamily }) {
  const t = useT();
  return (
    <Badge variant="outline" className="font-normal">
      {t(`pc.enums.protocol.${protocol}`)}
    </Badge>
  );
}
