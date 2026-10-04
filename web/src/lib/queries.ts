import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";

/**
 * Every read of the API, as TanStack Query options. `api` returns the client to call when the query runs: the
 * server passes its cookie-forwarding client, the browser passes `browserApi`. Both share one query key, so data the
 * server prefetched hydrates in the browser.
 */
export type ApiSource = () => ApiClient;

export const queryKeys = {
  whoami: ["auth", "whoami"] as const,
  projects: ["projects"] as const,
  project: (name: string) => ["projects", name] as const,
  adminStats: ["admin", "stats"] as const,
};

export const whoamiQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: queryKeys.whoami,
    queryFn: ({ signal }) => call(api().GET("/v1/auth/whoami", { signal })),
  });

export const projectsQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: queryKeys.projects,
    queryFn: ({ signal }) => call(api().GET("/v1/projects", { signal })),
  });

export const projectQuery = (api: ApiSource, name: string) =>
  queryOptions({
    queryKey: queryKeys.project(name),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}", { params: { path: { project: name } }, signal })),
  });

export const adminStatsQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: queryKeys.adminStats,
    queryFn: ({ signal }) => call(api().GET("/v1/admin/stats", { signal })),
  });

/** `admin.PROJECT_NAME`: a name the API can accept; anything else is not a project. */
export const PROJECT_NAME = /^[a-z0-9][a-z0-9-]{0,99}$/;
