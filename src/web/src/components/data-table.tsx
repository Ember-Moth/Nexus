"use client";

import {
  flexRender,
  getCoreRowModel,
  useReactTable,
  type ColumnDef,
  type RowData,
} from "@tanstack/react-table";
import type { ReactNode } from "react";

import { ErrorAlert, LoadingRows } from "@/components/feedback";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";

declare module "@tanstack/react-table" {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData extends RowData, TValue> {
    className?: string;
    align?: "right";
    /** Keep the column visible at the right edge while the table scrolls sideways. */
    sticky?: "right";
  }
}

/** Columns with id "actions" stay pinned so row actions are clickable without scrolling. */
function isSticky(column: { id: string; columnDef: { meta?: { sticky?: "right" } } }): boolean {
  return column.id === "actions" || column.columnDef.meta?.sticky === "right";
}

// Opaque versions of the header tint and row hover, so scrolled content does not show through.
const STICKY_HEAD = "sticky right-0 z-10 bg-[color-mix(in_oklab,var(--muted)_50%,var(--card))] shadow-[-8px_0_8px_-8px_rgb(0_0_0/0.15)]";
const STICKY_CELL =
  "sticky right-0 z-10 bg-card shadow-[-8px_0_8px_-8px_rgb(0_0_0/0.15)] [tr:hover>&]:bg-[color-mix(in_oklab,var(--muted)_50%,var(--card))]";

/**
 * TanStack Table rendered with shadcn table primitives. Pagination is cursor based and lives
 * in the footer slot (see LoadMoreFooter); sorting and filtering happen on the server.
 */
export function DataTable<T>({
  columns,
  data,
  loading,
  error,
  onRetry,
  empty,
  footer,
  getRowId,
  minWidth = 720,
  rowClassName,
  caption,
}: {
  columns: ColumnDef<T, unknown>[];
  data: T[];
  loading?: boolean;
  error?: unknown;
  onRetry?: () => void;
  empty?: ReactNode;
  footer?: ReactNode;
  getRowId?: (row: T) => string;
  minWidth?: number;
  rowClassName?: (row: T) => string | undefined;
  caption?: string;
}) {
  // TanStack Table returns non-memoizable functions; the React Compiler skips this component.
  // eslint-disable-next-line react-hooks/incompatible-library
  const table = useReactTable({
    data,
    columns,
    getCoreRowModel: getCoreRowModel(),
    getRowId: getRowId ? (row) => getRowId(row) : undefined,
  });

  let body: ReactNode;
  if (error && data.length === 0) {
    body = (
      <div className="p-4">
        <ErrorAlert error={error} onRetry={onRetry} />
      </div>
    );
  } else if (loading && data.length === 0) {
    body = <LoadingRows columns={Math.min(columns.length, 5)} />;
  } else if (data.length === 0) {
    body = empty ?? null;
  }

  return (
    <div className="overflow-hidden rounded-xl border bg-card">
      <div className="overflow-x-auto">
        <Table style={{ minWidth }}>
          {caption ? <caption className="sr-only">{caption}</caption> : null}
          <TableHeader className="bg-muted/50">
            {table.getHeaderGroups().map((group) => (
              <TableRow key={group.id} className="hover:bg-transparent">
                {group.headers.map((header) => (
                  <TableHead
                    key={header.id}
                    className={cn(
                      "h-10 px-4 text-xs font-medium text-muted-foreground",
                      header.column.columnDef.meta?.align === "right" && "text-right",
                      isSticky(header.column) && STICKY_HEAD,
                      header.column.columnDef.meta?.className,
                    )}
                  >
                    {header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}
                  </TableHead>
                ))}
              </TableRow>
            ))}
          </TableHeader>
          {body ? null : (
            <TableBody>
              {table.getRowModel().rows.map((row) => (
                <TableRow key={row.id} className={rowClassName?.(row.original)}>
                  {row.getVisibleCells().map((cell) => (
                    <TableCell
                      key={cell.id}
                      className={cn(
                        "h-12 px-4 text-[13px]",
                        cell.column.columnDef.meta?.align === "right" && "text-right",
                        isSticky(cell.column) && STICKY_CELL,
                        cell.column.columnDef.meta?.className,
                      )}
                    >
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          )}
        </Table>
      </div>
      {body}
      {error && data.length > 0 ? (
        <div className="border-t p-4">
          <ErrorAlert error={error} onRetry={onRetry} />
        </div>
      ) : null}
      {footer}
    </div>
  );
}
