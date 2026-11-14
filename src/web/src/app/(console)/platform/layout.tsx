import { ConsoleShell } from "@/components/shell/console-shell";

export default function PlatformLayout({ children }: LayoutProps<"/platform">) {
  return <ConsoleShell kind="platform">{children}</ConsoleShell>;
}
