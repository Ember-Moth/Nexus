"use client";

import { useQuery } from "@tanstack/react-query";
import { Building2Icon, CheckIcon, ChevronsUpDownIcon, ShieldIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { StatusBadge } from "@/components/status-badge";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { cn } from "@/lib/utils";

import { membershipRole, useWorkspace } from "./workspace";

function Initial({ text, platform, className }: { text: string; platform?: boolean; className?: string }) {
  return (
    <span
      aria-hidden
      className={cn(
        "flex size-8 shrink-0 items-center justify-center rounded-lg text-sm font-semibold text-primary-foreground",
        platform ? "bg-zinc-700" : "bg-primary",
        className,
      )}
    >
      {platform ? <ShieldIcon className="size-4" /> : text.slice(0, 1)}
    </span>
  );
}

export function WorkspaceSwitcher({ onNavigate }: { onNavigate?: () => void }) {
  const { t, maybeT } = useI18n();
  const workspace = useWorkspace();
  const router = useRouter();
  const [open, setOpen] = useState(false);

  // GET /organizations returns only what the caller may access (everything for platform roles).
  const organizations = useQuery({
    queryKey: ["organizations", "switcher"],
    queryFn: async () =>
      (await call(() => nexus.GET("/api/v1/organizations", { params: { query: { limit: 100 } } }))).data,
    enabled: open || workspace.kind === "org",
  });

  const isPlatform = workspace.kind === "platform";
  const roleLabel = (role?: string) => (role ? (maybeT(`roles.${role}`) ?? role) : "");
  const name = isPlatform ? t("switcher.platformName") : (workspace.organization?.name ?? "…");
  const sub = isPlatform
    ? workspace.platformRoles.map(roleLabel).join(" · ")
    : workspace.orgRole
      ? t("switcher.workspaceSub", { role: roleLabel(workspace.orgRole) })
      : t("switcher.platformViewer");

  const go = (href: string) => {
    setOpen(false);
    onNavigate?.();
    router.push(href);
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={t("switcher.title")}
          className="flex w-full items-center gap-2.5 rounded-lg p-2 text-left outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50 aria-expanded:bg-muted"
        >
          <Initial text={name} platform={isPlatform} />
          <span className="flex min-w-0 flex-1 flex-col">
            <span className="truncate text-sm font-semibold">{name}</span>
            <span className="truncate text-xs text-muted-foreground">{sub}</span>
          </span>
          <ChevronsUpDownIcon className="size-4 text-muted-foreground" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[300px] p-0">
        <Command>
          <CommandInput placeholder={t("switcher.search")} />
          <CommandList>
            <CommandEmpty>{t("switcher.noOrganizations")}</CommandEmpty>
            {workspace.platformRoles.length > 0 ? (
              <>
                <CommandGroup heading={t("switcher.platform")}>
                  <CommandItem value="__platform" onSelect={() => go("/platform/organizations")}>
                    <Initial text="" platform className="size-6 rounded-md" />
                    <span className="flex flex-1 flex-col">
                      <span className="font-medium">{t("switcher.platformName")}</span>
                      <span className="text-xs text-muted-foreground">
                        {workspace.platformRoles.map(roleLabel).join(" · ")}
                      </span>
                    </span>
                    {isPlatform ? <CheckIcon /> : null}
                  </CommandItem>
                </CommandGroup>
                <CommandSeparator />
              </>
            ) : null}
            <CommandGroup heading={t("switcher.organizations")}>
              {(organizations.data?.items ?? []).map((org) => {
                const role = membershipRole(workspace.me, org.id);
                return (
                  <CommandItem key={org.id} value={`${org.name} ${org.id}`} onSelect={() => go(`/o/${org.id}/overview`)}>
                    <Initial text={org.name} className="size-6 rounded-md text-xs" />
                    <span className="flex min-w-0 flex-1 flex-col">
                      <span className="truncate font-medium">{org.name}</span>
                      <span className="text-xs text-muted-foreground">
                        {role ? roleLabel(role) : t("switcher.platformViewer")}
                      </span>
                    </span>
                    {org.status !== "active" ? (
                      <StatusBadge tone={org.status === "provisioning" ? "info" : "warning"}>
                        {maybeT(`status.${org.status}`) ?? org.status}
                      </StatusBadge>
                    ) : null}
                    {workspace.orgId === org.id ? <CheckIcon /> : null}
                  </CommandItem>
                );
              })}
            </CommandGroup>
            {workspace.platformRoles.length > 0 ? (
              <>
                <CommandSeparator />
                <CommandGroup>
                  <CommandItem value="__browse" onSelect={() => go("/platform/organizations")}>
                    <Building2Icon />
                    {t("switcher.browseAll")}
                  </CommandItem>
                </CommandGroup>
              </>
            ) : null}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
