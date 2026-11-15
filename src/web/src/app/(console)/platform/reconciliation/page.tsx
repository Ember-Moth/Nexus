"use client";

import { useQuery } from "@tanstack/react-query";
import { FileStackIcon, InboxIcon, TriangleAlertIcon } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useState, type ReactNode } from "react";

import { CopyButton } from "@/components/copy-button";
import { EmptyState, ErrorAlert, LoadingRows } from "@/components/feedback";
import { Money } from "@/components/money";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { DetailList } from "@/components/platform-finance/field";
import { JsonDisclosure, JsonView } from "@/components/platform-finance/json-view";
import { Actor, OrgFilter, useOrgName, useUserLabels } from "@/components/platform-finance/scope";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useCursorList } from "@/lib/api/hooks";
import type { components } from "@/lib/api/schema";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

import { AccountPanel, StatementDialog } from "./account-actions";
import { ResolutionResult, ResolveForm } from "./resolve-form";

type ReviewCase = components["schemas"]["ReviewCase"];
type CaseDetail = components["schemas"]["ReviewCaseDetail"];
type CaseResolution = components["schemas"]["CaseResolution"];
type CaseState = ReviewCase["state"];

/** Filters and the selected case live in the URL so a case can be linked. */
function useUrlState() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const update = useCallback(
    (changes: Record<string, string | undefined>) => {
      const search = new URLSearchParams(params.toString());
      for (const [key, value] of Object.entries(changes)) {
        if (value) search.set(key, value);
        else search.delete(key);
      }
      const query = search.toString();
      router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
    },
    [params, pathname, router],
  );
  const state: CaseState = params.get("state") === "resolved" ? "resolved" : "open";
  return {
    orgId: params.get("org") ?? undefined,
    state,
    caseId: params.get("case") ?? undefined,
    update,
  };
}

function ReasonText({ reason }: { reason: string }) {
  const { maybeT } = useI18n();
  const label = maybeT(`pf.reconciliation.reasons.${reason}`);
  return label ? (
    <span title={reason}>{label}</span>
  ) : (
    <code className="font-mono text-[12.5px]">{reason}</code>
  );
}

function CaseQueue({
  orgId,
  state,
  selected,
  onSelect,
}: {
  orgId: string | undefined;
  state: CaseState;
  selected: string | undefined;
  onSelect: (id: string) => void;
}) {
  const { t } = useI18n();
  const orgName = useOrgName();
  const cases = useCursorList<ReviewCase>({
    queryKey: ["reconciliation-cases", "list", orgId ?? null, state],
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/reconciliation-cases", {
            params: { query: { organization_id: orgId, state, limit: 50, cursor } },
          }),
        )
      ).data,
    refetchInterval: 30_000,
  });

  return (
    <section aria-label={t("pf.reconciliation.queue")} className="flex min-w-0 flex-col overflow-hidden rounded-xl border bg-card">
      {cases.error && cases.items.length === 0 ? (
        <div className="p-3">
          <ErrorAlert error={cases.error} onRetry={() => void cases.refetch()} />
        </div>
      ) : cases.isPending ? (
        <LoadingRows rows={6} columns={2} />
      ) : cases.items.length === 0 ? (
        <EmptyState
          icon={<InboxIcon className="size-5" />}
          title={state === "open" ? t("pf.reconciliation.queueEmpty") : t("pf.reconciliation.resolvedEmpty")}
          description={state === "open" ? t("pf.reconciliation.queueEmptyBody") : undefined}
        />
      ) : (
        <ul className="flex flex-col divide-y">
          {cases.items.map((item) => (
            <li key={item.id}>
              <button
                type="button"
                onClick={() => onSelect(item.id)}
                aria-current={item.id === selected ? "true" : undefined}
                className={cn(
                  "flex w-full flex-col gap-1 px-4 py-3 text-left hover:bg-muted/50 focus-visible:bg-muted/50 focus-visible:outline-none",
                  item.id === selected && "bg-muted shadow-[inset_3px_0_0_var(--foreground)]",
                )}
              >
                <span className="flex items-start gap-2">
                  <span className="min-w-0 flex-1 text-[13px] font-medium">
                    <ReasonText reason={item.reason} />
                  </span>
                  {item.amount_at_risk ? (
                    <Money value={item.amount_at_risk} className="text-[13px] font-medium" />
                  ) : null}
                </span>
                <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                  <span className="rounded border px-1.5 py-px">{t(`pf.reconciliation.sources.${item.source_type}`)}</span>
                  <span className="truncate">{orgName(item.organization_id)}</span>
                  <span className="ml-auto tabular-nums">{formatDateTime(item.created_at, { seconds: false })}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {cases.error && cases.items.length > 0 ? (
        <div className="border-t p-3">
          <ErrorAlert error={cases.error} onRetry={() => void cases.refetch()} />
        </div>
      ) : null}
      {cases.items.length > 0 ? (
        <LoadMoreFooter
          count={cases.items.length}
          hasNext={Boolean(cases.hasNextPage)}
          loading={cases.isFetchingNextPage}
          onLoadMore={() => void cases.fetchNextPage()}
          note={t("pf.reconciliation.queueNote")}
        />
      ) : null}
    </section>
  );
}

function Section({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <h3 className="flex-1 text-[13px] font-semibold text-muted-foreground">{title}</h3>
        {aside}
      </div>
      {children}
    </section>
  );
}

function idCell(value: string | null | undefined) {
  if (!value) return "—";
  return (
    <span className="inline-flex items-center gap-1">
      <code className="font-mono text-xs break-all">{value}</code>
      <CopyButton value={value} />
    </span>
  );
}

function Decisions({ decisions }: { decisions: CaseDetail["decisions"] }) {
  const { t, maybeT } = useI18n();
  const userLabel = useUserLabels();
  if (decisions.length === 0) {
    return <p className="rounded-md border border-dashed px-3 py-3 text-sm text-muted-foreground">{t("pf.reconciliation.noDecisions")}</p>;
  }
  return (
    <ol className="flex flex-col gap-3 border-l pl-4">
      {decisions.map((decision) => (
        <li key={decision.id} className="relative flex flex-col gap-2">
          <span aria-hidden className="absolute top-1.5 -left-[21px] size-2.5 rounded-full border-2 border-background bg-foreground" />
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px]">
            <span className="font-medium">
              {maybeT(`pf.reconciliation.actions.${decision.action}`) ?? decision.action}
            </span>
            <span className="text-muted-foreground">·</span>
            <Actor id={decision.actor_id} label={userLabel} />
            <span className="ml-auto text-xs text-muted-foreground tabular-nums">
              {formatDateTime(decision.created_at)} {t("common.utc")}
            </span>
          </span>
          <DetailList
            items={[
              [t("pf.common.reason"), <span key="r" className="whitespace-pre-wrap">{decision.reason}</span>],
              [t("pf.common.evidence"), <span key="e" className="whitespace-pre-wrap">{decision.evidence}</span>],
            ]}
          />
          {Object.keys(decision.parameters).length ? (
            <JsonDisclosure title={t("pf.reconciliation.parameters")} value={decision.parameters} />
          ) : null}
          {Object.keys(decision.result).length ? (
            <JsonDisclosure title={t("pf.reconciliation.result")} value={decision.result} />
          ) : null}
        </li>
      ))}
    </ol>
  );
}

function CaseDetailPanel({ caseId }: { caseId: string }) {
  const { t } = useI18n();
  const orgName = useOrgName();
  const [result, setResult] = useState<CaseResolution | null>(null);
  const detail = useQuery({
    queryKey: ["reconciliation-cases", "detail", caseId],
    queryFn: async () => call(() => nexus.GET("/api/v1/reconciliation-cases/{case_id}", { params: { path: { case_id: caseId } } })),
    staleTime: 0,
  });

  if (detail.error) return <ErrorAlert error={detail.error} onRetry={() => void detail.refetch()} />;
  const data = detail.data?.data;
  if (!data) {
    return (
      <div className="flex flex-col gap-3 rounded-xl border p-5">
        <Skeleton className="h-6 w-1/2" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }
  // The detail response carries no ETag header; the case version is the conditional token.
  const ifMatch = detail.data?.etag ?? `"${data.version}"`;
  const name = orgName(data.organization_id);

  return (
    <article aria-labelledby="case-title" className="flex min-w-0 flex-col gap-5 rounded-xl border bg-card p-5">
      <header className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={data.state === "open" ? "warning" : "success"}>
            {t(`pf.common.states.${data.state}`)}
          </StatusBadge>
          <span className="rounded border px-1.5 py-px text-xs text-muted-foreground">
            {t(`pf.reconciliation.sources.${data.source_type}`)}
          </span>
          <span className="ml-auto text-xs text-muted-foreground">v{data.version}</span>
        </div>
        <h2 id="case-title" className="text-lg font-semibold">
          <ReasonText reason={data.reason} />
        </h2>
        {data.amount_at_risk ? (
          <p className="text-sm">
            <span className="text-muted-foreground">{t("pf.reconciliation.amountAtRisk")} </span>
            <Money value={data.amount_at_risk} className="font-semibold" />
          </p>
        ) : null}
      </header>
      <DetailList
        items={[
          [t("pf.common.organization"), name],
          [t("pf.reconciliation.sourceType"), t(`pf.reconciliation.sources.${data.source_type}`)],
          [t("pf.reconciliation.sourceId"), idCell(data.source_id)],
          [t("pf.reconciliation.requestId"), idCell(data.request_id)],
          [t("pf.common.createdAt"), `${formatDateTime(data.created_at)} ${t("common.utc")}`],
          [t("pf.reconciliation.resolvedAt"), data.resolved_at ? `${formatDateTime(data.resolved_at)} ${t("common.utc")}` : "—"],
          [t("pf.reconciliation.caseId"), idCell(data.id)],
        ]}
      />
      <AccountPanel orgId={data.organization_id} orgName={name} />
      <Section title={t("pf.reconciliation.evidence")}>
        <div className="rounded-md border p-3">
          <JsonView value={data.evidence} />
        </div>
      </Section>
      {data.request ? (
        <Section title={t("pf.reconciliation.request")}>
          <JsonDisclosure title={t("pf.reconciliation.requestSnapshot")} value={data.request} />
        </Section>
      ) : null}
      {data.usage_evidence && data.usage_evidence.length > 0 ? (
        <Section title={t("pf.reconciliation.usageEvidence")}>
          <ul className="flex flex-col gap-2">
            {data.usage_evidence.map((item) => (
              <li key={item.id} className="flex flex-col gap-2 rounded-md border p-3">
                <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                  <span className="tabular-nums">{formatDateTime(item.received_at)} {t("common.utc")}</span>
                  {item.rule_version ? (
                    <span>
                      {t("pf.reconciliation.ruleVersion")} <code className="font-mono">{item.rule_version}</code>
                    </span>
                  ) : null}
                  {item.review_reason ? (
                    <span className="text-amber-800">
                      <ReasonText reason={item.review_reason} />
                    </span>
                  ) : null}
                </span>
                {item.quantities ? <JsonView value={item.quantities} /> : null}
                <JsonDisclosure title={t("pf.reconciliation.envelope")} value={item.envelope} />
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
      <Section title={t("pf.reconciliation.history")}>
        <Decisions decisions={data.decisions} />
      </Section>
      {result ? <ResolutionResult result={result} /> : null}
      {data.state === "open" ? (
        <ResolveForm
          key={data.version}
          detail={data}
          ifMatch={ifMatch}
          onReload={() => void detail.refetch()}
          onResolved={(resolution) => {
            setResult(resolution);
            void detail.refetch();
          }}
        />
      ) : null}
    </article>
  );
}

function ReconciliationContent() {
  const { t } = useI18n();
  const { orgId, state, caseId, update } = useUrlState();
  const [statementOpen, setStatementOpen] = useState(false);

  return (
    <PageContainer className="max-w-[1440px]">
      <PageHeader
        title={t("pf.reconciliation.title")}
        description={t("pf.reconciliation.subtitle")}
        actions={
          <Button variant="outline" onClick={() => setStatementOpen(true)}>
            <FileStackIcon />
            {t("pf.reconciliation.statementAction")}
          </Button>
        }
      />
      <div className="flex flex-wrap items-center gap-2">
        <OrgFilter value={orgId} onChange={(next) => update({ org: next, case: undefined })} />
        <ToggleGroup
          type="single"
          variant="outline"
          value={state}
          onValueChange={(value) => value && update({ state: value === "open" ? undefined : value, case: undefined })}
          aria-label={t("pf.common.status")}
        >
          <ToggleGroupItem value="open">{t("pf.common.states.open")}</ToggleGroupItem>
          <ToggleGroupItem value="resolved">{t("pf.common.states.resolved")}</ToggleGroupItem>
        </ToggleGroup>
      </div>
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(300px,380px)_1fr]">
        <CaseQueue orgId={orgId} state={state} selected={caseId} onSelect={(id) => update({ case: id })} />
        {caseId ? (
          <CaseDetailPanel key={caseId} caseId={caseId} />
        ) : (
          <div className="rounded-xl border border-dashed">
            <EmptyState
              icon={<TriangleAlertIcon className="size-5" />}
              title={t("pf.reconciliation.pickCase")}
              description={t("pf.reconciliation.pickCaseBody")}
            />
          </div>
        )}
      </div>
      {statementOpen ? <StatementDialog defaultOrgId={orgId} onClose={() => setStatementOpen(false)} /> : null}
    </PageContainer>
  );
}

export default function ReconciliationPage() {
  return (
    <Suspense>
      <ReconciliationContent />
    </Suspense>
  );
}
