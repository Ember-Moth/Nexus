import { AuthGuard } from "@/components/shell/auth-guard";

export default function ConsoleLayout({ children }: LayoutProps<"/">) {
  return <AuthGuard>{children}</AuthGuard>;
}
