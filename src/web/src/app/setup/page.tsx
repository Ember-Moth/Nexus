import type { Metadata } from "next";

import { AuthGuard } from "@/components/shell/auth-guard";
import zhCN from "@/i18n/messages/zh-CN";

import { SetupWizard } from "./setup-wizard";

export const metadata: Metadata = { title: zhCN.id.wizard.title };

/** First-run setup of the built-in administrator; the console redirects here until it is done. */
export default function SetupPage() {
  return (
    <AuthGuard>
      <SetupWizard />
    </AuthGuard>
  );
}
