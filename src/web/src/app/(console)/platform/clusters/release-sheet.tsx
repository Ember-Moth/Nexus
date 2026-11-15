"use client";

import { useQuery } from "@tanstack/react-query";
import { CheckIcon, CircleXIcon } from "lucide-react";

import { ErrorAlert, LoadingRows, Spinner } from "@/components/feedback";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { pollUntil } from "@/lib/api/hooks";
import type { GatewayCluster, ReleaseStatus } from "@/lib/api/types";
import { formatDateTime, shortId } from "@/lib/format";

/** Every active node applied the desired revision and it was written to the replicas. */
function settled(status: ReleaseStatus | undefined) {
  if (!status) return false;
  const nodes = status.nodes.filter((node) => node.active);
  return (
    status.distributed_revision === status.desired_revision &&
    nodes.every((node) => node.applied_revision === status.desired_revision)
  );
}

export function ReleaseSheet({
  cluster,
  revision,
  onClose,
}: {
  cluster: Pick<GatewayCluster, "id" | "name">;
  revision: string;
  onClose: () => void;
}) {
  const t = useT();
  const status = useQuery({
    queryKey: ["gateway-clusters", cluster.id, "release", revision],
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/gateway-clusters/{cluster_id}/releases/{revision}", {
            params: { path: { cluster_id: cluster.id, revision: Number(revision) } },
          }),
        )
      ).data,
    refetchInterval: pollUntil<ReleaseStatus>(settled),
  });
  const data = status.data;

  return (
    <Sheet open onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full gap-0 sm:max-w-[480px]" closeLabel={t("common.close")}>
        <SheetHeader className="border-b">
          <SheetTitle>{t("clusters.sheetTitle", { name: cluster.name, revision })}</SheetTitle>
          <SheetDescription>{t("clusters.subtitle")}</SheetDescription>
        </SheetHeader>
        <div className="flex flex-col gap-5 overflow-y-auto p-4">
          {status.error ? <ErrorAlert error={status.error} onRetry={() => void status.refetch()} /> : null}
          {!data && !status.error ? <LoadingRows rows={4} columns={2} /> : null}
          {data ? (
            <>
              <dl className="grid grid-cols-[96px_1fr] gap-y-2 text-[13px]">
                <dt className="text-muted-foreground">{t("clusters.columns.desired")}</dt>
                <dd className="font-mono">r{data.desired_revision}</dd>
                <dt className="text-muted-foreground">{t("clusters.columns.distributed")}</dt>
                <dd className="font-mono">r{data.distributed_revision}</dd>
                <dt className="text-muted-foreground">{t("clusters.sha")}</dt>
                <dd className="font-mono text-xs break-all">{data.sha256}</dd>
                <dt className="text-muted-foreground">{t("clusters.delivery")}</dt>
                <dd>
                  {data.distribution.delivered_at
                    ? t("clusters.deliveryValue", {
                        attempts: data.distribution.attempts,
                        time: `${formatDateTime(data.distribution.delivered_at)} UTC`,
                      })
                    : t("clusters.notDelivered")}
                </dd>
              </dl>
              <section className="flex flex-col gap-2">
                <h3 className="flex items-center gap-2 text-sm font-semibold">
                  {t("clusters.nodes")}
                  {!settled(data) ? (
                    <span className="flex items-center gap-1 text-xs font-normal text-muted-foreground">
                      <Spinner className="size-3" />
                      {t("clusters.polling")}
                    </span>
                  ) : null}
                </h3>
                {data.nodes.length === 0 ? (
                  <p className="text-sm text-muted-foreground">{t("clusters.noNodes")}</p>
                ) : (
                  <ul className="divide-y rounded-md border text-[13px]">
                    {data.nodes.map((node) => {
                      const ack = node.last_ack;
                      return (
                        <li key={node.node_id} className="flex flex-wrap items-center gap-3 px-3 py-2.5">
                          <code className="font-mono text-xs" title={node.node_id}>
                            {shortId(node.node_id)}
                          </code>
                          <span className="font-mono">r{node.applied_revision}</span>
                          <span className="flex-1" />
                          {ack ? (
                            <span className={ack.applied ? "flex items-center gap-1 text-emerald-700" : "flex items-center gap-1 text-red-700"}>
                              {ack.applied ? <CheckIcon className="size-3.5" /> : <CircleXIcon className="size-3.5" />}
                              {ack.applied
                                ? t("clusters.ackApplied", { revision: ack.revision })
                                : t("clusters.ackFailed", { revision: ack.revision, code: ack.error_code ?? "—" })}
                            </span>
                          ) : (
                            <span className="text-muted-foreground">{t("clusters.noAck")}</span>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )}
              </section>
            </>
          ) : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}
