import type { Metadata } from "next";
import { Geist_Mono, Inter } from "next/font/google";
import { cookies } from "next/headers";
import { cn } from "@/lib/utils";

import { LOCALE_COOKIE, toLocale } from "@/i18n/types";
import zhCN from "@/i18n/messages/zh-CN";
import { Providers } from "./providers";
import "./globals.css";

const inter = Inter({ subsets: ["latin"], variable: "--font-sans" });
const mono = Geist_Mono({ subsets: ["latin"], variable: "--font-geist-mono" });

export const metadata: Metadata = {
  title: { default: zhCN.app.name, template: `%s · ${zhCN.app.name}` },
  description: zhCN.app.description,
  robots: { index: false, follow: false },
};

export default async function RootLayout({ children }: LayoutProps<"/">) {
  const locale = toLocale((await cookies()).get(LOCALE_COOKIE)?.value);
  return (
    <html lang={locale} className={cn("h-full antialiased", inter.variable, mono.variable)}>
      <body className="min-h-full bg-background font-sans text-foreground">
        <Providers locale={locale}>{children}</Providers>
      </body>
    </html>
  );
}
