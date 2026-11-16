import { AuthGuard } from "@/components/shell/auth-guard";
import { SetupGuard } from "@/components/shell/setup-guard";

export default function ConsoleLayout({ children }: LayoutProps<"/">) {
  return (
    <AuthGuard>
      <SetupGuard>{children}</SetupGuard>
    </AuthGuard>
  );
}
