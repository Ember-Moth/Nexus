"use client";

import { ChevronRightIcon } from "lucide-react";
import type { ReactNode } from "react";

import { CopyButton } from "@/components/copy-button";
import { useT } from "@/i18n/provider";
import { cn } from "@/lib/utils";

import { UUID_PATTERN } from "./validation";

type Json = string | number | boolean | null | Json[] | { [key: string]: Json };

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

function Scalar({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="text-muted-foreground">null</span>;
  if (typeof value === "boolean") return <code className="font-mono text-xs">{String(value)}</code>;
  if (typeof value === "number") return <span className="font-mono text-xs tabular-nums">{value}</span>;
  const text = String(value);
  if (UUID_PATTERN.test(text)) {
    return (
      <span className="inline-flex items-center gap-1">
        <code className="font-mono text-xs break-all">{text}</code>
        <CopyButton value={text} />
      </span>
    );
  }
  return <span className="font-mono text-xs break-all whitespace-pre-wrap">{text}</span>;
}

function Node({ value, depth }: { value: unknown; depth: number }): ReactNode {
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="font-mono text-xs text-muted-foreground">[]</span>;
    if (value.every((item) => !isRecord(item) && !Array.isArray(item))) {
      return (
        <span className="flex flex-wrap gap-1">
          {value.map((item, index) => (
            <span key={index} className="rounded border bg-muted/40 px-1.5">
              <Scalar value={item} />
            </span>
          ))}
        </span>
      );
    }
    return (
      <ol className="flex flex-col gap-2">
        {value.map((item, index) => (
          <li key={index} className="rounded-md border p-2">
            <Node value={item} depth={depth + 1} />
          </li>
        ))}
      </ol>
    );
  }
  if (isRecord(value)) {
    const entries = Object.entries(value);
    if (entries.length === 0) return <span className="font-mono text-xs text-muted-foreground">{"{}"}</span>;
    if (depth >= 4) {
      return <pre className="overflow-x-auto font-mono text-xs whitespace-pre-wrap">{JSON.stringify(value, null, 2)}</pre>;
    }
    return (
      <dl className="grid grid-cols-[minmax(96px,max-content)_1fr] gap-x-3 gap-y-1">
        {entries.map(([key, item]) => (
          <div key={key} className="contents">
            <dt className="pt-px font-mono text-xs text-muted-foreground">{key}</dt>
            <dd className={cn("min-w-0", (isRecord(item) || Array.isArray(item)) && "border-l pl-2")}>
              <Node value={item} depth={depth + 1} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  return <Scalar value={value} />;
}

/** Generic, read-only rendering of JSON evidence: nested key/value lists, UUIDs copyable. */
export function JsonView({ value, className }: { value: unknown; className?: string }) {
  return (
    <div className={cn("min-w-0 text-[13px]", className)}>
      <Node value={value as Json} depth={0} />
    </div>
  );
}

/** Collapsible JSON section; closed by default for large or secondary payloads. */
export function JsonDisclosure({
  title,
  value,
  defaultOpen,
}: {
  title: ReactNode;
  value: unknown;
  defaultOpen?: boolean;
}) {
  const t = useT();
  return (
    <details className="group rounded-md border" open={defaultOpen}>
      <summary className="flex cursor-pointer list-none items-center gap-1.5 px-3 py-2 text-[13px] font-medium select-none">
        <ChevronRightIcon aria-hidden className="size-4 text-muted-foreground transition-transform group-open:rotate-90" />
        {title}
        {/* Copying must not toggle the disclosure. */}
        <span className="ml-auto" onClick={(event) => event.preventDefault()}>
          <CopyButton value={JSON.stringify(value, null, 2)} label={t("pf.common.copyJson")} />
        </span>
      </summary>
      <div className="border-t p-3">
        <JsonView value={value} />
      </div>
    </details>
  );
}
