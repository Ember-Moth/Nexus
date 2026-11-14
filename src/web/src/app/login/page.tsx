import type { Metadata } from "next";
import { Suspense } from "react";

import zhCN from "@/i18n/messages/zh-CN";
import { LoginForm } from "./login-form";

export const metadata: Metadata = { title: zhCN.login.title };

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}
