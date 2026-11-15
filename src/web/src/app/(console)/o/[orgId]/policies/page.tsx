"use client";

import { PlusIcon, ShieldIcon } from "lucide-react";
import { useMemo, useState } from "react";

import { EmptyState } from "@/components/feedback";
import { PageContainer, PageHeader } from "@/components/page";
import { useOrgWorkspace } from "@/components/shell/workspace";
import { Button } from "@/components/ui/button";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useModelNames } from "@/components/workspace/lookups";
import { useI18n } from "@/i18n/provider";

import {
  InheritanceNote,
  MissingBaseline,
  PolicyDialog,
  type PolicyDialogTarget,
  PolicyTable,
  policyPermissions,
  useAccessPolicies,
  usePolicyNames,
} from "./policy-editor";

type Filter = "all" | "organization" | "project" | "key";
const FILTERS: Filter[] = ["all", "organization", "project", "key"];

export default function PoliciesPage() {
  const { t } = useI18n();
  const workspace = useOrgWorkspace();
  const { orgId } = workspace;
  const permissions = policyPermissions(workspace);
  const policies = useAccessPolicies(orgId, permissions.canView);
  const modelNames = useModelNames(workspace);
  const names = usePolicyNames(orgId, workspace, modelNames);
  const [filter, setFilter] = useState<Filter>("all");
  const [target, setTarget] = useState<PolicyDialogTarget | null>(null);
  const all = useMemo(() => policies.data?.items ?? [], [policies.data]);
  // Organization first, then projects, then keys; platform before customer within a scope.
  const shown = useMemo(() => {
    const rank = { organization: 0, project: 1, key: 2 } as Record<string, number>;
    return all
      .filter((p) => filter === "all" || p.scope_type === filter)
      .sort(
        (a, b) =>
          (rank[a.scope_type] ?? 3) - (rank[b.scope_type] ?? 3) ||
          (a.project_id ?? "").localeCompare(b.project_id ?? "") ||
          (a.layer === b.layer ? 0 : a.layer === "platform" ? -1 : 1),
      );
  }, [all, filter]);
  const baselineMissing = policies.data !== undefined && !all.some((p) => p.layer === "platform" && p.scope_type === "organization");

  if (!permissions.canView) {
    return (
      <PageContainer>
        <PageHeader title={t("ws.policies.title")} description={t("ws.policies.subtitle")} />
        <EmptyState icon={<ShieldIcon className="size-5" />} title={t("ws.policies.noAccess")} className="rounded-xl border" />
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader
        title={t("ws.policies.title")}
        description={t("ws.policies.subtitle")}
        actions={
          permissions.layers.length > 0 ? (
            <Button onClick={() => setTarget({ kind: "create" })} disabled={!policies.data}>
              <PlusIcon />
              {t("ws.policies.create")}
            </Button>
          ) : null
        }
      />
      <InheritanceNote />
      {baselineMissing ? <MissingBaseline /> : null}
      <ToggleGroup
        type="single"
        variant="outline"
        aria-label={t("ws.policies.filterLabel")}
        value={filter}
        onValueChange={(value) => value && setFilter(value as Filter)}
        className="self-start"
      >
        {FILTERS.map((value) => (
          <ToggleGroupItem key={value} value={value}>
            {t(`ws.policies.filters.${value}`)}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      <PolicyTable
        policies={shown}
        names={names}
        permissions={permissions}
        onEdit={(policy) => setTarget({ kind: "edit", policy })}
        loading={policies.isPending}
        error={policies.error}
        onRetry={() => void policies.refetch()}
        empty={
          <EmptyState
            icon={<ShieldIcon className="size-5" />}
            title={filter === "all" ? t("ws.policies.emptyTitle") : t("states.filteredEmpty")}
            description={filter === "all" ? t("ws.policies.emptyBody") : undefined}
          />
        }
      />
      {policies.data?.truncated ? <p className="text-xs text-muted-foreground">{t("ws.policies.truncated")}</p> : null}
      {target ? (
        <PolicyDialog
          key={target.kind === "edit" ? target.policy.id : "new"}
          orgId={orgId}
          target={target}
          policies={all}
          permissions={permissions}
          names={names}
          onClose={() => setTarget(null)}
        />
      ) : null}
    </PageContainer>
  );
}
