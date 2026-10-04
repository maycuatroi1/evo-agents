"use client";

import { type QueryKey, useQuery, type UseQueryOptions } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";

import type { ApiError, ApiErrorInfo } from "@/lib/api/errors";

import { ApiErrorState, LoadingState } from "./states";

export type HubQueryState<T> =
  | { status: "loading" }
  | { status: "error"; error: ApiErrorInfo; retry: () => void }
  | { status: "success"; data: T };

/**
 * A query whose first answer may already be known: the server prefetched it (the data hydrates) or failed to
 * (`initialError`, shown at once without asking the API again until the visitor retries).
 */
export function useHubQuery<T, K extends QueryKey>(
  options: UseQueryOptions<T, ApiError, T, K>,
  initialError: ApiErrorInfo | null = null,
): HubQueryState<T> {
  const [serverError, setServerError] = useState(initialError);
  const query = useQuery({ ...options, enabled: serverError === null && options.enabled !== false });
  if (serverError) return { status: "error", error: serverError, retry: () => setServerError(null) };
  if (query.isSuccess) return { status: "success", data: query.data };
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
