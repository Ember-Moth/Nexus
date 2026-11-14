import { cn } from "@/lib/utils";
import { splitMoney } from "@/lib/money";

/**
 * Renders a decimal string as "1,234.56" followed by any further significant digits in a
 * muted tone, e.g. −0.00<muted>4318</muted>. Nothing is rounded; the exact value is in the title.
 */
export function Money({
  value,
  currency,
  signed,
  className,
  extraClassName,
}: {
  value: string | null | undefined;
  currency?: string;
  signed?: boolean;
  className?: string;
  extraClassName?: string;
}) {
  if (value === null || value === undefined || value === "") {
    return <span className={className}>—</span>;
  }
  const parts = splitMoney(value, { signed });
  return (
    <span className={cn("tabular-nums", className)} title={currency ? `${value} ${currency}` : value}>
      {currency ? <span className="mr-1 text-muted-foreground">{currency}</span> : null}
      {parts.main}
      {parts.extra ? (
        <span className={cn("font-normal text-muted-foreground/70", extraClassName)}>{parts.extra}</span>
      ) : null}
    </span>
  );
}
