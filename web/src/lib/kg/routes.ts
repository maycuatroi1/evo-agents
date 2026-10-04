import type { Route } from "next";

import { projectHref } from "@/components/shell/nav";

import type { Hops } from "./types";

/** The knowledge graph's pages: `/p/{project}/kg` (status and search) and `/p/{project}/kg/node?id=` (one node). */
export const KG_SEGMENT = "kg";

export function kgHref(project: string, search?: { q?: string; kind?: string }): Route {
  const params = new URLSearchParams();
  if (search?.q) params.set("q", search.q);
  if (search?.kind) params.set("kind", search.kind);
  const query = params.toString();
  return `${projectHref(project, KG_SEGMENT)}${query ? `?${query}` : ""}` as Route;
}

/** Node ids hold `:`, `#`, `/` and any letter: they travel in the query string, never in the path. */
export function nodeHref(project: string, id: string, hops?: Hops): Route {
  const params = new URLSearchParams({ id });
  if (hops !== undefined && hops !== 2) params.set("hops", String(hops));
  return `${projectHref(project, KG_SEGMENT)}/node?${params.toString()}` as Route;
}
