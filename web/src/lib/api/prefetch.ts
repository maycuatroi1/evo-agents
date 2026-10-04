import "server-only";

import { type FetchQueryOptions, type QueryClient, type QueryKey } from "@tanstack/react-query";
import { redirect } from "next/navigation";
import { cache } from "react";

import { LOGIN_PATH } from "@/lib/config";
import { makeQueryClient } from "@/lib/query-client";

import { type ApiError, type ApiErrorInfo, isApiError, toInfo } from "./errors";

/** One QueryClient per request, shared by the layouts and the page of that request. */
export const getQueryClient = cache((): QueryClient => {
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { ...client.getDefaultOptions().queries, retry: false } });
  return client;
});

/**
 * Fetch a query on the server so the page renders with its data. A 401 sends the visitor to sign in; any other
 * failure comes back as a plain object for the page's client component to show, and is not cached.
 */
export async function prefetch<T, K extends QueryKey>(
  client: QueryClient,
  options: FetchQueryOptions<T, ApiError, T, K>,
): Promise<ApiErrorInfo | null> {
  let failure: ApiErrorInfo | null = null;
  try {
    await client.fetchQuery(options);
  } catch (error) {
    if (isApiError(error) && error.kind === "unauthorized") redirect(LOGIN_PATH);
    failure = toInfo(error);
  }
  return failure;
}
