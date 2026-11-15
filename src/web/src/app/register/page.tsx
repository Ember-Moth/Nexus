"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useQueryClient } from "@tanstack/react-query";
import { MailCheckIcon } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
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
import { setCsrfToken } from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";

type Created = components["schemas"]["RegistrationCreated"] & { csrf_token: string };

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

const schema = z
  .object({
    login: z.string().trim().min(3).max(254).regex(/^\S+$/),
    email: z.string().trim().max(254),
    password: z.string().min(16).max(1024),
    confirm: z.string(),
  })
  .refine((v) => v.password === v.confirm, { path: ["confirm"], message: "mismatch" })
  .refine((v) => v.email === "" || EMAIL.test(v.email), { path: ["email"], message: "email" });
type Values = z.infer<typeof schema>;

export default function RegisterPage() {
  const t = useT();
  const router = useRouter();
  const queryClient = useQueryClient();
  const options = useAuthOptions();
  const turnstile = useTurnstile();
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { login: "", email: "", password: "", confirm: "" },
  });
  const errors = form.formState.errors;
  const verification = options.data?.email_verification ?? false;
  const needsTurnstile = options.data?.turnstile ?? false;

  const onSubmit = form.handleSubmit(async (values) => {
    setError(null);
    // With email verification on, an email-shaped login also counts as the address.
    if (verification && !values.email && !EMAIL.test(values.login)) {
      form.setError("email", { message: "required" });
      return;
    }
    if (needsTurnstile && !turnstile.token) {
      setError(null);
      form.setError("root", { message: "turnstile" });
      return;
    }
    try {
      const result = await postPublic<Created>("/api/auth/register", {
        login: values.login.trim(),
        password: values.password,
        email: values.email.trim() || undefined,
        turnstile_token: turnstile.token ?? undefined,
      });
      if (result.status === 201 && result.data) {
        setCsrfToken(result.data.csrf_token);
        queryClient.clear();
        router.replace(`/o/${result.data.personal_organization_id}/overview`);
        return;
      }
      setPending(true);
    } catch (cause) {
      setError(cause);
      form.setValue("password", "");
      form.setValue("confirm", "");
    } finally {
      turnstile.reset();
    }
  });

  const closed = isApiError(error) && (error.code === "registration_closed" || (error.status === 403 && error.code === "forbidden"));

  return (
    <AuthLayout>
      <AuthCard title={pending ? t("id.register.pendingTitle") : t("id.register.title")} subtitle={pending ? undefined : t("id.register.subtitle")}>
        {pending ? (
          <>
            <Alert>
              <MailCheckIcon />
              <AlertDescription>{t("id.register.pendingBody")}</AlertDescription>
            </Alert>
            <Button asChild size="lg">
              <Link href="/register/verify">{t("id.register.continueVerify")}</Link>
            </Button>
          </>
        ) : (
          <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
            {options.error ? <ErrorAlert error={options.error} title={t("id.shared.optionsUnavailable")} /> : null}
            {closed ? (
              <Alert className="border-amber-200 bg-amber-50 text-amber-900">
                <AlertTitle>{t("id.register.closed")}</AlertTitle>
              </Alert>
            ) : error ? (
              <ErrorAlert error={error} />
            ) : null}
            {verification && options.data && !options.data.email_delivery_available ? (
              <Alert className="border-amber-200 bg-amber-50 text-amber-900">
                <AlertDescription className="text-amber-900">{t("id.register.emailUnavailable")}</AlertDescription>
              </Alert>
            ) : null}
            <div className="grid gap-2">
              <Label htmlFor="reg-login">{t("id.register.login")}</Label>
              <Input id="reg-login" autoComplete="username" autoFocus aria-invalid={Boolean(errors.login) || undefined} {...form.register("login")} />
              <span className={errors.login ? "text-xs text-destructive" : "text-xs text-muted-foreground"}>
                {errors.login ? t("id.register.loginInvalid") : t("id.register.loginHint")}
              </span>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="reg-email">{verification ? t("id.register.email") : t("id.register.emailOptional")}</Label>
              <Input id="reg-email" type="email" autoComplete="email" aria-invalid={Boolean(errors.email) || undefined} {...form.register("email")} />
              {errors.email ? (
                <span className="text-xs text-destructive">
                  {errors.email.message === "required" ? t("id.register.emailRequired") : t("id.shared.emailInvalid")}
                </span>
              ) : verification ? (
                <span className="text-xs text-muted-foreground">{t("id.register.emailRequired")}</span>
              ) : null}
            </div>
            <div className="grid gap-2">
              <Label htmlFor="reg-password">{t("id.register.password")}</Label>
              <Input id="reg-password" type="password" autoComplete="new-password" aria-invalid={Boolean(errors.password) || undefined} {...form.register("password")} />
              <span className={errors.password ? "text-xs text-destructive" : "text-xs text-muted-foreground"}>
                {errors.password ? t("id.shared.passwordTooShort") : t("id.shared.passwordHint")}
              </span>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="reg-confirm">{t("id.register.confirmPassword")}</Label>
              <Input id="reg-confirm" type="password" autoComplete="new-password" aria-invalid={Boolean(errors.confirm) || undefined} {...form.register("confirm")} />
              {errors.confirm ? <span className="text-xs text-destructive">{t("id.shared.passwordMismatch")}</span> : null}
            </div>
            {needsTurnstile && options.data?.turnstile_site_key ? (
              <Turnstile siteKey={options.data.turnstile_site_key} action="register" onToken={turnstile.setToken} widgetRef={turnstile.widget} />
            ) : null}
            {errors.root?.message === "turnstile" ? <span className="text-xs text-destructive">{t("id.shared.turnstileRequired")}</span> : null}
            <Button type="submit" size="lg" disabled={form.formState.isSubmitting || options.isPending}>
              {form.formState.isSubmitting ? <Spinner /> : null}
              {form.formState.isSubmitting ? t("id.register.submitting") : t("id.register.submit")}
            </Button>
            <p className="text-[13px] text-muted-foreground">
              {t("id.register.haveAccount")}{" "}
              <Link href="/login" className="font-medium text-foreground underline underline-offset-3">
                {t("id.register.loginLink")}
              </Link>
            </p>
          </form>
        )}
      </AuthCard>
    </AuthLayout>
  );
}
