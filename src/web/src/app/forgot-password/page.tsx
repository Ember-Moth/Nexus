"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { MailCheckIcon } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { AuthCard, AuthLayout, postPublic, useAuthOptions } from "@/components/auth-layout";
import { ErrorAlert, Spinner } from "@/components/feedback";
import { Turnstile, useTurnstile } from "@/components/turnstile";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useT } from "@/i18n/provider";

const schema = z.object({ email: z.string().trim().regex(/^[^\s@]+@[^\s@]+\.[^\s@]+$/).max(254) });

export default function ForgotPasswordPage() {
  const t = useT();
  const options = useAuthOptions();
  const turnstile = useTurnstile();
  const [error, setError] = useState<unknown>(null);
  const [sent, setSent] = useState(false);
  const form = useForm<{ email: string }>({ resolver: zodResolver(schema), defaultValues: { email: "" } });
  const needsTurnstile = options.data?.turnstile ?? false;

  const onSubmit = form.handleSubmit(async ({ email }) => {
    setError(null);
    if (needsTurnstile && !turnstile.token) {
      form.setError("root", { message: "turnstile" });
      return;
    }
    try {
      await postPublic("/api/auth/forgot-password", { email: email.trim(), turnstile_token: turnstile.token ?? undefined });
      setSent(true);
    } catch (cause) {
      setError(cause);
    } finally {
      turnstile.reset();
    }
  });

  return (
    <AuthLayout>
      <AuthCard title={t("id.forgot.title")} subtitle={sent ? undefined : t("id.forgot.subtitle")}>
        {sent ? (
          <Alert>
            <MailCheckIcon />
            <AlertTitle>{t("id.forgot.sentTitle")}</AlertTitle>
            <AlertDescription>{t("id.forgot.sentBody")}</AlertDescription>
          </Alert>
        ) : (
          <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
            {error ? <ErrorAlert error={error} /> : null}
            {options.data && !options.data.email_delivery_available ? (
              <Alert className="border-amber-200 bg-amber-50 text-amber-900">
                <AlertDescription className="text-amber-900">{t("errors.codes.mail_unavailable")}</AlertDescription>
              </Alert>
            ) : null}
            <div className="grid gap-2">
              <Label htmlFor="forgot-email">{t("id.forgot.email")}</Label>
              <Input id="forgot-email" type="email" autoComplete="email" autoFocus aria-invalid={Boolean(form.formState.errors.email) || undefined} {...form.register("email")} />
              {form.formState.errors.email ? <span className="text-xs text-destructive">{t("id.shared.emailInvalid")}</span> : null}
            </div>
            {needsTurnstile && options.data?.turnstile_site_key ? (
              <Turnstile siteKey={options.data.turnstile_site_key} action="forgot_password" onToken={turnstile.setToken} widgetRef={turnstile.widget} />
            ) : null}
            {form.formState.errors.root?.message === "turnstile" ? <span className="text-xs text-destructive">{t("id.shared.turnstileRequired")}</span> : null}
            <Button type="submit" size="lg" disabled={form.formState.isSubmitting}>
              {form.formState.isSubmitting ? <Spinner /> : null}
              {form.formState.isSubmitting ? t("id.forgot.submitting") : t("id.forgot.submit")}
            </Button>
            <p className="text-[13px] leading-relaxed text-muted-foreground">{t("id.forgot.noEmail")}</p>
          </form>
        )}
        <p className="text-[13px] text-muted-foreground">
          {t("id.forgot.haveToken")}{" "}
          <Link href="/reset-password" className="font-medium text-foreground underline underline-offset-3">
            {t("id.forgot.resetLink")}
          </Link>
          {" · "}
          <Link href="/login" className="underline underline-offset-3">
            {t("id.shared.backToLogin")}
          </Link>
        </p>
      </AuthCard>
    </AuthLayout>
  );
}
