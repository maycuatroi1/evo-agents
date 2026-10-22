import { keepPreviousData, queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * The reads of the memories pages. The browser holds a web session, so the API filters every answer by the
 * member's grant (its max level) and by owner for user, feedback and personal memories; nothing here filters by
 * label again. Facets (location, type) only narrow what the API already returned.
 */
type Schemas = components["schemas"];
export type Memory = Schemas["Memory"];
export type FoundMemory = Schemas["Found"];
export type RevisionSummary = Schemas["RevisionSummary"];
export type MemoryRevision = Schemas["MemoryRevision"];
export type Revisions = Schemas["Revisions"];
export type Provenance = Schemas["Provenance"];

/** Whose memories a page shows: one project's, or the visitor's personal ones. */
export type MemoryScope = { kind: "project"; project: string } | { kind: "personal" };

export type MemoryList = { items: Memory[]; truncated: boolean };

/** Memories per request, the API's maximum, and how many requests one listing makes at most. */
export const LIST_PAGE = 500;
export const LIST_PAGES = 10;
export const SEARCH_LIMIT = 50;
export const REVISIONS_PAGE = 50;

function scopeKey(scope: MemoryScope): string {
  return scope.kind === "project" ? `project:${scope.project}` : "personal";
}

function scopeQuery(scope: MemoryScope) {
  return scope.kind === "project"
    ? { scope: "project" as const, project: scope.project }
    : { scope: "personal" as const };
}

/** The API's ?agent=: an agent's memories are listed only when it is named, never with the shared ones. */
const agentQuery = (agent: string | null) => (agent ? { agent } : {});
const agentKey = (agent: string | null) => (agent ? [`agent:${agent}`] : []);

export const memoryKeys = {
  all: ["memories"] as const,
  list: (scope: MemoryScope, agent: string | null = null) => ["memories", "list", scopeKey(scope), ...agentKey(agent)] as const,
  search: (scope: MemoryScope, q: string, location: string | null, agent: string | null = null) =>
    ["memories", "search", scopeKey(scope), q, location ?? "", ...agentKey(agent)] as const,
  one: (id: number) => ["memories", "one", id] as const,
  revisions: (id: number) => ["memories", "revisions", id] as const,
  revision: (id: number, revision: number) => ["memories", "revision", id, revision] as const,
  latest: (id: number, revision: number) => ["memories", "latest", id, revision] as const,
};

/**
 * Every memory of the scope the visitor sees, following the cursor; `truncated` when more pages remained. With `agent`
 * (a project's agent's handle), that agent's memories instead of the shared ones.
 */
export const memoryListQuery = (api: ApiSource, scope: MemoryScope, agent: string | null = null) =>
  queryOptions({
    queryKey: memoryKeys.list(scope, agent),
    queryFn: async ({ signal }): Promise<MemoryList> => {
      const items: Memory[] = [];
      let cursor: string | undefined;
      for (let page = 0; page < LIST_PAGES; page += 1) {
        const query = { ...scopeQuery(scope), ...agentQuery(agent), limit: LIST_PAGE, ...(cursor ? { cursor } : {}) };
        const data = await call(api().GET("/v1/memories", { params: { query }, signal }));
        items.push(...data.items);
        if (!data.next_cursor) return { items, truncated: false };
        cursor = data.next_cursor;
      }
      return { items, truncated: true };
    },
  });

/** Full-text search (the API's, configuration simple), best match first; `location` narrows it on the server. */
export const memorySearchQuery = (
  api: ApiSource,
  scope: MemoryScope,
  q: string,
  location: string | null,
  agent: string | null = null,
) =>
  queryOptions({
    queryKey: memoryKeys.search(scope, q, location, agent),
    queryFn: async ({ signal }) => {
      const query = { ...scopeQuery(scope), ...agentQuery(agent), q, limit: SEARCH_LIMIT, ...(location ? { location } : {}) };
      return (await call(api().GET("/v1/memories/search", { params: { query }, signal }))).items;
    },
    placeholderData: keepPreviousData, // the last results stay while the next query runs, instead of a skeleton
  });

export const memoryQuery = (api: ApiSource, id: number) =>
  queryOptions({
    queryKey: memoryKeys.one(id),
    queryFn: ({ signal }) => call(api().GET("/v1/memories/{memory_id}", { params: { path: { memory_id: id } }, signal })),
  });

export const memoryRevisionsQuery = (api: ApiSource, id: number) =>
  queryOptions({
    queryKey: memoryKeys.revisions(id),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/memories/{memory_id}/revisions", {
          params: { path: { memory_id: id }, query: { limit: REVISIONS_PAGE } },
          signal,
        }),
      ),
  });

/** The latest revision of a memory at `revision` and where it came from (run, agent, origin, hop), or null. */
export const latestRevisionQuery = (api: ApiSource, id: number, revision: number) =>
  queryOptions({
    queryKey: memoryKeys.latest(id, revision),
    queryFn: async ({ signal }) => {
      const page = await call(
        api().GET("/v1/memories/{memory_id}/revisions", { params: { path: { memory_id: id }, query: { limit: 1 } }, signal }),
      );
      return page.items[0] ?? null;
    },
    staleTime: Infinity, // keyed by the revision, which never changes
  });

/** Bring back an agent's memory the daily upkeep archived (a writer of its project): the memory as it is now. */
export async function restoreMemory(api: ApiClient, id: number): Promise<Memory> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/memories/{memory_id}/restore", { params: { path: { memory_id: id } }, headers }));
}

/** Older revisions than `before`, for "show more" in the history. */
export function olderRevisions(api: ApiSource, id: number, before: number, signal?: AbortSignal) {
  return call(
    api().GET("/v1/memories/{memory_id}/revisions", {
      params: { path: { memory_id: id }, query: { limit: REVISIONS_PAGE, before } },
      signal,
    }),
  );
}

export const memoryRevisionQuery = (api: ApiSource, id: number, revision: number) =>
  queryOptions({
    queryKey: memoryKeys.revision(id, revision),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/memories/{memory_id}/revisions/{revision}", {
          params: { path: { memory_id: id, revision } },
          signal,
        }),
      ),
    staleTime: Infinity, // a revision never changes
  });

/** A memory id as the API accepts it (1 to 2^63 - 1, kept within what a JavaScript number holds exactly). */
export function parseMemoryId(text: string): number | null {
  if (!/^[1-9][0-9]{0,15}$/.test(text)) return null;
  const id = Number(text);
  return Number.isSafeInteger(id) ? id : null;
}

/** A revision number from the URL, or null for the current one. */
export function parseRevision(text: string | string[] | undefined): number | null {
  if (typeof text !== "string" || !/^[1-9][0-9]{0,9}$/.test(text)) return null;
  const revision = Number(text);
  return revision <= 2 ** 31 - 1 ? revision : null;
}
