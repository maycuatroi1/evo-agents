"use client";

import { QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "next-themes";
import { type ReactNode, useState } from "react";

import { HubToaster } from "@/components/feedback/toast";
import { LiveProvider } from "@/components/live/live-context";
import { TooltipProvider } from "@/components/ui/tooltip";
import { MobileHintProvider } from "@/hooks/use-mobile";
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

/**
 * `mobile` is the server's guess that the request comes from a phone (`lib/mobile-hint.ts`): the server renders, and
 * the browser hydrates, the phone layout on it, before the browser measures its own width.
 */
export function Providers({ nonce, mobile = false, children }: { nonce?: string; mobile?: boolean; children: ReactNode }) {
  const [queryClient] = useState(browserQueryClient);
  return (
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange nonce={nonce}>
      <MobileHintProvider mobile={mobile}>
        <QueryClientProvider client={queryClient}>
          <LiveProvider>
            <TooltipProvider delayDuration={300}>{children}</TooltipProvider>
          </LiveProvider>
          {/* Results of writes, bottom right (components/feedback/toast.tsx). */}
          <HubToaster />
        </QueryClientProvider>
      </MobileHintProvider>
    </ThemeProvider>
  );
}
