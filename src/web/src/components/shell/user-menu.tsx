"use client";

import { useQueryClient } from "@tanstack/react-query";
import { EllipsisIcon, LanguagesIcon, LogOutIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { useErrorToast } from "@/components/feedback";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useI18n } from "@/i18n/provider";
import { getCsrfToken, setCsrfToken } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";

import { useWorkspace } from "./workspace";

/** Revokes the Nexus session (DELETE /auth/sessions/current via the BFF) and clears cookies. */
export function useSignOut() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const showError = useErrorToast();
  const [pending, setPending] = useState(false);
  const signOut = async () => {
    setPending(true);
    try {
      const response = await fetch("/api/session", {
        method: "DELETE",
        credentials: "same-origin",
        headers: { "x-csrf-token": await getCsrfToken().catch(() => "") },
      });
      if (!response.ok && response.status !== 401) {
        throw ApiError.fromResponse(response, await response.json().catch(() => null));
      }
      setCsrfToken(null);
      queryClient.clear();
      router.replace("/login");
    } catch (error) {
      showError(error);
      setPending(false);
    }
  };
  return { signOut, pending };
}

export function UserMenu() {
  const { t, maybeT, locale, setLocale } = useI18n();
  const { me } = useWorkspace();
  const { signOut, pending } = useSignOut();
  const roles = me.platform_roles.map((role) => maybeT(`roles.${role}`) ?? role).join(" · ");

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={t("user.menu")}
          className="flex w-full items-center gap-2.5 rounded-lg p-2 text-left outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50"
        >
          <span
            aria-hidden
            className="flex size-8 shrink-0 items-center justify-center rounded-full bg-muted text-sm font-semibold"
          >
            {me.user.login.slice(0, 1).toUpperCase()}
          </span>
          <span className="flex min-w-0 flex-1 flex-col">
            <span className="truncate text-sm font-medium">{me.user.login}</span>
            {roles ? <span className="truncate text-xs text-muted-foreground">{roles}</span> : null}
          </span>
          <EllipsisIcon className="size-4 text-muted-foreground" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="start" className="w-56">
        <DropdownMenuLabel className="font-normal">
          <span className="block text-sm font-medium">{me.user.login}</span>
          <span className="block font-mono text-xs text-muted-foreground">{me.user.id}</span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => setLocale(locale === "en" ? "zh-CN" : "en")}>
          <LanguagesIcon />
          {t("user.language")}
        </DropdownMenuItem>
        <DropdownMenuItem
          disabled={pending}
          onSelect={(event) => {
            event.preventDefault();
            void signOut();
          }}
        >
          <LogOutIcon />
          {pending ? t("user.signingOut") : t("user.signOut")}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
