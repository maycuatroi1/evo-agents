"use client";

import { type QueryKey, useQuery, type UseQueryOptions, type UseQueryResult } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";

import { useLiveQuery } from "@/components/live/live-context";
import type { ApiError, ApiErrorInfo } from "@/lib/api/errors";

import { ApiErrorState, LoadingState } from "./states";

export type HubQueryState<T> =
  | { status: "loading" }
  | { status: "error"; error: ApiErrorInfo; retry: () => void }
  | { status: "success"; data: T };

/**
 * Whether a live query that failed for want of an answer (no answer, or a 5xx) keeps the data it last read on screen.
 * TanStack marks such a refetch as an error though the data is kept; the page then shows that data as it was, and the
 * top bar's LiveIndicator says it is no longer current (Reconnecting, then Offline with the time of the last update).
 * A 403 or 404 is an answer, and the page shows it. Pages without `live` have no indicator, and show the error.
 */
export function keepsStaleData(query: Pick<UseQueryResult<unknown, ApiError>, "isError" | "data" | "error">, live: boolean): boolean {
  return live && query.isError && query.data !== undefined && (query.error?.kind === "network" || query.error?.kind === "server");
}

/**
 * A query whose first answer may already be known: the server prefetched it (the data hydrates) or failed to
 * (`initialError`, shown at once without asking the API again until the visitor retries). With `live`, it is the
 * page's main query: the top bar's LiveIndicator says from it whether the page is current, while it polls.
 */
export function useHubQuery<T, K extends QueryKey>(
  options: UseQueryOptions<T, ApiError, T, K>,
  initialError: ApiErrorInfo | null = null,
  { live = false }: { live?: boolean } = {},
): HubQueryState<T> {
  const [serverError, setServerError] = useState(initialError);
  const enabled = serverError === null && options.enabled !== false;
  const query = useQuery({ ...options, enabled });
  useLiveQuery(options, live && enabled);
  if (serverError) return { status: "error", error: serverError, retry: () => setServerError(null) };
  if (query.isSuccess) return { status: "success", data: query.data };
  if (keepsStaleData(query, live)) return { status: "success", data: query.data as T };
  if (query.isError) return { status: "error", error: query.error.info, retry: () => void query.refetch() };
  return { status: "loading" };
}

type QueryViewProps<T> = {
  state: HubQueryState<T>;
  loading?: ReactNode;
  children: (data: T) => ReactNode;
};

/** Renders the loading skeleton, the error state for the failure, or the data. */
export function QueryView<T>({ state, loading, children }: QueryViewProps<T>) {
  if (state.status === "loading") return <LoadingState>{loading}</LoadingState>;
  if (state.status === "error") return <ApiErrorState error={state.error} onRetry={state.retry} />;
  return <>{children(state.data)}</>;
}
