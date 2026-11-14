"use client";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useT } from "@/i18n/provider";
import type { Project, TimeBasis } from "@/lib/api/types";
import { formatDate, startOfUtcDay, startOfUtcMonth, utcInputToIso } from "@/lib/format";

export interface ReportFilterState {
  from: string;
  to: string;
  basis: TimeBasis;
  projectId: string;
}

export const ALL_PROJECTS = "__all";

export function defaultReportFilters(): ReportFilterState {
  return {
    from: formatDate(startOfUtcMonth()),
    // The end is exclusive: tomorrow 00:00 UTC keeps today in range.
    to: formatDate(startOfUtcDay(-1)),
    basis: "occurred_at",
    projectId: ALL_PROJECTS,
  };
}

/** Query parameters shared by requests, charges, ledger and usage (operations.md). */
export function reportQuery(filters: ReportFilterState) {
  return {
    start: utcInputToIso(filters.from) ?? undefined,
    end: utcInputToIso(filters.to) ?? undefined,
    time_basis: filters.basis,
    project_id: filters.projectId === ALL_PROJECTS ? undefined : filters.projectId,
  };
}

export function ReportFilters({
  value,
  onChange,
  projects,
  showBasis = true,
  allProjectsLabel,
}: {
  value: ReportFilterState;
  onChange: (value: ReportFilterState) => void;
  projects: Project[];
  showBasis?: boolean;
  allProjectsLabel: string;
}) {
  const t = useT();
  return (
    <div className="flex flex-wrap items-end gap-3">
      <div className="grid gap-1.5">
        <Label htmlFor="filter-from" className="text-xs text-muted-foreground">
          {t("wallet.from")}
        </Label>
        <Input
          id="filter-from"
          type="date"
          value={value.from}
          onChange={(event) => onChange({ ...value, from: event.target.value })}
          className="w-40"
        />
      </div>
      <div className="grid gap-1.5">
        <Label htmlFor="filter-to" className="text-xs text-muted-foreground">
          {t("wallet.to")}
        </Label>
        <Input
          id="filter-to"
          type="date"
          value={value.to}
          onChange={(event) => onChange({ ...value, to: event.target.value })}
          className="w-40"
        />
      </div>
      {showBasis ? (
        <ToggleGroup
          type="single"
          variant="outline"
          aria-label={t("wallet.basis")}
          value={value.basis}
          onValueChange={(basis) => basis && onChange({ ...value, basis: basis as TimeBasis })}
        >
          <ToggleGroupItem value="occurred_at">{t("wallet.basisOccurred")}</ToggleGroupItem>
          <ToggleGroupItem value="posted_at">{t("wallet.basisPosted")}</ToggleGroupItem>
        </ToggleGroup>
      ) : null}
      <Select value={value.projectId} onValueChange={(projectId) => onChange({ ...value, projectId })}>
        <SelectTrigger aria-label={t("wallet.project")} className="min-w-48">
          <span className="text-muted-foreground">{t("wallet.project")}</span>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL_PROJECTS}>{allProjectsLabel}</SelectItem>
          {projects.map((project) => (
            <SelectItem key={project.id} value={project.id}>
              {project.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <span className="pb-2 text-xs text-muted-foreground">{t("wallet.rangeHint")}</span>
    </div>
  );
}
