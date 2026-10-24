import type { ClusterSummary, MapNode } from "./types";

/**
 * Clusters as the explorer and the cluster page show them: each cluster's colour, its place in the map's order, and the
 * members a filter keeps. The hub sends only what the reader can see (GET /v1/kg/{project}/clusters), so nothing here
 * filters for visibility.
 */

/** Categorical colours of the clusters, the first cluster (the largest) first; they repeat past the twelfth. */
export const CLUSTER_PALETTE = [
  "#4e79a7",
  "#f28e2b",
  "#e15759",
  "#76b7b2",
  "#59a14f",
  "#edc948",
  "#b07aa1",
  "#ff9da7",
  "#9c755f",
  "#bab0ac",
  "#2f4bd8",
  "#0f8c80",
] as const;

export function clusterColour(index: number): string {
  return CLUSTER_PALETTE[((index % CLUSTER_PALETTE.length) + CLUSTER_PALETTE.length) % CLUSTER_PALETTE.length];
}

/** The index of each cluster id in `clusters`, the order colours and the canvas's cluster numbers follow. */
export function clusterIndex(clusters: readonly Pick<ClusterSummary, "id">[]): Map<string, number> {
  return new Map(clusters.map((cluster, index) => [cluster.id, index]));
}

/** The members whose name, id or kind holds `text`, case and accents aside; all of them for empty text. */
export function filterMembers<T extends Pick<MapNode, "id" | "kind" | "name">>(members: readonly T[], text: string): T[] {
  // "đ" has no decomposition, so it is mapped by hand.
  const fold = (value: string) =>
    value.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().replace(/đ/g, "d");
  const wanted = fold(text.trim());
  if (!wanted) return [...members];
  return members.filter((m) => [m.name ?? "", m.id, m.kind].some((value) => fold(value).includes(wanted)));
}
