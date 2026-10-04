import { queryOptions } from "@tanstack/react-query";

import { call } from "@/lib/api/client";
import type { ApiSource } from "@/lib/queries";

import type { Hops, KgBuilds } from "./types";

/**
 * The knowledge graph pages' reads of the API, as TanStack Query options shared by the server (prefetch) and the
 * browser. All of them are reads: the web never queues a build.
 */
export const BUILDS_SHOWN = 20;
/** While a build waits or runs, the status page asks again this often (ms). */
export const ACTIVE_REFRESH = 5_000;

export const kgKeys = {
  all: (project: string) => ["kg", project] as const,
  builds: (project: string) => ["kg", project, "builds"] as const,
  graph: (project: string) => ["kg", project, "graph"] as const,
  search: (project: string, query: string, kind: string) => ["kg", project, "search", query, kind] as const,
  node: (project: string, id: string) => ["kg", project, "node", id] as const,
  neighbourhood: (project: string, id: string, hops: Hops) => ["kg", project, "neighbourhood", id, hops] as const,
};

/** Whether anything of the project's builds is still moving: a job in the queue, or a build queued or running. */
export function buildsActive(data: KgBuilds | undefined): boolean {
  if (!data) return false;
  return data.jobs.length > 0 || data.builds.some((build) => build.status === "queued" || build.status === "running");
}

export const kgBuildsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: kgKeys.builds(project),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/kg/{project}/builds", {
          params: { path: { project }, query: { limit: BUILDS_SHOWN } },
          signal,
        }),
      ),
    refetchInterval: (query) => (buildsActive(query.state.data) ? ACTIVE_REFRESH : false),
  });

export const kgGraphQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: kgKeys.graph(project),
    queryFn: ({ signal }) => call(api().GET("/v1/kg/{project}/graph", { params: { path: { project } }, signal })),
  });

export const kgSearchQuery = (api: ApiSource, project: string, query: string, kind: string) =>
  queryOptions({
    queryKey: kgKeys.search(project, query, kind),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/kg/{project}/nodes", {
          params: { path: { project }, query: { q: query, ...(kind ? { kind: [kind] } : {}) } },
          signal,
        }),
      ),
  });

export const kgNodeQuery = (api: ApiSource, project: string, id: string) =>
  queryOptions({
    queryKey: kgKeys.node(project, id),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/kg/{project}/node", { params: { path: { project }, query: { id } }, signal })),
  });

export const kgNeighbourhoodQuery = (api: ApiSource, project: string, id: string, hops: Hops) =>
  queryOptions({
    queryKey: kgKeys.neighbourhood(project, id, hops),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/kg/{project}/neighbourhood", {
          params: { path: { project }, query: { id, hops } },
          signal,
        }),
      ),
  });

/** A search query as the URL carries it: trimmed, at most what the API accepts. */
export const MAX_QUERY = 200;
export function cleanQuery(value: string | string[] | undefined): string {
  const text = Array.isArray(value) ? (value[0] ?? "") : (value ?? "");
  return text.trim().slice(0, MAX_QUERY);
}

export function parseHops(value: string | string[] | undefined): Hops {
  const text = Array.isArray(value) ? value[0] : value;
  return text === "1" ? 1 : 2;
}
