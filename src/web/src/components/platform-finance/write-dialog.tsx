"use client";

import type { ReactNode } from "react";

import { ConflictAlert, ErrorAlert, Spinner } from "@/components/feedback";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useT } from "@/i18n/provider";
import { isConflict } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

/**
 * Confirmation dialog for a conditional command (post, cancel, revoke, …). The caller reads
 * the resource with its ETag when the dialog opens and passes the load state; a 412 from the
 * command shows ConflictAlert, whose reload re-reads the resource.
 */
export function ConfirmWriteDialog({
  open,
  onClose,
  title,
  description,
  children,
  submitLabel,
  destructive,
  loading,
  loadError,
  onRetryLoad,
  pending,
  error,
  onReload,
  disabled,
  onSubmit,
  className,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  submitLabel: ReactNode;
  destructive?: boolean;
  loading?: boolean;
  loadError?: unknown;
  onRetryLoad?: () => void;
  pending: boolean;
  error: unknown;
  onReload: () => void;
  disabled?: boolean;
  onSubmit: () => void;
  className?: string;
}) {
  const t = useT();
  const conflict = isConflict(error);
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className={cn("sm:max-w-[480px]", className)}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description ? <DialogDescription>{description}</DialogDescription> : null}
        </DialogHeader>
        {conflict ? <ConflictAlert onReload={onReload} /> : error ? <ErrorAlert error={error} /> : null}
        {loadError ? <ErrorAlert error={loadError} onRetry={onRetryLoad} /> : null}
        {loading ? <Skeleton className="h-24 w-full" /> : children}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            type="button"
            className={destructive ? "bg-destructive text-white hover:bg-destructive/90" : undefined}
            disabled={pending || loading || conflict || Boolean(loadError) || disabled}
            onClick={onSubmit}
          >
            {pending ? <Spinner /> : null}
            {submitLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
