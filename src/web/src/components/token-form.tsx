"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useSearchParams } from "next/navigation";
import { useState, type ReactNode } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { useAuthOptions } from "@/components/auth-layout";
import { ErrorAlert, Spinner } from "@/components/feedback";
import { Turnstile, useTurnstile } from "@/components/turnstile";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useT } from "@/i18n/provider";
import { isApiError } from "@/lib/api/errors";

const schema = z
  .object({
    token: z.string().trim().min(1).max(128),
    password: z.string().min(16).max(1024),
    confirm: z.string(),
    confirmRequired: z.boolean(),
  })
  .refine((v) => !v.confirmRequired || v.password === v.confirm, { path: ["confirm"], message: "mismatch" });
type Values = z.infer<typeof schema>;

/**
 * Token + password form shared by "完成注册" and "重置密码". The token can arrive as ?token=
 * or be pasted from the email (the mails only carry the token).
 */
export function TokenPasswordForm({
  tokenLabel,
  passwordLabel,
  confirmLabel,
  submitLabel,
  submittingLabel,
  turnstileAction,
  onSubmit,
  children,
}: {
  tokenLabel: string;
  passwordLabel: string;
  confirmLabel?: string;
  submitLabel: string;
  submittingLabel: string;
  turnstileAction: string;
  onSubmit: (values: { token: string; password: string; turnstile_token?: string }) => Promise<void>;
  children?: ReactNode;
}) {
  const t = useT();
  const params = useSearchParams();
  const options = useAuthOptions();
  const turnstile = useTurnstile();
  const [error, setError] = useState<unknown>(null);
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { token: params.get("token") ?? "", password: "", confirm: "", confirmRequired: Boolean(confirmLabel) },
  });
  const errors = form.formState.errors;
  const needsTurnstile = options.data?.turnstile ?? false;

  const submit = form.handleSubmit(async (values) => {
    setError(null);
    if (needsTurnstile && !turnstile.token) {
      form.setError("root", { message: "turnstile" });
      return;
    }
    try {
      await onSubmit({ token: values.token.trim(), password: values.password, turnstile_token: turnstile.token ?? undefined });
    } catch (cause) {
      setError(cause);
    } finally {
      turnstile.reset();
    }
  });

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4">
      {error ? (
        <ErrorAlert error={error} description={isApiError(error) && error.status === 401 ? t("id.shared.tokenInvalid") : undefined} />
      ) : null}
      <div className="grid gap-2">
        <Label htmlFor="token">{tokenLabel}</Label>
        <Input id="token" autoComplete="one-time-code" className="font-mono" aria-invalid={Boolean(errors.token) || undefined} {...form.register("token")} />
        <span className={errors.token ? "text-xs text-destructive" : "text-xs text-muted-foreground"}>
          {errors.token ? t("id.shared.tokenRequired") : t("id.shared.tokenHint")}
        </span>
      </div>
      <div className="grid gap-2">
        <Label htmlFor="password">{passwordLabel}</Label>
        <Input
          id="password"
          type="password"
          autoComplete={confirmLabel ? "new-password" : "current-password"}
          aria-invalid={Boolean(errors.password) || undefined}
          {...form.register("password")}
        />
        <span className={errors.password ? "text-xs text-destructive" : "text-xs text-muted-foreground"}>
          {errors.password ? t("id.shared.passwordTooShort") : t("id.shared.passwordHint")}
        </span>
      </div>
      {confirmLabel ? (
        <div className="grid gap-2">
          <Label htmlFor="confirm">{confirmLabel}</Label>
          <Input id="confirm" type="password" autoComplete="new-password" aria-invalid={Boolean(errors.confirm) || undefined} {...form.register("confirm")} />
          {errors.confirm ? <span className="text-xs text-destructive">{t("id.shared.passwordMismatch")}</span> : null}
        </div>
      ) : null}
      {needsTurnstile && options.data?.turnstile_site_key ? (
        <Turnstile siteKey={options.data.turnstile_site_key} action={turnstileAction} onToken={turnstile.setToken} widgetRef={turnstile.widget} />
      ) : null}
      {errors.root?.message === "turnstile" ? <span className="text-xs text-destructive">{t("id.shared.turnstileRequired")}</span> : null}
      <Button type="submit" size="lg" disabled={form.formState.isSubmitting}>
        {form.formState.isSubmitting ? <Spinner /> : null}
        {form.formState.isSubmitting ? submittingLabel : submitLabel}
      </Button>
      {children}
    </form>
  );
}
