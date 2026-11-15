"use client";

import type { ReactNode } from "react";

import { Label } from "@/components/ui/label";
import { useI18n } from "@/i18n/provider";
import { cn } from "@/lib/utils";

/**
 * Form validation messages are short codes ("required", "precision", …) so that schemas stay
 * free of copy; this resolves them to pf.common.fieldErrors.* and falls back to the raw text.
 */
export function useFieldErrorText() {
  const { maybeT } = useI18n();
  return (code: string | undefined, vars?: Record<string, string | number>) =>
    code ? (maybeT(`pf.common.fieldErrors.${code}`, vars) ?? code) : undefined;
}

/** Label, control, then either the validation error or a hint. */
export function Field({
  id,
  label,
  hint,
  error,
  errorVars,
  children,
  className,
}: {
  id?: string;
  label: ReactNode;
  hint?: ReactNode;
  error?: string;
  errorVars?: Record<string, string | number>;
  children: ReactNode;
  className?: string;
}) {
  const describe = useFieldErrorText();
  const message = describe(error, errorVars);
  return (
    <div className={cn("grid min-w-0 gap-1.5", className)}>
      <Label htmlFor={id} className="text-[13px]">
        {label}
      </Label>
      {children}
      {message ? (
        <span role="alert" className="text-xs text-destructive">
          {message}
        </span>
      ) : hint ? (
        <span className="text-xs text-muted-foreground">{hint}</span>
      ) : null}
    </div>
  );
}

/** Read-only value shown next to editable fields (immutable attributes). */
export function ReadOnlyValue({ label, children, mono }: { label: ReactNode; children: ReactNode; mono?: boolean }) {
  return (
    <div className="grid min-w-0 gap-1.5">
      <span className="text-[13px] font-medium">{label}</span>
      <span
        className={cn(
          "flex min-h-9 items-center rounded-md border border-dashed bg-muted/40 px-2.5 text-sm break-all text-muted-foreground",
          mono && "font-mono text-xs",
        )}
      >
        {children}
      </span>
    </div>
  );
}

/** Two-column definition list used by detail panels and confirmation dialogs. */
export function DetailList({ items, className }: { items: [ReactNode, ReactNode][]; className?: string }) {
  return (
    <dl className={cn("grid grid-cols-[minmax(88px,max-content)_1fr] gap-x-4 gap-y-1.5 text-[13px]", className)}>
      {items.map(([label, value], index) => (
        <div key={index} className="contents">
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="min-w-0 break-words">{value}</dd>
        </div>
      ))}
    </dl>
  );
}
