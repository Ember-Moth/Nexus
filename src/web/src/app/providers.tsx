"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { isApiError } from "@/lib/api/errors";
import { I18nProvider } from "@/i18n/provider";
import type { Locale } from "@/i18n/types";

function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        refetchOnWindowFocus: false,
        // Authorization and validation failures will not change by retrying.
        retry: (count, error) =>
          count < 2 && !(isApiError(error) && error.status >= 400 && error.status < 500),
      },
    },
  });
}

export function Providers({ locale, children }: { locale: Locale; children: ReactNode }) {
  const [queryClient] = useState(createQueryClient);
  return (
    <QueryClientProvider client={queryClient}>
      <I18nProvider locale={locale}>
        <TooltipProvider delayDuration={300}>
          {children}
          <Toaster position="bottom-right" />
        </TooltipProvider>
      </I18nProvider>
    </QueryClientProvider>
  );
}
