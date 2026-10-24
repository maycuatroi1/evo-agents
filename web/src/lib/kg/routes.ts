import type { Route } from "next";

import { projectHref } from "@/components/shell/nav";

import type { Scope } from "./explorer";
import type { Hops, ReviewRule } from "./types";

/**
 * The knowledge graph's pages: `/p/{project}/kg` (the explorer: `?cluster=` or `?node=` for its scope, `?q=&kind=` for
 * its search), `/p/{project}/kg/builds` (status, commits read, shape and builds), `/p/{project}/kg/node?id=` (one node),
 * `/p/{project}/kg/cluster?id=` (one cluster) and `/p/{project}/kg/review` (the review queue).
 */
export const KG_SEGMENT = "kg";

export function kgHref(project: string, search?: { q?: string; kind?: string }): Route {
  const params = new URLSearchParams();
  if (search?.q) params.set("q", search.q);
  if (search?.kind) params.set("kind", search.kind);
  const query = params.toString();
  return `${projectHref(project, KG_SEGMENT)}${query ? `?${query}` : ""}` as Route;
}

/** The explorer at `scope`: the overview, a cluster or a node. */
export function explorerHref(project: string, scope: Scope): Route {
  const base = projectHref(project, KG_SEGMENT);
  if (scope.kind === "overview") return base;
  return `${base}?${new URLSearchParams({ [scope.kind]: scope.id }).toString()}` as Route;
}

/** The builds page; `failed` lists the failed builds only, at the history's anchor. */
export function buildsHref(project: string, failed = false): Route {
  const base = `${projectHref(project, KG_SEGMENT)}/builds`;
  return (failed ? `${base}?builds=failed#${BUILD_HISTORY_ANCHOR}` : base) as Route;
}

/** The anchor of the build history on the builds page (`BUILD_HISTORY_ID` of ./queries). */
const BUILD_HISTORY_ANCHOR = "build-history";

/** Node ids hold `:`, `#`, `/` and any letter: they travel in the query string, never in the path. */
export function nodeHref(project: string, id: string, hops?: Hops): Route {
  const params = new URLSearchParams({ id });
  if (hops !== undefined && hops !== 2) params.set("hops", String(hops));
  return `${projectHref(project, KG_SEGMENT)}/node?${params.toString()}` as Route;
}

/** Cluster ids travel in the query string, as node ids do. */
export function clusterHref(project: string, id: string): Route {
  return `${projectHref(project, KG_SEGMENT)}/cluster?${new URLSearchParams({ id }).toString()}` as Route;
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
