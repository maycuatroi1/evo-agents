"use client";

import { type QueryKey, useQuery, type UseQueryOptions } from "@tanstack/react-query";
import { useState } from "react";

import type { HubQueryState } from "@/components/states/query-view";
import type { ApiError, ApiErrorInfo } from "@/lib/api/errors";

/**
 * `useHubQuery` for a list paged by cursor: the same states, plus whether the rows shown are the previous page's,
 * kept on screen while the next page loads. That flag is false whenever the rows are the ones asked for, which is
 * always the case while the server-rendered page hydrates, so server and browser render the same markup.
 */
export function usePagedQuery<T, K extends QueryKey>(
  options: UseQueryOptions<T, ApiError, T, K>,
  initialError: ApiErrorInfo | null = null,
): { state: HubQueryState<T>; stale: boolean } {
  const [serverError, setServerError] = useState(initialError);
  const query = useQuery({ ...options, enabled: serverError === null && options.enabled !== false });
  if (serverError) return { state: { status: "error", error: serverError, retry: () => setServerError(null) }, stale: false };
  if (query.isSuccess) return { state: { status: "success", data: query.data }, stale: query.isPlaceholderData };
  if (query.isError) return { state: { status: "error", error: query.error.info, retry: () => void query.refetch() }, stale: false };
  return { state: { status: "loading" }, stale: false };
}
