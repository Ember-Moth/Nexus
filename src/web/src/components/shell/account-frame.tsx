"use client";

import { ArrowLeftIcon } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { ErrorAlert } from "@/components/feedback";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useT } from "@/i18n/provider";
import { useMe } from "@/lib/api/hooks";

import { useSignOut } from "./user-menu";

/** Signed-in pages that belong to the person rather than an organization (account, invitations). */
export function AccountFrame({ children }: { children: ReactNode }) {
  const t = useT();
  const me = useMe();
  const { signOut, pending } = useSignOut();
  return (
    <div className="flex min-h-screen flex-col bg-muted/30">
      <header className="flex h-14 items-center gap-3 border-b bg-background px-4 md:px-8">
        <span aria-hidden className="flex size-7 items-center justify-center rounded-md bg-primary text-sm font-bold text-primary-foreground">
          N
        </span>
        <Link href="/" className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
          <ArrowLeftIcon className="size-4" aria-hidden />
          {t("id.account.back")}
        </Link>
        <span className="flex-1" />
        {me.data ? <span className="text-sm text-muted-foreground">{me.data.user.login}</span> : null}
        <Button variant="ghost" size="sm" disabled={pending} onClick={() => void signOut()}>
          {t("user.signOut")}
        </Button>
      </header>
      <main id="main" className="mx-auto flex w-full max-w-2xl flex-col gap-5 px-4 py-8">
        {me.error ? <ErrorAlert error={me.error} onRetry={() => void me.refetch()} /> : me.isPending ? <Skeleton className="h-40 w-full" /> : children}
      </main>
    </div>
  );
}
