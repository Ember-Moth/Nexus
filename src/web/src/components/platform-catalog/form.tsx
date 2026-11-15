"use client";

import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useT } from "@/i18n/provider";
import { cn } from "@/lib/utils";

import { PcErrorAlert } from "./errors";

/** Label, control, hint and validation message stacked in one grid cell. */
export function Field({
  id,
  label,
  hint,
  error,
  children,
  className,
}: {
  id?: string;
  label: ReactNode;
  hint?: ReactNode;
  error?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("grid content-start gap-1.5", className)}>
      {id ? <Label htmlFor={id}>{label}</Label> : <span className="text-sm font-medium">{label}</span>}
      {children}
      {error ? (
        <span role="alert" className="text-xs text-destructive">
          {error}
        </span>
      ) : hint ? (
        <span className="text-xs text-muted-foreground">{hint}</span>
      ) : null}
    </div>
  );
}

/** Multi-select of enum values rendered as checkboxes; `locked` values stay checked. */
export function CheckboxGroup<T extends string>({
  idPrefix,
  options,
  value,
  onChange,
  label,
  locked = [],
  disabled,
  invalid,
  columns = 2,
}: {
  idPrefix: string;
  options: readonly T[];
  value: readonly T[];
  onChange: (next: T[]) => void;
  label: (option: T) => ReactNode;
  locked?: readonly T[];
  disabled?: boolean;
  invalid?: boolean;
  columns?: 1 | 2 | 3;
}) {
  const toggle = (option: T, checked: boolean) => {
    const next = checked ? [...value, option] : value.filter((v) => v !== option);
    onChange(options.filter((o) => next.includes(o)));
  };
  return (
    <div
      className={cn(
        "grid gap-x-4 gap-y-2 rounded-md border px-3 py-2.5",
        columns === 1 ? "grid-cols-1" : columns === 2 ? "sm:grid-cols-2" : "sm:grid-cols-3",
        invalid && "border-destructive",
      )}
    >
      {options.map((option) => {
        const id = `${idPrefix}-${option}`;
        const isLocked = locked.includes(option);
        return (
          <div key={option} className="flex items-center gap-2">
            <Checkbox
              id={id}
              checked={isLocked || value.includes(option)}
              disabled={disabled || isLocked}
              aria-invalid={invalid || undefined}
              onCheckedChange={(checked) => toggle(option, checked === true)}
            />
            <Label htmlFor={id} className="font-normal">
              {label(option)}
            </Label>
          </div>
        );
      })}
    </div>
  );
}

/** A labelled switch with a hint underneath, for boolean settings. */
export function SwitchRow({
  id,
  label,
  hint,
  checked,
  onChange,
  disabled,
  children,
}: {
  id: string;
  label: ReactNode;
  hint?: ReactNode;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  children?: ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4 rounded-md border px-3 py-2.5">
      <div className="flex min-w-0 flex-col gap-1">
        <Label htmlFor={id}>{label}</Label>
        {hint ? <span className="text-xs text-muted-foreground">{hint}</span> : null}
        {children}
      </div>
      <Switch id={id} checked={checked} disabled={disabled} onCheckedChange={onChange} className="mt-0.5" />
    </div>
  );
}

interface PagedOptions {
  error: Error | null;
  refetch: () => Promise<unknown>;
  hasNextPage: boolean;
  fetchNextPage: () => Promise<unknown>;
  isFetchingNextPage: boolean;
}

/** "Load more" for pickers backed by a cursor list; shows the error instead when loading failed. */
export function MoreOptions({ query }: { query: PagedOptions }) {
  const t = useT();
  if (query.error) return <PcErrorAlert error={query.error} onRetry={() => void query.refetch()} />;
  if (!query.hasNextPage) return null;
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className="w-fit"
      disabled={query.isFetchingNextPage}
      onClick={() => void query.fetchNextPage()}
    >
      {t(query.isFetchingNextPage ? "common.loading" : "pc.common.loadMoreOptions")}
    </Button>
  );
}

/** Small read-only key/value list used in dialogs and detail headers. */
export function Facts({ items, className }: { items: [ReactNode, ReactNode][]; className?: string }) {
  return (
    <dl className={cn("grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 rounded-md border px-3 py-2.5 text-[13px]", className)}>
      {items.map(([term, value], index) => (
        <div key={index} className="contents">
          <dt className="text-muted-foreground">{term}</dt>
          <dd className="min-w-0 break-words">{value}</dd>
        </div>
      ))}
    </dl>
  );
}
