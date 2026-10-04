import { QueryClient } from "@tanstack/react-query";

import { isApiError } from "@/lib/api/errors";

const MAX_RETRIES = 2;

/** Retry what may pass on a second try (no answer, 5xx); a 4xx answers the same every time. */
export function shouldRetry(failures: number, error: unknown): boolean {
  if (failures >= MAX_RETRIES) return false;
  if (!isApiError(error)) return false;
  return error.kind === "network" || error.kind === "server";
}

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000, // prefetched data is not refetched as soon as it hydrates
        retry: shouldRetry,
        refetchOnWindowFocus: true,
      },
    },
  });
}
