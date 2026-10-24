import type { Route } from "next";

import { projectHref } from "@/components/shell/nav";

import type { Hops, KgNode, ReviewRule } from "./types";

/**
 * The knowledge graph's pages: `/p/{project}/kg` (status, search and the cluster map), `/p/{project}/kg/node?id=` (one
 * node), `/p/{project}/kg/cluster?id=` (one cluster) and `/p/{project}/kg/review` (the review queue).
 */
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

/** The anchor of the cluster map on the knowledge graph page. */
export const KG_MAP_ID = "kg-map";

/**
 * The cluster map with the cluster of node `focus` lit up (the node's own id, or its file's or document's), keeping the
 * search the reader came from.
 */
export function mapHref(project: string, focus: string, search?: { q?: string; kind?: string }): Route {
  const params = new URLSearchParams();
  if (search?.q) params.set("q", search.q);
  if (search?.kind) params.set("kind", search.kind);
  params.set("focus", focus);
  return `${projectHref(project, KG_SEGMENT)}?${params.toString()}#${KG_MAP_ID}` as Route;
}

/** Cluster ids travel in the query string, as node ids do. */
export function clusterHref(project: string, id: string): Route {
  return `${projectHref(project, KG_SEGMENT)}/cluster?${new URLSearchParams({ id }).toString()}` as Route;
}

/**
 * The node a search result counts as on the map, as the hub's clusters project the graph: a Symbol is its file, a
 * Section its document, any other node itself.
 */
export function mapOwner(node: Pick<KgNode, "id" | "kind" | "props">): string {
  const key = node.kind === "Symbol" ? "file" : node.kind === "Section" ? "item" : null;
  const owner = key ? node.props[key] : null;
  return typeof owner === "string" && owner ? owner : node.id;
}

/** What the review page lists: one rule, one cluster, and open, dismissed or all items. */
export type ReviewState = "open" | "dismissed" | "all";
export type ReviewFilter = { rule?: ReviewRule | null; cluster?: string | null; state?: ReviewState };

/** The review queue, narrowed as `filter` says; the defaults (every rule and cluster, open items) stay out of the URL. */
export function reviewHref(project: string, filter: ReviewFilter = {}): Route {
  const params = new URLSearchParams();
  if (filter.rule) params.set("rule", filter.rule);
  if (filter.cluster) params.set("cluster", filter.cluster);
  if (filter.state && filter.state !== "open") params.set("state", filter.state);
  const query = params.toString();
  return `${projectHref(project, KG_SEGMENT)}/review${query ? `?${query}` : ""}` as Route;
}
