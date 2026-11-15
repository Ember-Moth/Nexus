"use client";

import { AlertTriangleIcon, CircleAlertIcon, InboxIcon, Loader2Icon } from "lucide-react";
import type { ReactNode } from "react";
import { toast } from "sonner";

import { CopyButton } from "@/components/copy-button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/i18n/provider";
import { isApiError } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

/** Codes whose server message carries the specific reason; shown after the translation. */
const DETAILED_CODES = new Set(["invalid_snapshot", "invalid_catalog_config", "invalid_deployment", "invalid_gateway_configuration"]);

/** Localized description of an API error: known codes first, then the server message. */
export function useErrorText() {
  const { t, maybeT } = useI18n();
  return (error: unknown): string => {
    if (!isApiError(error)) return t("errors.network");
    if (error.status === 0) return t("errors.network");
    if (error.status === 412) return t("errors.conflictBody");
    const known = maybeT(`errors.codes.${error.code}`);
    // A 503 with a specific code (mail_unavailable, gateway_unavailable…) says more than the generic text.
    if (error.status === 503) return known ?? t("errors.unavailable");
    // For these codes the server message names the exact problem (e.g. which route is missing).
    if (known && DETAILED_CODES.has(error.code) && error.message) return `${known}：${error.message}`;
    return known ?? error.message;
  };
}

export function TraceId({ traceId, className }: { traceId?: string; className?: string }) {
  const { t } = useI18n();
  if (!traceId) return null;
  return (
    <span className={cn("inline-flex items-center gap-1 text-xs text-muted-foreground", className)}>
      {t("errors.traceId")}
      <code className="font-mono text-foreground">{traceId}</code>
      <CopyButton value={traceId} label={t("errors.copyTraceId")} />
    </span>
  );
}

/** Inline error with a copyable trace ID and an optional retry button. */
export function ErrorAlert({
  error,
  title,
  description,
  onRetry,
  className,
}: {
  error: unknown;
  title?: string;
  /** Replaces the text derived from the error code, e.g. when the caller knows better. */
  description?: string;
  onRetry?: () => void;
  className?: string;
}) {
  const { t } = useI18n();
  const text = useErrorText()(error);
  return (
    <Alert variant="destructive" className={className}>
      <CircleAlertIcon />
      <AlertTitle>{title ?? t("errors.title")}</AlertTitle>
      <AlertDescription className="gap-2">
        <span>{description ?? text}</span>
        <TraceId traceId={isApiError(error) ? error.traceId : undefined} />
        {onRetry ? (
          <Button type="button" variant="outline" size="sm" onClick={onRetry} className="mt-1">
            {t("common.retry")}
          </Button>
        ) : null}
      </AlertDescription>
    </Alert>
  );
}

/** Toast for failed actions; the trace ID is shown and can be copied. */
export function useErrorToast() {
  const { t } = useI18n();
  const describe = useErrorText();
  return (error: unknown, title?: string) => {
    const traceId = isApiError(error) ? error.traceId : undefined;
    toast.error(title ?? t("errors.title"), {
      description: (
        <span className="flex flex-col gap-1">
          <span>{describe(error)}</span>
          {traceId ? (
            <span className="text-xs">
              {t("errors.traceId")} <code className="font-mono">{traceId}</code>
            </span>
          ) : null}
        </span>
      ),
      action: traceId
        ? { label: t("errors.copyTraceId"), onClick: () => void navigator.clipboard.writeText(traceId) }
        : undefined,
      duration: 10_000,
    });
  };
}

/** 412: the resource changed since it was read. The caller reloads and re-applies. */
export function ConflictAlert({ onReload, children }: { onReload: () => void; children?: ReactNode }) {
  const { t } = useI18n();
  return (
    <Alert className="border-amber-200 bg-amber-50 text-amber-900">
      <AlertTriangleIcon className="text-amber-700" />
      <AlertTitle>{t("errors.conflictTitle")}</AlertTitle>
      <AlertDescription className="gap-2 text-amber-900/90">
        <span>{t("errors.conflictBody")}</span>
        {children}
        <Button type="button" size="sm" onClick={onReload} className="mt-1">
          {t("common.reload")}
        </Button>
      </AlertDescription>
    </Alert>
  );
}

export function EmptyState({
  title,
  description,
  icon,
  action,
  className,
}: {
  title: string;
  description?: string;
  icon?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center gap-2 px-6 py-12 text-center", className)}>
      <span className="mb-1 flex size-11 items-center justify-center rounded-lg border text-muted-foreground">
        {icon ?? <InboxIcon className="size-5" />}
      </span>
      <p className="font-semibold">{title}</p>
      {description ? <p className="max-w-sm text-sm text-muted-foreground">{description}</p> : null}
      {action ? <div className="mt-2 flex gap-2">{action}</div> : null}
    </div>
  );
}

export function LoadingRows({ rows = 5, columns = 4 }: { rows?: number; columns?: number }) {
  const { t } = useI18n();
  return (
    <div aria-busy="true" aria-label={t("states.loadingLabel")} className="divide-y">
      {Array.from({ length: rows }, (_, row) => (
        <div key={row} className="flex h-11 items-center gap-6 px-4">
          {Array.from({ length: columns }, (_, column) => (
            <Skeleton key={column} className="h-3 flex-1" style={{ maxWidth: `${50 + ((row + column) % 4) * 10}%` }} />
          ))}
        </div>
      ))}
    </div>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2Icon aria-hidden className={cn("size-4 animate-spin", className)} />;
}
