"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PlayIcon, ZapIcon } from "lucide-react";
import { toast } from "sonner";

import { ConflictAlert, ErrorAlert, Spinner } from "@/components/feedback";
import { StatusBadge, type Tone } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";
import { formatDateTime } from "@/lib/format";
import type { GatewayCluster } from "@/lib/api/types";

type AutoPublishState = components["schemas"]["AutoPublishStatus"]["state"];

const STATE_TONES: Record<AutoPublishState, Tone> = {
  current: "success",
  pending: "info",
  failing: "danger",
  paused: "warning",
  disabled: "neutral",
  unknown: "neutral",
};

/** Auto-publish state as a labelled badge; the failure reason is in the tooltip. */
export function AutoPublishBadge({ cluster }: { cluster: GatewayCluster }) {
  const t = useT();
  const status = cluster.auto_publish_status;
  const failure = status.last_failure;
  return (
    <StatusBadge tone={STATE_TONES[status.state]} className={failure && status.state === "failing" ? "cursor-help" : undefined}>
      <span title={failure && status.state === "failing" ? `${failure.code}: ${failure.message}` : undefined}>
        {t(`clusters.autoStates.${status.state}`)}
      </span>
    </StatusBadge>
  );
}

/** "3 个企业 · 2 个部署", or the rule for all / personal organizations. */
export function useScopeSummary() {
  const t = useT();
  return (cluster: Pick<GatewayCluster, "organization_scope" | "organization_ids" | "deployment_ids">) =>
    cluster.organization_scope === "listed"
      ? t("clusters.scope", { orgs: cluster.organization_ids.length, deployments: cluster.deployment_ids.length })
      : t(`clusters.scopeRule.${cluster.organization_scope}`, { deployments: cluster.deployment_ids.length });
}

/**
 * Automatic publication status from GET /gateway-clusters/{cluster_id}: the last automatic
 * release, the last failure and a rollback pause, which PATCH auto_publish=true resumes.
 */
export function AutoPublishCard({ cluster, etag }: { cluster: GatewayCluster; etag?: string | null }) {
  const t = useT();
  const queryClient = useQueryClient();
  const status = cluster.auto_publish_status;
  const resume = useMutation({
    mutationFn: async () =>
      call(() =>
        nexus.PATCH("/api/v1/gateway-clusters/{cluster_id}", {
          params: { path: { cluster_id: cluster.id }, header: { "If-Match": etag ?? `"${cluster.version}"` } },
          // key_grace_seconds is only read when public_key changes; the contract still requires it.
          body: { auto_publish: true, key_grace_seconds: 3600 },
        }),
      ),
    onSuccess: () => {
      toast.success(t("clusters.autoResumed"));
      void queryClient.invalidateQueries({ queryKey: ["gateway-clusters"] });
    },
  });
  const trigger = status.last_published ? Object.keys(status.last_published.trigger) : [];

  return (
    <section aria-labelledby="auto-publish-title" className="flex flex-col gap-3 rounded-xl border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="auto-publish-title" className="flex items-center gap-2 text-sm font-semibold">
          <ZapIcon className="size-4 text-muted-foreground" aria-hidden />
          {t("clusters.autoPublish")}
          <AutoPublishBadge cluster={cluster} />
        </h2>
        {status.checked_at ? (
          <span className="text-xs text-muted-foreground tabular-nums">{t("clusters.autoChecked", { time: formatDateTime(status.checked_at) })}</span>
        ) : null}
      </div>
      {resume.error ? (
        isConflict(resume.error) ? (
          <ConflictAlert
            onReload={() => {
              resume.reset();
              void queryClient.invalidateQueries({ queryKey: ["gateway-clusters"] });
            }}
          />
        ) : (
          <ErrorAlert error={resume.error} />
        )
      ) : null}
      {status.paused ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2.5 text-[13px] text-amber-900">
          <span>
            {t("clusters.autoPaused", { revision: status.paused.revision, time: formatDateTime(status.paused.at) })}
          </span>
          <Button size="sm" variant="outline" disabled={resume.isPending || !cluster.active} onClick={() => resume.mutate()}>
            {resume.isPending ? <Spinner /> : <PlayIcon />}
            {t("clusters.autoResume")}
          </Button>
        </div>
      ) : null}
      <dl className="grid gap-x-6 gap-y-2 text-[13px] sm:grid-cols-[max-content_1fr]">
        <dt className="text-muted-foreground">{t("clusters.autoLastPublished")}</dt>
        <dd>
          {status.last_published ? (
            <span className="flex flex-wrap items-baseline gap-x-2">
              <span className="font-mono font-medium">r{status.last_published.revision}</span>
              <span className="tabular-nums">{formatDateTime(status.last_published.at)}</span>
              {trigger.length ? (
                <span className="text-xs text-muted-foreground">{t("clusters.autoTrigger", { sources: trigger.join(", ") })}</span>
              ) : null}
            </span>
          ) : (
            <span className="text-muted-foreground">{t("clusters.autoNever")}</span>
          )}
        </dd>
        {status.pending_since ? (
          <>
            <dt className="text-muted-foreground">{t("clusters.autoPendingSince")}</dt>
            <dd className="tabular-nums">{formatDateTime(status.pending_since)}</dd>
          </>
        ) : null}
        <dt className="text-muted-foreground">{t("clusters.autoLastFailure")}</dt>
        <dd>
          {status.last_failure ? (
            <span className="flex flex-col gap-0.5">
              <span className="flex flex-wrap items-baseline gap-x-2">
                <code className="font-mono text-xs text-red-700">{status.last_failure.code}</code>
                <span className="tabular-nums text-muted-foreground">{formatDateTime(status.last_failure.at)}</span>
              </span>
              <span className="break-words">{status.last_failure.message}</span>
            </span>
          ) : (
            <span className="text-muted-foreground">{t("clusters.autoNoFailure")}</span>
          )}
        </dd>
      </dl>
      {status.state === "disabled" ? <p className="text-xs text-muted-foreground">{t("clusters.autoDisabledHint")}</p> : null}
    </section>
  );
}
