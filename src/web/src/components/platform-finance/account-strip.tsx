"use client";

import { Money } from "@/components/money";
import { StatusBadge, toneFor } from "@/components/status-badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorAlert } from "@/components/feedback";
import { useI18n } from "@/i18n/provider";
import { useAccount } from "@/lib/api/queries";
import { formatDateTime } from "@/lib/format";

/** Compact authoritative balance of one organization (GET …/account). */
export function AccountStrip({ orgId }: { orgId: string }) {
  const { t, maybeT } = useI18n();
  const account = useAccount(orgId);
  if (account.error) return <ErrorAlert error={account.error} onRetry={() => void account.refetch()} />;
  const data = account.data;
  if (!data) return <Skeleton className="h-[62px] w-full rounded-xl" />;
  const cells: [string, string][] = [
    [t("pf.common.balance"), data.balance],
    [t("pf.common.held"), data.held_amount],
    [t("pf.common.available"), data.available_balance],
  ];
  return (
    <div className="flex flex-wrap items-stretch gap-px overflow-hidden rounded-xl border bg-border">
      {cells.map(([label, value]) => (
        <div key={label} className="flex min-w-[160px] flex-1 flex-col gap-0.5 bg-card px-4 py-2.5">
          <span className="text-xs text-muted-foreground">{label}</span>
          <Money value={value} currency={data.currency} className="text-[15px] font-semibold" />
        </div>
      ))}
      <div className="flex min-w-[160px] flex-1 flex-col gap-1 bg-card px-4 py-2.5">
        <span className="text-xs text-muted-foreground">{t("pf.common.accountStatus")}</span>
        <span className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={data.status === "frozen" ? "danger" : toneFor(data.status)}>
            {maybeT(`pf.common.accountStates.${data.status}`) ?? data.status}
          </StatusBadge>
          <span className="text-[11px] text-muted-foreground tabular-nums">
            {formatDateTime(data.as_of)} {t("common.utc")}
          </span>
        </span>
      </div>
    </div>
  );
}
