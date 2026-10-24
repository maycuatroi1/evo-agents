import type { ClusterLink, ClusterSummary, MapNode } from "./types";

/**
 * What the cluster map draws, computed without the canvas: each cluster's colour, the points' first positions, the
 * matrix of links between clusters, the centre a label sits at, and the members a filter keeps. The hub sends only
 * what the reader can see (GET /v1/kg/{project}/clusters), so nothing here filters for visibility.
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

/** The clusters the matrix shows at most, the largest first: past that its cells get too small to read. */
export const MATRIX_CLUSTERS = 24;

export function clusterColour(index: number): string {
  return CLUSTER_PALETTE[((index % CLUSTER_PALETTE.length) + CLUSTER_PALETTE.length) % CLUSTER_PALETTE.length];
}

/** "#rgb" or "#rrggbb" as red, green, blue and alpha from 0 to 1; grey for anything else. */
export function rgba(hex: string, alpha = 1): [number, number, number, number] {
  const text = hex.trim().replace(/^#/, "");
  const full = text.length === 3 ? [...text].map((c) => c + c).join("") : text;
  if (!/^[0-9a-fA-F]{6}$/.test(full)) return [0.5, 0.5, 0.5, alpha];
  const value = Number.parseInt(full, 16);
  return [((value >> 16) & 255) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255, alpha];
}

/** The index of each cluster id in `clusters`, the order colours and the canvas's cluster numbers follow. */
export function clusterIndex(clusters: readonly Pick<ClusterSummary, "id">[]): Map<string, number> {
  return new Map(clusters.map((cluster, index) => [cluster.id, index]));
}

/** A number from 0 to 1 that depends only on `text`, so the same graph always starts from the same picture. */
function unit(text: string, salt: number): number {
  let hash = 2166136261 ^ salt;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return ((hash >>> 0) % 100000) / 100000;
}

/**
 * Where each cluster starts, on a golden-angle spiral around the centre of a square of side `space`, the largest
 * cluster in the middle, as flat [x, y, x, y, ...].
 */
export function clusterCentres(count: number, space: number): number[] {
  const middle = space / 2;
  const step = (space * 0.4) / Math.sqrt(Math.max(count, 1));
  const golden = Math.PI * (3 - Math.sqrt(5));
  return Array.from({ length: count }, (_, i) => {
    const radius = step * Math.sqrt(i);
    return [middle + radius * Math.cos(i * golden), middle + radius * Math.sin(i * golden)];
  }).flat();
}

/** Each point's first position, near its cluster's centre (`centres` from clusterCentres), as flat [x, y, ...]. */
export function startPositions(
  ids: readonly string[],
  clusterOf: readonly (number | undefined)[],
  centres: readonly number[],
  spread: number,
): Float32Array {
  const out = new Float32Array(ids.length * 2);
  const middle = centres.length ? (centres[0] ?? 0) : 0;
  ids.forEach((id, i) => {
    const cluster = clusterOf[i];
    const cx = cluster === undefined ? middle : (centres[cluster * 2] ?? middle);
    const cy = cluster === undefined ? middle : (centres[cluster * 2 + 1] ?? middle);
    const angle = unit(id, 1) * Math.PI * 2;
    const radius = Math.sqrt(unit(id, 2)) * spread;
    out[i * 2] = cx + radius * Math.cos(angle);
    out[i * 2 + 1] = cy + radius * Math.sin(angle);
  });
  return out;
}

/** The mean position of each cluster's points, from flat [x, y, ...]; null for a cluster without a point. */
export function centroids(
  positions: ArrayLike<number>,
  clusterOf: readonly (number | undefined)[],
  count: number,
): ([number, number] | null)[] {
  const sums = Array.from({ length: count }, () => [0, 0, 0]);
  clusterOf.forEach((cluster, i) => {
    const sum = cluster === undefined ? undefined : sums[cluster];
    const x = positions[i * 2];
    const y = positions[i * 2 + 1];
    if (!sum || x === undefined || y === undefined || Number.isNaN(x) || Number.isNaN(y)) return;
    sum[0] += x;
    sum[1] += y;
    sum[2] += 1;
  });
  return sums.map(([x, y, n]) => (n ? [x / n, y / n] : null));
}

export type MatrixCell = { row: number; col: number; weight: number };
export type Matrix = { clusters: ClusterSummary[]; cells: MatrixCell[]; max: number; hidden: number };

/**
 * The cluster-by-cluster matrix: the `limit` largest clusters (the map lists them largest first), and a cell on both
 * sides of the diagonal for each link between two of them, weighted by its edges. `hidden` counts the clusters left
 * out.
 */
export function clusterMatrix(
  clusters: readonly ClusterSummary[],
  links: readonly ClusterLink[],
  limit = MATRIX_CLUSTERS,
): Matrix {
  const shown = clusters.slice(0, limit);
  const index = clusterIndex(shown);
  const cells: MatrixCell[] = [];
  let max = 0;
  for (const link of links) {
    const a = index.get(link.a);
    const b = index.get(link.b);
    if (a === undefined || b === undefined || a === b) continue;
    cells.push({ row: a, col: b, weight: link.weight }, { row: b, col: a, weight: link.weight });
    max = Math.max(max, link.weight);
  }
  return { clusters: shown, cells, max, hidden: clusters.length - shown.length };
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
