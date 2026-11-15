"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { ConflictAlert, Spinner } from "@/components/feedback";
import { PcErrorAlert } from "@/components/platform-catalog/errors";
import { Field } from "@/components/platform-catalog/form";
import { OneTimeTokenPanel } from "@/components/platform-catalog/one-time-token";
import type { SetupIssued, UserView } from "@/components/platform-catalog/types";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import type { PlatformRole } from "@/lib/api/types";

export const usersKey = ["platform-users"] as const;

/** Dialog shell that blocks Escape, outside clicks and the close button while a token is shown. */
function TokenDialog({
  issued,
  onClose,
  children,
  className,
}: {
  issued: SetupIssued | null;
  onClose: () => void;
  children: ReactNode;
  className?: string;
}) {
  const showing = issued !== null;
  return (
    <Dialog open onOpenChange={(next) => !next && !showing && onClose()}>
      <DialogContent
        className={className ?? "sm:max-w-[460px]"}
        showCloseButton={!showing}
        onEscapeKeyDown={(event) => showing && event.preventDefault()}
        onInteractOutside={(event) => showing && event.preventDefault()}
      >
        {children}
      </DialogContent>
    </Dialog>
  );
}

const createSchema = z.object({
  login: z
    .string()
    .trim()
    .min(3)
    .max(254)
    .regex(/^\S+$/),
});
type CreateValues = z.infer<typeof createSchema>;

export function CreateUserDialog({ onClose }: { onClose: () => void }) {
  const t = useT();
  const queryClient = useQueryClient();
  const [issued, setIssued] = useState<SetupIssued | null>(null);
  const form = useForm<CreateValues>({ resolver: zodResolver(createSchema), defaultValues: { login: "" } });

  // POST /users has no Idempotency-Key; a lost response surfaces as 409 on retry.
  const create = useMutation({
    mutationFn: async (login: string) => (await call(() => nexus.POST("/api/v1/users", { body: { login } }))).data,
    onSuccess: (result) => {
      setIssued(result);
      void queryClient.invalidateQueries({ queryKey: usersKey });
    },
  });

  return (
    <TokenDialog issued={issued} onClose={onClose}>
      {issued ? (
        <OneTimeTokenPanel issued={issued} onDone={onClose} />
      ) : (
        <form noValidate className="grid gap-5" onSubmit={form.handleSubmit((values) => create.mutate(values.login))}>
          <DialogHeader>
            <DialogTitle>{t("pc.users.createDialog.title")}</DialogTitle>
            <DialogDescription>{t("pc.users.createDialog.description")}</DialogDescription>
          </DialogHeader>
          {create.error ? <PcErrorAlert error={create.error} /> : null}
          <Field
            id="user-login"
            label={t("pc.users.createDialog.login")}
            hint={t("pc.users.createDialog.loginHint")}
            error={form.formState.errors.login ? t("pc.users.createDialog.loginInvalid") : undefined}
          >
            <Input
              id="user-login"
              autoComplete="off"
              autoFocus
              maxLength={254}
              aria-invalid={Boolean(form.formState.errors.login) || undefined}
              {...form.register("login")}
            />
          </Field>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? <Spinner /> : null}
              {t("pc.users.createDialog.submit")}
            </Button>
          </DialogFooter>
        </form>
      )}
    </TokenDialog>
  );
}

export function ResetTokenDialog({ user, onClose }: { user: UserView; onClose: () => void }) {
  const t = useT();
  const [issued, setIssued] = useState<SetupIssued | null>(null);
  const reset = useMutation({
    mutationFn: async () =>
      (
        await call(() =>
          nexus.POST("/api/v1/users/{user_id}/password-reset-tokens", { params: { path: { user_id: user.id } } }),
        )
      ).data,
    onSuccess: setIssued,
  });

  return (
    <TokenDialog issued={issued} onClose={onClose}>
      {issued ? (
        <OneTimeTokenPanel issued={issued} reset onDone={onClose} />
      ) : (
        <>
          <DialogHeader>
            <DialogTitle>{t("pc.users.resetDialog.title")}</DialogTitle>
            <DialogDescription>{t("pc.users.resetDialog.description", { login: user.login })}</DialogDescription>
          </DialogHeader>
          {reset.error ? <PcErrorAlert error={reset.error} /> : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button type="button" disabled={reset.isPending} onClick={() => reset.mutate()}>
              {reset.isPending ? <Spinner /> : null}
              {t("pc.users.resetDialog.submit")}
            </Button>
          </DialogFooter>
        </>
      )}
    </TokenDialog>
  );
}

const ROLES: { role: PlatformRole; label: "pc.users.rolesDialog.admin" | "pc.users.rolesDialog.finance"; hint: "pc.users.rolesDialog.adminHint" | "pc.users.rolesDialog.financeHint" }[] = [
  { role: "platform_admin", label: "pc.users.rolesDialog.admin", hint: "pc.users.rolesDialog.adminHint" },
  { role: "platform_finance", label: "pc.users.rolesDialog.finance", hint: "pc.users.rolesDialog.financeHint" },
];

/** GET roles for the ETag, then PATCH the complete role set with If-Match. */
export function RolesDialog({ user, isSelf, onClose }: { user: UserView; isSelf: boolean; onClose: () => void }) {
  const t = useT();
  const queryClient = useQueryClient();
  const detail = useQuery({
    queryKey: [...usersKey, "roles", user.id],
    queryFn: async () =>
      call(() => nexus.GET("/api/v1/users/{user_id}/platform-roles", { params: { path: { user_id: user.id } } })),
    staleTime: 0,
  });
  const current = detail.data?.data;

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-[460px]">
        <DialogHeader>
          <DialogTitle>{t("pc.users.rolesDialog.title", { login: user.login })}</DialogTitle>
          <DialogDescription>{t("pc.users.rolesDialog.description")}</DialogDescription>
        </DialogHeader>
        {detail.error ? <PcErrorAlert error={detail.error} onRetry={() => void detail.refetch()} /> : null}
        {current ? (
          <RolesForm
            key={current.version}
            userId={user.id}
            roles={current.roles}
            etag={detail.data?.etag ?? `"${current.version}"`}
            isSelf={isSelf}
            reloading={detail.isFetching}
            onReload={() => void detail.refetch()}
            onSaved={() => {
              toast.success(t("pc.users.rolesDialog.saved"));
              void queryClient.invalidateQueries({ queryKey: usersKey });
              if (isSelf) void queryClient.invalidateQueries({ queryKey: ["me"] });
              onClose();
            }}
            onCancel={onClose}
          />
        ) : detail.isPending ? (
          <div className="flex justify-center py-6">
            <Spinner />
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function RolesForm({
  userId,
  roles,
  etag,
  isSelf,
  reloading,
  onReload,
  onSaved,
  onCancel,
}: {
  userId: string;
  roles: string[];
  etag: string;
  isSelf: boolean;
  reloading: boolean;
  onReload: () => void;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const t = useT();
  const [selected, setSelected] = useState<PlatformRole[]>(
    ROLES.map((r) => r.role).filter((role) => roles.includes(role)),
  );
  const save = useMutation({
    mutationFn: async (next: PlatformRole[]) =>
      call(() =>
        nexus.PATCH("/api/v1/users/{user_id}/platform-roles", {
          params: { path: { user_id: userId }, header: { "If-Match": etag } },
          body: { roles: next },
        }),
      ),
    onSuccess: onSaved,
  });
  const unchanged =
    selected.length === roles.filter((r) => r === "platform_admin" || r === "platform_finance").length &&
    selected.every((role) => roles.includes(role));
  const conflict = isConflict(save.error);

  return (
    <div className="grid gap-5">
      {conflict ? (
        <ConflictAlert
          onReload={() => {
            save.reset();
            onReload();
          }}
        />
      ) : save.error ? (
        <PcErrorAlert error={save.error} />
      ) : null}
      <div className="grid gap-2">
        {ROLES.map(({ role, label, hint }) => (
          <Label key={role} htmlFor={`role-${role}`} className="cursor-pointer items-start rounded-md border px-3 py-2.5 font-normal">
            <Checkbox
              id={`role-${role}`}
              className="mt-0.5"
              checked={selected.includes(role)}
              onCheckedChange={(checked) =>
                setSelected((prev) =>
                  ROLES.map((r) => r.role).filter((r) => (r === role ? checked === true : prev.includes(r))),
                )
              }
            />
            <span className="flex flex-col gap-0.5">
              <span className="font-medium">{t(label)}</span>
              <span className="text-xs text-muted-foreground">{hint ? t(hint) : null}</span>
              <code className="font-mono text-[11px] text-muted-foreground">{role}</code>
            </span>
          </Label>
        ))}
      </div>
      {isSelf && roles.includes("platform_admin") && !selected.includes("platform_admin") ? (
        <p role="alert" className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          {t("pc.users.rolesDialog.selfWarning")}
        </p>
      ) : null}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>
          {t("common.cancel")}
        </Button>
        <Button type="button" disabled={save.isPending || unchanged || conflict || reloading} onClick={() => save.mutate(selected)}>
          {save.isPending ? <Spinner /> : null}
          {t("common.save")}
        </Button>
      </DialogFooter>
    </div>
  );
}
