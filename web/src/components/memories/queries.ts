import { keepPreviousData, queryOptions } from "@tanstack/react-query";

import { call } from "@/lib/api/client";
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

export const memoryKeys = {
  all: ["memories"] as const,
  list: (scope: MemoryScope) => ["memories", "list", scopeKey(scope)] as const,
  search: (scope: MemoryScope, q: string, location: string | null) =>
    ["memories", "search", scopeKey(scope), q, location ?? ""] as const,
  one: (id: number) => ["memories", "one", id] as const,
  revisions: (id: number) => ["memories", "revisions", id] as const,
  revision: (id: number, revision: number) => ["memories", "revision", id, revision] as const,
};

/** Every memory of the scope the visitor sees, following the cursor; `truncated` when more pages remained. */
export const memoryListQuery = (api: ApiSource, scope: MemoryScope) =>
  queryOptions({
    queryKey: memoryKeys.list(scope),
    queryFn: async ({ signal }): Promise<MemoryList> => {
      const items: Memory[] = [];
      let cursor: string | undefined;
      for (let page = 0; page < LIST_PAGES; page += 1) {
        const query = { ...scopeQuery(scope), limit: LIST_PAGE, ...(cursor ? { cursor } : {}) };
        const data = await call(api().GET("/v1/memories", { params: { query }, signal }));
        items.push(...data.items);
        if (!data.next_cursor) return { items, truncated: false };
        cursor = data.next_cursor;
      }
      return { items, truncated: true };
    },
  });

/** Full-text search (the API's, configuration simple), best match first; `location` narrows it on the server. */
export const memorySearchQuery = (api: ApiSource, scope: MemoryScope, q: string, location: string | null) =>
  queryOptions({
    queryKey: memoryKeys.search(scope, q, location),
    queryFn: async ({ signal }) => {
      const query = { ...scopeQuery(scope), q, limit: SEARCH_LIMIT, ...(location ? { location } : {}) };
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
