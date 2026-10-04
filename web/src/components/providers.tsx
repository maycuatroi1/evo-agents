"use client";

import { QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "next-themes";
import { type ReactNode, useState } from "react";

import { TooltipProvider } from "@/components/ui/tooltip";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { makeQueryClient } from "@/lib/query-client";

function browserQueryClient(): QueryClient {
  const client = makeQueryClient();
  // A session that expired or was revoked while the page was open: go and sign in again.
  const cache = new QueryCache({
    onError: (error) => {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
    },
  });
  return new QueryClient({ queryCache: cache, defaultOptions: client.getDefaultOptions() });
}

export function Providers({ nonce, children }: { nonce?: string; children: ReactNode }) {
  const [queryClient] = useState(browserQueryClient);
  return (
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange nonce={nonce}>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider delayDuration={300}>{children}</TooltipProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
