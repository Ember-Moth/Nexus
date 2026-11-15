"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckIcon } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { ErrorAlert, Spinner } from "@/components/feedback";
import { AccountFrame } from "@/components/shell/account-frame";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";
import { useMe } from "@/lib/api/hooks";

const schema = z.object({ token: z.string().trim().min(1).max(128) });

function AcceptInvitation() {
  const { t, maybeT } = useI18n();
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const me = useMe();
  const form = useForm<z.infer<typeof schema>>({ resolver: zodResolver(schema), defaultValues: { token: params.get("token") ?? "" } });

  // POST /invitations/accept with the signed-in account's session; returns the membership.
  const accept = useMutation({
    mutationFn: async (token: string) =>
      (await call(() => nexus.POST("/api/v1/invitations/accept", { body: { token } }))).data,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["me"] }),
  });
  const gone = isApiError(accept.error) && [409, 410].includes(accept.error.status);
  const mismatch = isApiError(accept.error) && (accept.error.status === 403 || accept.error.code === "invitation_unavailable");

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("id.invitation.title")}</CardTitle>
        <CardDescription>{t("id.invitation.subtitle")}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {me.data ? <p className="text-sm text-muted-foreground">{t("id.invitation.signedInAs", { login: me.data.user.login })}</p> : null}
        {accept.data ? (
          <>
            <Alert className="border-emerald-200 bg-emerald-50 text-emerald-900">
              <CheckIcon />
              <AlertDescription className="text-emerald-900">
                {t("id.invitation.joined", { role: maybeT(`roles.${accept.data.role}`) ?? accept.data.role })}
              </AlertDescription>
            </Alert>
            <Button asChild className="self-start">
              <Link href={`/o/${accept.data.organization_id}/overview`}>{t("id.invitation.open")}</Link>
            </Button>
          </>
        ) : (
          <form noValidate className="flex flex-col gap-4" onSubmit={form.handleSubmit((values) => accept.mutate(values.token.trim()))}>
            {gone ? (
              <ErrorAlert error={accept.error} title={t("id.invitation.gone")} />
            ) : mismatch ? (
              <ErrorAlert error={accept.error} title={t("id.invitation.mismatch")} />
            ) : accept.error ? (
              <ErrorAlert error={accept.error} />
            ) : null}
            <div className="grid gap-2">
              <Label htmlFor="invite-token">{t("id.invitation.token")}</Label>
              <Input id="invite-token" className="font-mono" autoComplete="off" aria-invalid={Boolean(form.formState.errors.token) || undefined} {...form.register("token")} />
              {form.formState.errors.token ? <span className="text-xs text-destructive">{t("id.shared.tokenRequired")}</span> : null}
            </div>
            <Button type="submit" className="self-start" disabled={accept.isPending}>
              {accept.isPending ? <Spinner /> : null}
              {accept.isPending ? t("id.invitation.accepting") : t("id.invitation.accept")}
            </Button>
          </form>
        )}
      </CardContent>
    </Card>
  );
}

export default function AcceptInvitationPage() {
  return (
    <AccountFrame>
      <Suspense>
        <AcceptInvitation />
      </Suspense>
    </AccountFrame>
  );
}
