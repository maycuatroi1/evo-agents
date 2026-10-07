"use client";

import {
  type DehydratedState,
  hydrate,
  type QueryClient,
  HydrationBoundary as QueryHydrationBoundary,
  useQueryClient,
} from "@tanstack/react-query";
import { type ReactNode, useMemo } from "react";

/**
 * The queries of `state` that the cache already holds without data: an observer created the entry (the sidebar's run
 * count, its fleet line, the project switcher) and nothing has filled it yet.
 */
export function emptyEntries(client: QueryClient, state: DehydratedState): DehydratedState["queries"] {
  const cache = client.getQueryCache();
  return state.queries.filter((query) => {
    const existing = cache.get(query.queryHash);
    return existing !== undefined && existing.state.data === undefined && query.state.data !== undefined;
  });
}

/**
 * Puts what a server component fetched into the browser's cache, as TanStack's HydrationBoundary does, with one
 * difference: an entry the cache holds without data is filled during render too, not in an effect after commit.
 *
 * TanStack fills new entries during render and holds back existing ones until the effect, so a transition does not
 * change what the current page shows before it commits. The shell renders before the page and reads some of the
 * page's queries, so on the server, and in the browser's first render, those entries exist already, empty: held back,
 * the page would render its skeleton on the server and match it in the browser, its data arriving only after the
 * effect. An entry without data shows nothing yet, so filling it early changes no page's content; entries that hold
 * data keep TanStack's order.
 *
 * Use it wherever a server component hands its prefetched queries to the client (ESLint points here).
 */
export function HydrationBoundary({ state, children }: { state: DehydratedState; children: ReactNode }) {
  const client = useQueryClient();
  // In render, like TanStack's own hydration of new entries; idempotent, as an entry filled once is no longer empty.
  useMemo(() => {
    const queries = emptyEntries(client, state);
    if (queries.length > 0) hydrate(client, { queries });
  }, [client, state]);
  return <QueryHydrationBoundary state={state}>{children}</QueryHydrationBoundary>;
}
