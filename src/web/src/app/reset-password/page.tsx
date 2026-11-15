"use client";

import Link from "next/link";
import { Suspense, useState } from "react";

import { AuthCard, AuthLayout, postPublic } from "@/components/auth-layout";
import { TokenPasswordForm } from "@/components/token-form";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useT } from "@/i18n/provider";

function ResetPassword() {
  const t = useT();
  const [done, setDone] = useState(false);
  return (
    <AuthCard title={t("id.reset.title")} subtitle={t("id.reset.subtitle")}>
      {done ? (
        <>
          <Alert>
            <AlertDescription>{t("id.reset.done")}</AlertDescription>
          </Alert>
          <Button asChild size="lg">
            <Link href="/login">{t("login.submit")}</Link>
          </Button>
        </>
      ) : (
        <TokenPasswordForm
          tokenLabel={t("id.reset.token")}
          passwordLabel={t("id.reset.password")}
          confirmLabel={t("id.reset.confirmPassword")}
          submitLabel={t("id.reset.submit")}
          submittingLabel={t("id.reset.submitting")}
          turnstileAction="reset_password"
          onSubmit={async (values) => {
            await postPublic("/api/auth/reset-password", values);
            setDone(true);
          }}
        >
          <Link href="/login" className="text-[13px] text-muted-foreground underline underline-offset-3">
            {t("id.shared.backToLogin")}
          </Link>
        </TokenPasswordForm>
      )}
    </AuthCard>
  );
}

export default function ResetPasswordPage() {
  return (
    <AuthLayout>
      <Suspense>
        <ResetPassword />
      </Suspense>
    </AuthLayout>
  );
}
