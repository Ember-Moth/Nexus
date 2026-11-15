"use client";

import { useQueries } from "@tanstack/react-query";
import { CopyPlusIcon, PlusIcon, TagIcon } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useMemo, useState } from "react";

import { EmptyState, Spinner } from "@/components/feedback";
import { PageContainer, PageHeader } from "@/components/page";
import { PcErrorAlert } from "@/components/platform-catalog/errors";
import { MoreOptions } from "@/components/platform-catalog/form";
import { modelDimensions } from "@/components/platform-catalog/labels";
import { useDeploymentDetail, useDeployments, useModelDetail, useModels } from "@/components/platform-catalog/queries";
import type { PriceKind, PriceVersion } from "@/components/platform-catalog/types";
import {
  latestStart,
  PhaseBadge,
  phasesOf,
  sortVersions,
  useNow,
  useRangeText,
  VersionTimeline,
} from "@/components/platform-catalog/version-timeline";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useCursorList } from "@/lib/api/hooks";
import { shortId } from "@/lib/format";
import { formatMoney } from "@/lib/money";

import { CreatePriceDialog, dimensionLabel, DraftPanel, fetchPrice, pricesKey, RatesTable } from "./price-form";

/** The selected model (sale) or deployment (cost) lives in ?scope= so the page can be linked. */
function useScopeParam(): [string | undefined, (id: string) => void] {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const scopeId = params.get("scope") ?? undefined;
  const set = useCallback(
    (next: string) => {
      const search = new URLSearchParams(params.toString());
      search.set("scope", next);
      router.replace(`${pathname}?${search.toString()}`, { scroll: false });
    },
    [params, pathname, router],
  );
  return [scopeId, set];
}

/**
 * Sale prices (scope = public model) and cost prices (scope = deployment) share this page:
 * a version timeline on the left, the selected version on the right (drafts are edited and
 * published in place). Versions come from GET /{kind}-price-versions?scope_id=; drafts created
 * in this session are also read one by one, so the page stays usable while the list fails.
 */
export function PriceVersionsPage({ kind }: { kind: PriceKind }) {
  return (
    <Suspense>
      <PriceVersionsView kind={kind} />
    </Suspense>
  );
}

type CreateTarget = { source: PriceVersion | null };

function PriceVersionsView({ kind }: { kind: PriceKind }) {
  const t = useT();
  const now = useNow();
  const range = useRangeText();
  const [scopeId, setScopeId] = useScopeParam();
  const [sessionIds, setSessionIds] = useState<string[]>([]);
  const [selection, setSelection] = useState<{ scopeId: string; id: string } | null>(null);
  const [creating, setCreating] = useState<CreateTarget | null>(null);

  const models = useModels();
  const deployments = useDeployments({}, { enabled: kind === "cost" });
  const deploymentDetail = useDeploymentDetail(kind === "cost" ? scopeId : undefined);
  const deployment =
    kind === "cost" ? (deploymentDetail.data?.data ?? deployments.items.find((d) => d.id === scopeId)) : undefined;
  const modelId = kind === "sale" ? scopeId : deployment?.model_id;
  const model = useModelDetail(modelId);
  const scopeModel = model.data?.data;
  const dimensions = useMemo(() => (scopeModel ? modelDimensions(scopeModel) : []), [scopeModel]);
  const modelName = (id: string) => models.items.find((m) => m.id === id)?.public_name ?? shortId(id);

  const list = useCursorList<PriceVersion>({
    queryKey: pricesKey(kind, "list", scopeId ?? null),
    enabled: Boolean(scopeId),
    fetchPage: async (cursor) => {
      const query = { scope_id: scopeId, limit: 50, cursor };
      return kind === "sale"
        ? (await call(() => nexus.GET("/api/v1/sale-price-versions", { params: { query } }))).data
        : (await call(() => nexus.GET("/api/v1/cost-price-versions", { params: { query } }))).data;
    },
  });

  const session = useQueries({
    queries: sessionIds.map((id) => ({ queryKey: pricesKey(kind, "detail", id), queryFn: () => fetchPrice(kind, id) })),
    combine: (results) => results.flatMap((q) => (q.data ? [q.data.data] : [])),
  });

  const versions = useMemo(() => {
    const byId = new Map<string, PriceVersion>();
    for (const item of list.items) byId.set(item.id, item);
    // A fresher single read (after edits) wins over the list row.
    for (const item of session) if (item.scope_id === scopeId) byId.set(item.id, item);
    return [...byId.values()];
  }, [list.items, session, scopeId]);

  const phases = phasesOf(versions, now);
  const ordered = sortVersions(versions, phases);
  const live = versions.find((v) => phases.get(v.id)?.phase === "live") ?? null;
  const notBefore = latestStart(versions);
  const selectedId = selection && selection.scopeId === scopeId ? selection.id : undefined;
  const selected = ordered.find((v) => v.id === selectedId) ?? ordered[0];
  const selectedPhase = selected ? phases.get(selected.id) : undefined;
  const defaultCurrency =
    live?.currency ?? ordered.find((v) => v.state !== "draft")?.currency ?? ordered[0]?.currency ?? "USD";
  const scopeName = kind === "sale" ? scopeModel?.public_name : deployment?.name;

  const summary = (item: PriceVersion) => {
    const input = item.rates.input_uncached_tokens?.unit_price;
    const output = item.rates.output_tokens?.unit_price;
    return input && output ? t("pc.prices.summary", { input: formatMoney(input), output: formatMoney(output) }) : null;
  };

  const scopeSelect =
    kind === "sale" ? (
      <Select value={scopeId ?? ""} onValueChange={setScopeId}>
        <SelectTrigger aria-label={t("pc.prices.sale.scope")} className="min-w-64">
          <span className="text-muted-foreground">{t("pc.prices.sale.scope")}</span>
          <SelectValue placeholder={t("pc.prices.sale.selectScope")}>
            {scopeId ? <span className="font-mono">{scopeModel?.public_name ?? modelName(scopeId)}</span> : undefined}
          </SelectValue>
        </SelectTrigger>
        <SelectContent>
          {models.items.map((m) => (
            <SelectItem key={m.id} value={m.id}>
              <span className="font-mono">{m.public_name}</span>
              {m.status !== "active" ? <span className="text-muted-foreground">· {t(`pc.enums.status.${m.status}`)}</span> : null}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    ) : (
      <Select value={scopeId ?? ""} onValueChange={setScopeId}>
        <SelectTrigger aria-label={t("pc.prices.cost.scope")} className="min-w-72">
          <span className="text-muted-foreground">{t("pc.prices.cost.scope")}</span>
          <SelectValue placeholder={t("pc.prices.cost.selectScope")}>
            {scopeId ? (deployment?.name ?? shortId(scopeId)) : undefined}
          </SelectValue>
        </SelectTrigger>
        <SelectContent>
          {deployments.items.map((d) => (
            <SelectItem key={d.id} value={d.id}>
              {d.name}
              <span className="font-mono text-muted-foreground">· {modelName(d.model_id)}</span>
              {d.status !== "active" ? <span className="text-muted-foreground">· {t(`pc.enums.status.${d.status}`)}</span> : null}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );

  return (
    <PageContainer>
      <PageHeader
        title={
          scopeName ? (
            <>
              {t(`pc.prices.${kind}.title`)} · <span className="font-mono text-[21px]">{scopeName}</span>
            </>
          ) : (
            t(`pc.prices.${kind}.title`)
          )
        }
        description={t(`pc.prices.${kind}.subtitle`)}
        actions={
          <Button
            variant={live ? "outline" : "default"}
            disabled={!scopeId || !scopeModel}
            onClick={() => setCreating({ source: live })}
          >
            {live ? <CopyPlusIcon /> : <PlusIcon />}
            {live ? t("pc.prices.createFromLive") : t("pc.prices.create")}
          </Button>
        }
      />
      <div className="flex flex-wrap items-center gap-3">
        {scopeSelect}
        <MoreOptions query={kind === "sale" ? models : deployments} />
      </div>
      {!scopeId ? (
        <div className="rounded-xl border bg-card">
          <EmptyState icon={<TagIcon className="size-5" />} title={t(`pc.prices.${kind}.pickPrompt`)} />
        </div>
      ) : (
        <>
          {deploymentDetail.error ? (
            <PcErrorAlert error={deploymentDetail.error} onRetry={() => void deploymentDetail.refetch()} />
          ) : model.error ? (
            <PcErrorAlert error={model.error} onRetry={() => void model.refetch()} />
          ) : null}
          {scopeModel ? (
            <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
              <span>{t("pc.prices.scopeModel", { model: scopeModel.public_name })}</span>
              {dimensions.map((d) => (
                <Badge key={d} variant="outline" className="font-normal">
                  {dimensionLabel(t, d)}
                </Badge>
              ))}
            </div>
          ) : null}
          {list.error ? (
            <PcErrorAlert error={list.error} title={t("pc.prices.listError")} onRetry={() => void list.refetch()} />
          ) : null}
          <div className="flex flex-wrap items-start gap-5">
            <section
              aria-labelledby="timeline-title"
              className="max-w-[360px] min-w-0 flex-[1_1_280px] overflow-hidden rounded-xl border"
            >
              <div className="flex items-baseline justify-between gap-2 border-b px-4 py-3">
                <h2 id="timeline-title" className="text-[15px] font-semibold">
                  {t("pc.timeline.title")}
                </h2>
                <span className="text-xs text-muted-foreground">{t("pc.timeline.utcNote")}</span>
              </div>
              {list.isPending && !list.error ? (
                <div className="flex justify-center py-8">
                  <Spinner />
                </div>
              ) : versions.length === 0 ? (
                <EmptyState
                  className="py-8"
                  icon={<TagIcon className="size-5" />}
                  title={t("pc.timeline.noVersions")}
                  description={t("pc.timeline.noVersionsBody")}
                />
              ) : (
                <VersionTimeline
                  className="p-2"
                  items={versions}
                  now={now}
                  selectedId={selected?.id}
                  onSelect={(item) => scopeId && setSelection({ scopeId, id: item.id })}
                  title={(item) => (
                    <>
                      {item.enabled === false ? <StatusBadge tone="warning">{t("pc.prices.disabled")}</StatusBadge> : null}
                      {list.error && sessionIds.includes(item.id) ? (
                        <Badge variant="outline" className="font-normal">
                          {t("pc.prices.sessionBadge")}
                        </Badge>
                      ) : null}
                    </>
                  )}
                  summary={summary}
                />
              )}
              {list.hasNextPage ? (
                <div className="border-t p-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    className="w-full"
                    disabled={list.isFetchingNextPage}
                    onClick={() => void list.fetchNextPage()}
                  >
                    {list.isFetchingNextPage ? t("common.loading") : t("common.loadMore")}
                  </Button>
                </div>
              ) : null}
            </section>
            <div className="flex min-w-0 flex-[999_1_520px] flex-col gap-4">
              {selected && selectedPhase?.phase === "draft" ? (
                <DraftPanel
                  key={selected.id}
                  kind={kind}
                  draft={selected}
                  live={live}
                  dimensions={dimensions}
                  notBefore={notBefore}
                  now={now}
                />
              ) : selected && selectedPhase ? (
                <section aria-labelledby="version-title" className="overflow-hidden rounded-xl border">
                  <div className="flex flex-wrap items-center gap-3 border-b px-4 py-3">
                    <h2 id="version-title" className="text-[15px] font-semibold">
                      {t("pc.prices.versionTitle")}{" "}
                      <code className="font-mono text-[13px] font-normal text-muted-foreground">{shortId(selected.id)}</code>
                    </h2>
                    <PhaseBadge phase={selectedPhase.phase} />
                    {selected.enabled === false ? <StatusBadge tone="warning">{t("pc.prices.disabled")}</StatusBadge> : null}
                    <span className="text-xs text-muted-foreground tabular-nums">{range(selected, selectedPhase)}</span>
                    <span className="flex-1" />
                    <Button variant="outline" size="sm" disabled={!scopeModel} onClick={() => setCreating({ source: selected })}>
                      <CopyPlusIcon />
                      {t("pc.prices.copy")}
                    </Button>
                  </div>
                  <RatesTable version={selected} live={live} />
                </section>
              ) : (
                <div className="rounded-xl border">
                  <EmptyState title={t("pc.prices.noSelection")} />
                </div>
              )}
            </div>
          </div>
        </>
      )}
      {creating && scopeId ? (
        <CreatePriceDialog
          key={creating.source?.id ?? "new"}
          kind={kind}
          scopeId={scopeId}
          source={creating.source}
          live={live}
          dimensions={dimensions}
          defaultCurrency={defaultCurrency}
          onCreated={(created) => {
            setSessionIds((ids) => (ids.includes(created.id) ? ids : [...ids, created.id]));
            setSelection({ scopeId, id: created.id });
          }}
          onClose={() => setCreating(null)}
        />
      ) : null}
    </PageContainer>
  );
}
