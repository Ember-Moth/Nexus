"use client";

import { ConstructionIcon } from "lucide-react";
import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { useT, type MessageKey } from "@/i18n/provider";
import { cn } from "@/lib/utils";

export function PageContainer({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("flex w-full max-w-[1280px] flex-col gap-5 px-4 py-6 md:px-8", className)}>{children}</div>;
}

export function PageHeader({
  title,
  description,
  actions,
  badge,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  badge?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end gap-4">
      <div className="flex min-w-[240px] flex-1 flex-col gap-1.5">
        <div className="flex items-center gap-2.5">
          <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
          {badge}
        </div>
        {description ? <p className="text-sm text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap gap-2">{actions}</div> : null}
    </div>
  );
}

/**
 * Placeholder for pages that are designed but not implemented yet. Lists the API operations
 * the page will use so the gap is visible in the product, not only in the README.
 */
export function SkeletonPage({ title, description, endpoints }: {
  title: MessageKey;
  description?: string;
  endpoints: string[];
}) {
  const t = useT();
  return (
    <PageContainer>
      <PageHeader
        title={t(title)}
        description={description}
        badge={<Badge variant="outline">{t("skeleton.badge")}</Badge>}
      />
      <Card>
        <CardContent className="flex gap-3">
          <ConstructionIcon className="mt-0.5 size-5 shrink-0 text-muted-foreground" aria-hidden />
          <div className="flex flex-col gap-2 text-sm">
            <p>{t("skeleton.body")}</p>
            <ul className="flex flex-col gap-1">
              {endpoints.map((endpoint) => (
                <li key={endpoint}>
                  <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{endpoint}</code>
                </li>
              ))}
            </ul>
          </div>
        </CardContent>
      </Card>
    </PageContainer>
  );
}

export function LoadMoreFooter({
  count,
  hasNext,
  loading,
  onLoadMore,
  note,
}: {
  count: number;
  hasNext: boolean;
  loading: boolean;
  onLoadMore: () => void;
  note?: string;
}) {
  const t = useT();
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t px-4 py-3 text-sm text-muted-foreground">
      <span>
        {note ?? t("common.noTotal")} · {t("common.loadedCount", { count })}
      </span>
      {hasNext ? (
        <button
          type="button"
          onClick={onLoadMore}
          disabled={loading}
          className="h-8 rounded-md border bg-background px-3 font-medium text-foreground hover:bg-muted disabled:opacity-50"
        >
          {loading ? t("common.loading") : t("common.loadMore")}
        </button>
      ) : null}
    </div>
  );
}
