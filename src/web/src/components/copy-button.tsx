"use client";

import { CheckIcon, CopyIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { useT } from "@/i18n/provider";
import { cn } from "@/lib/utils";

export function useCopy(resetMs = 2000) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), resetMs);
    return () => clearTimeout(timer);
  }, [copied, resetMs]);
  const copy = async (text: string) => {
    await navigator.clipboard.writeText(text);
    setCopied(true);
  };
  return { copied, copy };
}

export function CopyButton({
  value,
  label,
  className,
  size = "icon-xs",
}: {
  value: string;
  label?: string;
  className?: string;
  size?: "icon-xs" | "icon-sm" | "sm";
}) {
  const t = useT();
  const { copied, copy } = useCopy();
  const text = copied ? t("common.copied") : (label ?? t("common.copy"));
  return (
    <Button
      type="button"
      variant="ghost"
      size={size}
      aria-label={text}
      title={text}
      className={cn(size === "sm" ? "" : "text-muted-foreground", className)}
      onClick={() => void copy(value)}
    >
      {copied ? <CheckIcon className="text-emerald-700" /> : <CopyIcon />}
      {size === "sm" ? text : null}
    </Button>
  );
}
