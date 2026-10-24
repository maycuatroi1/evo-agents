import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { ApiSource } from "@/lib/queries";

import { kgKeys } from "./queries";
import type { ReviewFilter, ReviewState } from "./routes";
import type { ReviewItem, ReviewRule } from "./types";

/** The rules of the review queue, in the order the hub lists them (`evo_agents.kg.review.RULES`). */
export const REVIEW_RULES: readonly ReviewRule[] = [
  "dangling_mention",
  "unmentioned_document",
  "cluster_without_knowledge",
  "possible_duplicate",
  "ambiguous_match",
  "bulk_noise",
];

const STATES: readonly ReviewState[] = ["open", "dismissed", "all"];
/** The longest reason the hub keeps (`MAX_REASON_CHARS` of `evo_agents.hub.server.kg_review`). */
export const MAX_REASON = 1000;
const MAX_CLUSTER = 200;

export type ReviewCluster = { cluster: string | null; label: string | null; items: ReviewItem[] };
export type ReviewGroup = { rule: ReviewRule; items: number; clusters: ReviewCluster[] };

/**
 * The items grouped by rule (in REVIEW_RULES order), then by cluster: the clusters of a rule ordered by their most
 * connected item, the items of a cluster by weight, as the hub orders the queue. Items in no cluster come last.
 */
export function groupReview(items: readonly ReviewItem[]): ReviewGroup[] {
  const groups: ReviewGroup[] = [];
  for (const rule of REVIEW_RULES) {
    const mine = items.filter((item) => item.rule === rule);
    if (mine.length === 0) continue;
    const byCluster = new Map<string, ReviewCluster>();
    for (const item of [...mine].sort((a, b) => b.weight - a.weight || a.fingerprint.localeCompare(b.fingerprint))) {
      const key = item.cluster ?? "";
      let found = byCluster.get(key);
      if (!found) {
        found = { cluster: item.cluster, label: item.cluster_label, items: [] };
        byCluster.set(key, found);
      }
      found.items.push(item);
    }
    const clusters = [...byCluster.values()].sort((a, b) => Number(a.cluster === null) - Number(b.cluster === null));
    groups.push({ rule, items: mine.length, clusters });
  }
  return groups;
}

function first(value: string | string[] | null | undefined): string {
  return (Array.isArray(value) ? value[0] : value) ?? "";
}

/** The review page's filter from its URL: unknown rules and states fall back to every rule and open items. */
export function parseReviewFilter(search: {
  rule?: string | string[] | null;
  cluster?: string | string[] | null;
  state?: string | string[] | null;
}): Required<ReviewFilter> {
  const rule = first(search.rule);
  const cluster = first(search.cluster);
  const state = first(search.state);
  return {
    rule: (REVIEW_RULES as readonly string[]).includes(rule) ? (rule as ReviewRule) : null,
    cluster: cluster && cluster.length <= MAX_CLUSTER ? cluster : null,
    state: (STATES as readonly string[]).includes(state) ? (state as ReviewState) : "open",
  };
}

/** The names of an item's nodes a line shows, the rest counted. */
export function nodeNames(item: ReviewItem, shown = 3): { names: string[]; more: number } {
  return {
    names: item.nodes.slice(0, shown).map((node) => node.name || node.id),
    more: Math.max(0, item.nodes.length - shown),
  };
}

/** The items of the review queue the reader can see, narrowed by `filter`, with the counts of every rule. */
export const kgReviewQuery = (api: ApiSource, project: string, filter: ReviewFilter) =>
  queryOptions({
    queryKey: kgKeys.reviewList(project, filter),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/kg/{project}/review", {
          params: {
            path: { project },
            query: {
              state: filter.state ?? "open",
              ...(filter.rule ? { rule: filter.rule } : {}),
              ...(filter.cluster ? { cluster: filter.cluster } : {}),
            },
          },
          signal,
        }),
      ),
  });

/** A writer dismisses an item of the review queue with a reason; it stays dismissed in later builds. */
export async function dismissReviewItem(api: ApiClient, project: string, fingerprint: string, reason: string) {
  const headers = await csrfHeaders(api);
  return call(
    api.POST("/v1/kg/{project}/review/{fingerprint}/dismiss", {
      params: { path: { project, fingerprint } },
      body: { reason },
      headers,
    }),
  );
}
