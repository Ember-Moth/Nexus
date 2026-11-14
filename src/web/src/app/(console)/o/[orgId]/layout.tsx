"use client";

import { useParams } from "next/navigation";
import type { ReactNode } from "react";

import { ConsoleShell } from "@/components/shell/console-shell";

export default function OrganizationLayout({ children }: { children: ReactNode }) {
  const { orgId } = useParams<{ orgId: string }>();
  return (
    <ConsoleShell key={orgId} kind="org" orgId={orgId}>
      {children}
    </ConsoleShell>
  );
}
