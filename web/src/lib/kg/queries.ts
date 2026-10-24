import { queryOptions } from "@tanstack/react-query";

import { call } from "@/lib/api/client";
import type { ApiSource } from "@/lib/queries";

import type { ReviewFilter } from "./routes";
import { GROUP_SHOWN, type Hops, type KgBuilds } from "./types";

/**
 * The knowledge graph pages' reads of the API, as TanStack Query options shared by the server (prefetch) and the
 * browser. All of them are reads: the web never queues a build. The review queue's read and its one write, a writer
 * dismissing an item, are in `./review`, which only the review page loads; the decisions and a writer's steps on them
 * are in `./decisions`.
 */
export const BUILDS_SHOWN = 20;
/** While a build waits or runs, the status page asks again this often (ms). */
export const ACTIVE_REFRESH = 5_000;

export const kgKeys = {
  all: (project: string) => ["kg", project] as const,
  builds: (project: string) => ["kg", project, "builds"] as const,
  failedBuilds: (project: string) => ["kg", project, "builds", "failed"] as const,
  graph: (project: string) => ["kg", project, "graph"] as const,
  search: (project: string, query: string, kind: string) => ["kg", project, "search", query, kind] as const,
  node: (project: string, id: string) => ["kg", project, "node", id] as const,
  neighbourhood: (project: string, id: string, hops: Hops, grouping?: Grouping) =>
    grouping
      ? (["kg", project, "neighbourhood", id, hops, grouping.group, ...openKeys(grouping)] as const)
      : (["kg", project, "neighbourhood", id, hops] as const),
  shape: (project: string) => ["kg", project, "shape"] as const,
  clusterSummary: (project: string) => ["kg", project, "clusters", "summary"] as const,
  cluster: (project: string, id: string) => ["kg", project, "cluster", id] as const,
  review: (project: string) => ["kg", project, "review"] as const,
  reviewList: (project: string, filter: ReviewFilter) =>
    ["kg", project, "review", filter.rule ?? "", filter.cluster ?? "", filter.state ?? "open"] as const,
  decisions: (project: string) => ["kg", project, "decisions"] as const,
};

/**
 * How the node page asks for a neighbourhood: at most `group` direct neighbours of each (edge type, direction, kind)
 * group, the most connected first, and the whole of the groups whose keys are in `open`.
 */
export type Grouping = { group: number; open: readonly string[] };

/** The node page's grouping before the reader opens a group. */
export const NODE_PAGE_GROUPING: Grouping = { group: GROUP_SHOWN, open: [] };

/** The opened keys in one order, so the same set always makes the same query. */
export function openKeys(grouping: Grouping): string[] {
  return [...new Set(grouping.open)].sort();
}

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

/** The anchor of the build history on the knowledge graph page. */
export const BUILD_HISTORY_ID = "build-history";

/** Which builds the history lists: all of the latest, or the latest failed ones (`?builds=failed`). */
export type BuildFilter = "all" | "failed";

export function parseBuildFilter(value: string | string[] | null | undefined): BuildFilter {
  const text = Array.isArray(value) ? value[0] : value;
  return text === "failed" ? "failed" : "all";
}

/** The project's latest failed builds, for the history's Failed view; the admin overview links there. */
export const kgFailedBuildsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: kgKeys.failedBuilds(project),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/kg/{project}/builds", {
          params: { path: { project }, query: { limit: BUILDS_SHOWN, status: "failed" } },
          signal,
        }),
      ),
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

/**
 * The neighbourhood of a node; with `grouping`, as the node page draws it. Opening a group asks again, and the view
 * of the same node and steps stays on screen until the answer comes; another node never shows the last one's.
 */
export const kgNeighbourhoodQuery = (api: ApiSource, project: string, id: string, hops: Hops, grouping?: Grouping) =>
  queryOptions({
    queryKey: kgKeys.neighbourhood(project, id, hops, grouping),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/kg/{project}/neighbourhood", {
          params: {
            path: { project },
            query: grouping ? { id, hops, group: grouping.group, open: openKeys(grouping) } : { id, hops },
          },
          signal,
        }),
      ),
    placeholderData: (previous, query) =>
      query && query.queryKey[3] === id && query.queryKey[4] === hops ? previous : undefined,
  });

/** How many visible edges join each kind of node to each other, by edge type. */
export const kgShapeQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: kgKeys.shape(project),
    queryFn: ({ signal }) => call(api().GET("/v1/kg/{project}/shape", { params: { path: { project } }, signal })),
  });

/** The level-1 clusters and the links between them without their members: what the explorer's overview draws. */
export const kgClusterSummaryQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: kgKeys.clusterSummary(project),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/kg/{project}/clusters", { params: { path: { project }, query: { nodes: false } }, signal })),
    staleTime: 60_000,
  });

/** One cluster with its subclusters, members and neighbouring clusters. */
export const kgClusterQuery = (api: ApiSource, project: string, id: string) =>
  queryOptions({
    queryKey: kgKeys.cluster(project, id),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/kg/{project}/cluster", { params: { path: { project }, query: { id } }, signal })),
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
