"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Suspense } from "react";

import { AuthCard, AuthLayout, postPublic } from "@/components/auth-layout";
import { TokenPasswordForm } from "@/components/token-form";
import { useT } from "@/i18n/provider";
import { setCsrfToken } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

type Created = components["schemas"]["RegistrationCreated"] & { csrf_token: string };

function VerifyRegistration() {
  const t = useT();
  const router = useRouter();
  const queryClient = useQueryClient();
  return (
    <AuthCard title={t("id.verifyRegistration.title")} subtitle={t("id.verifyRegistration.subtitle")}>
      <TokenPasswordForm
        tokenLabel={t("id.verifyRegistration.token")}
        passwordLabel={t("id.verifyRegistration.password")}
        submitLabel={t("id.verifyRegistration.submit")}
        submittingLabel={t("id.verifyRegistration.submitting")}
        turnstileAction="confirm_registration"
        onSubmit={async (values) => {
          const result = await postPublic<Created>("/api/auth/register/verify", values);
          if (result.data) {
            setCsrfToken(result.data.csrf_token);
            queryClient.clear();
            router.replace(`/o/${result.data.personal_organization_id}/overview`);
          }
        }}
      >
        <Link href="/login" className="text-[13px] text-muted-foreground underline underline-offset-3">
          {t("id.shared.backToLogin")}
        </Link>
      </TokenPasswordForm>
    </AuthCard>
  );
}

export default function VerifyRegistrationPage() {
  return (
    <AuthLayout>
      <Suspense>
        <VerifyRegistration />
      </Suspense>
    </AuthLayout>
  );
}
