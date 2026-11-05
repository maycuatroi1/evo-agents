import { queryOptions } from "@tanstack/react-query";

import { call } from "@/lib/api/client";
import type { ApiSource } from "@/lib/queries";

/**
 * The Entities view's reads of the API (`/v1/kg/{project}/entity-graph`, `.../entity-graph/around`, `.../entity`,
 * `.../relation`), as TanStack Query options. All reads; the hub answers only what the reader's grant shows.
 */
export const entityKeys = {
  graph: (project: string, limit: number) => ["kg", project, "entity-graph", limit] as const,
  around: (project: string, id: string, depth: number, limit: number) => ["kg", project, "entity-around", id, depth, limit] as const,
  entity: (project: string, id: string) => ["kg", project, "entity", id] as const,
  relation: (project: string, id: string) => ["kg", project, "relation", id] as const,
  search: (project: string, query: string, kinds: readonly string[]) => ["kg", project, "entity-search", query, ...kinds] as const,
};

/** The entities the most documents mention, with the relations between them. */
export const entityGraphQuery = (api: ApiSource, project: string, limit: number) =>
  queryOptions({
    queryKey: entityKeys.graph(project, limit),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/kg/{project}/entity-graph", { params: { path: { project }, query: { limit } }, signal })),
    staleTime: 60_000,
    // A new limit keeps the last graph on screen until its answer comes.
    placeholderData: (previous) => previous,
  });

/** The entities within `depth` relations of one, heaviest first, at most `limit`. */
export const entityAroundQuery = (api: ApiSource, project: string, id: string, depth: number, limit: number) =>
  queryOptions({
    queryKey: entityKeys.around(project, id, depth, limit),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/kg/{project}/entity-graph/around", {
          params: { path: { project }, query: { id, depth, limit } },
          signal,
        }),
      ),
    staleTime: 60_000,
    placeholderData: (previous, query) => (query && query.queryKey[3] === id ? previous : undefined),
  });

export const entityDetailQuery = (api: ApiSource, project: string, id: string) =>
  queryOptions({
    queryKey: entityKeys.entity(project, id),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/kg/{project}/entity", { params: { path: { project }, query: { id } }, signal })),
  });

export const relationDetailQuery = (api: ApiSource, project: string, id: string) =>
  queryOptions({
    queryKey: entityKeys.relation(project, id),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/kg/{project}/relation", { params: { path: { project }, query: { id } }, signal })),
  });

/** The graph's search, limited to the entity kinds drawn: entities outside the view a reader can focus on (ids `entity:`). */
export const entitySearchQuery = (api: ApiSource, project: string, query: string, kinds: readonly string[]) =>
  queryOptions({
    queryKey: entityKeys.search(project, query, kinds),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/kg/{project}/nodes", { params: { path: { project }, query: { q: query, kind: [...kinds] } }, signal })),
  });
