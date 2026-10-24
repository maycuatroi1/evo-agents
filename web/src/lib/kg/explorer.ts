import { displayName, kindStyle, shortLabel, type Shape, type Tone } from "./graph";
import { clusterColour, clusterIndex } from "./map";
import type { ClusterDetail, ClusterLink, ClusterSummary, MapNode } from "./types";

/**
 * The model of the knowledge graph explorer (`/p/{project}/kg`), without the canvas: what it draws at each scope, how a
 * scope reads in the URL, and how the neighbourhoods a reader expands merge into one working set.
 *
 * Three scopes, one canvas. The overview draws the level-1 clusters as circles sized by their members, joined by the
 * links between them; a cluster draws its members inside boxes for its parts (level-2 clusters); a node draws itself
 * and its direct neighbours, and each neighbour the reader expands adds its own (./explorer-working.ts). Every scope
 * reads only what the hub sends the reader, so nothing here filters for visibility.
 */

export type Scope = { kind: "overview" } | { kind: "cluster"; id: string } | { kind: "node"; id: string };

export const OVERVIEW: Scope = { kind: "overview" };

/** A key that names the scope, for React keys and for telling two scopes apart. */
export function scopeKey(scope: Scope): string {
  return scope.kind === "overview" ? "overview" : `${scope.kind}:${scope.id}`;
}

type Params = { get(name: string): string | null };

/** The scope a URL names: `?node=` before `?cluster=`; `?focus=`, which the map's links used to set, reads as a node. */
export function parseScope(params: Params): Scope {
  const node = (params.get("node") ?? params.get("focus") ?? "").trim();
  if (node) return { kind: "node", id: node };
  const cluster = (params.get("cluster") ?? "").trim();
  if (cluster) return { kind: "cluster", id: cluster };
  return OVERVIEW;
}

/** The search of `params` with `scope` in place of the one it named, keeping everything else (the query, the kind). */
export function withScope(params: Params & { toString(): string }, scope: Scope): URLSearchParams {
  const next = new URLSearchParams(params.toString());
  for (const name of ["node", "cluster", "focus"]) next.delete(name);
  if (scope.kind !== "overview") next.set(scope.kind, scope.id);
  return next;
}

/** What the inspector shows: a cluster or a node of the canvas. */
export type Selection = { type: "cluster"; id: string } | { type: "node"; id: string } | null;

/** One element of the canvas, as Cytoscape takes it; `classes` drive the stylesheet (explorer-style.ts). */
export type ExplorerElement = {
  group: "nodes" | "edges";
  data: Record<string, string | number | boolean | undefined>;
  classes?: string[];
};

/** A drawable node's look: the kind's tone and shape, so colour is never the only signal. */
export type NodeLook = { tone: Tone; shape: Shape };

// -- Overview -----------------------------------------------------------------------------------------------------

/** Each cluster keeps its heaviest links; the others show only while one of their clusters is hovered or selected. */
export const STRONG_LINKS = 3;

/** The diameter of a cluster's circle, by its members, on a square-root scale (px). */
export function clusterDiameter(size: number, largest: number): number {
  const min = 22;
  const max = 92;
  if (largest <= 0) return min;
  return Math.round(min + (max - min) * Math.sqrt(Math.max(size, 0) / largest));
}

/** The width of a link, by its edges, on a log scale against the heaviest (px). */
export function linkWidth(weight: number, heaviest: number): number {
  if (heaviest <= 1) return 1;
  return Math.round((0.75 + 6.25 * (Math.log(Math.max(weight, 1)) / Math.log(heaviest))) * 100) / 100;
}

/** The links each cluster keeps (its `per` heaviest), as the set of their "a b" keys. */
export function strongLinks(links: readonly ClusterLink[], per = STRONG_LINKS): Set<string> {
  const byCluster = new Map<string, ClusterLink[]>();
  for (const link of links) {
    for (const end of [link.a, link.b]) byCluster.set(end, [...(byCluster.get(end) ?? []), link]);
  }
  const kept = new Set<string>();
  for (const list of byCluster.values()) {
    for (const link of [...list].sort((x, y) => y.weight - x.weight).slice(0, per)) kept.add(linkKey(link));
  }
  return kept;
}

export function linkKey(link: Pick<ClusterLink, "a" | "b">): string {
  return `${link.a} ${link.b}`;
}

/** The members of a cluster left once the hidden kinds are taken out. */
export function shownSize(cluster: Pick<ClusterSummary, "kinds">, hidden: ReadonlySet<string>): number {
  return Object.entries(cluster.kinds).reduce((sum, [kind, count]) => sum + (hidden.has(kind) ? 0 : count), 0);
}

export function overviewElements(
  clusters: readonly ClusterSummary[],
  links: readonly ClusterLink[],
  hidden: ReadonlySet<string> = new Set(),
): ExplorerElement[] {
  const index = clusterIndex(clusters);
  const largest = Math.max(0, ...clusters.map((cluster) => cluster.size));
  const heaviest = Math.max(0, ...links.map((link) => link.weight));
  const strong = strongLinks(links);
  const nodes: ExplorerElement[] = clusters.map((cluster) => {
    const shown = shownSize(cluster, hidden);
    return {
      group: "nodes",
      data: {
        id: cluster.id,
        label: cluster.label,
        size: cluster.size,
        diameter: clusterDiameter(cluster.size, largest),
        colour: clusterColour(index.get(cluster.id) ?? 0),
      },
      classes: ["cluster", ...(shown === 0 ? ["filtered"] : [])],
    };
  });
  const edges: ExplorerElement[] = links
    .filter((link) => index.has(link.a) && index.has(link.b))
    .map((link) => ({
      group: "edges",
      data: {
        id: `link:${linkKey(link)}`,
        source: link.a,
        target: link.b,
        weight: link.weight,
        width: linkWidth(link.weight, heaviest),
      },
      classes: ["link", ...(strong.has(linkKey(link)) ? [] : ["weak"])],
    }));
  return [...nodes, ...edges];
}

/** The clusters a cluster's links reach, heaviest first, from the overview's links. */
export function linkedClusters(
  id: string,
  clusters: readonly ClusterSummary[],
  links: readonly ClusterLink[],
): { cluster: ClusterSummary; weight: number }[] {
  const byId = new Map(clusters.map((cluster) => [cluster.id, cluster]));
  return links
    .filter((link) => link.a === id || link.b === id)
    .flatMap((link) => {
      const cluster = byId.get(link.a === id ? link.b : link.a);
      return cluster ? [{ cluster, weight: link.weight }] : [];
    })
    .sort((x, y) => y.weight - x.weight);
}

// -- Labels -------------------------------------------------------------------------------------------------------

/** The nodes of a view that keep their label at any zoom: the most linked, or all of a small view. */
export const MAJOR_LABELS = 24;
export const SMALL_VIEW = 40;

/** The ids of the `MAJOR_LABELS` highest of `degrees` (all of them in a view of `SMALL_VIEW` or fewer). */
export function majorIds(degrees: readonly { id: string; degree: number }[]): Set<string> {
  if (degrees.length <= SMALL_VIEW) return new Set(degrees.map((entry) => entry.id));
  return new Set(
    [...degrees]
      .sort((a, b) => b.degree - a.degree || a.id.localeCompare(b.id))
      .slice(0, MAJOR_LABELS)
      .map((entry) => entry.id),
  );
}

// -- Cluster ------------------------------------------------------------------------------------------------------

/** The members a cluster draws at most, the most linked first; the inspector and the cluster page list them all. */
export const CLUSTER_DRAWN = 600;

/** The diameter of a node by its edges in the view, on a square-root scale (px). */
export function nodeDiameter(degree: number, highest: number): number {
  const min = 12;
  const max = 30;
  if (highest <= 0) return min;
  return Math.round(min + (max - min) * Math.sqrt(Math.max(degree, 0) / highest));
}

/** The members' weighted degree inside the cluster, from the detail's [index, index, weight] edges. */
export function memberDegrees(detail: Pick<ClusterDetail, "members" | "edges">): number[] {
  const degree = new Array<number>(detail.members.length).fill(0);
  for (const [a, b, weight] of detail.edges) {
    if (a === undefined || b === undefined) continue;
    degree[a] = (degree[a] ?? 0) + (weight ?? 1);
    degree[b] = (degree[b] ?? 0) + (weight ?? 1);
  }
  return degree;
}

export type ClusterDrawing = { elements: ExplorerElement[]; drawn: number; total: number };

export function clusterElements(detail: ClusterDetail, hidden: ReadonlySet<string> = new Set()): ClusterDrawing {
  const degree = memberDegrees(detail);
  const order = detail.members
    .map((member, index) => ({ member, index, degree: degree[index] ?? 0 }))
    .sort((a, b) => b.degree - a.degree || a.member.id.localeCompare(b.member.id));
  const drawn = order.slice(0, CLUSTER_DRAWN);
  const drawnIndex = new Set(drawn.map((entry) => entry.index));
  const highest = Math.max(0, ...drawn.map((entry) => entry.degree));
  const parts = new Set(detail.subclusters.map((part) => part.id));
  const major = majorIds(drawn.map(({ member, degree: d }) => ({ id: member.id, degree: d })));
  const partNodes: ExplorerElement[] = detail.subclusters.map((part) => ({
    group: "nodes",
    data: { id: part.id, label: part.label, size: part.size },
    classes: ["part"],
  }));
  const memberNodes: ExplorerElement[] = drawn.map(({ member, degree: d }) => {
    const { tone, shape } = kindStyle(member.kind);
    const parent = member.sub && parts.has(member.sub) && member.sub !== detail.cluster.id ? member.sub : undefined;
    return {
      group: "nodes",
      data: {
        id: member.id,
        label: shortLabel(displayName(member)),
        name: displayName(member),
        kind: member.kind,
        tone,
        shape,
        degree: d,
        diameter: nodeDiameter(d, highest),
        parent,
      },
      classes: ["member", ...(major.has(member.id) ? ["major"] : []), ...(hidden.has(member.kind) ? ["kind-hidden"] : [])],
    };
  });
  const edges: ExplorerElement[] = detail.edges.flatMap(([a, b, weight], i) => {
    if (a === undefined || b === undefined || !drawnIndex.has(a) || !drawnIndex.has(b)) return [];
    const source = detail.members[a];
    const target = detail.members[b];
    if (!source || !target) return [];
    return [
      {
        group: "edges",
        data: { id: `pair:${i}`, source: source.id, target: target.id, weight: weight ?? 1 },
        classes: ["pair"],
      },
    ];
  });
  return { elements: [...partNodes, ...memberNodes, ...edges], drawn: drawn.length, total: detail.members.length };
}

/** The members of a cluster by kind, most first. */
export function kindsOfMembers(members: readonly Pick<MapNode, "kind">[]): { kind: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const member of members) counts.set(member.kind, (counts.get(member.kind) ?? 0) + 1);
  return [...counts.entries()].map(([kind, count]) => ({ kind, count })).sort((a, b) => b.count - a.count || a.kind.localeCompare(b.kind));
}

/** The ids the keyboard steps through, in the order a list beside the canvas would read them. */
export function stepOrder(elements: readonly ExplorerElement[]): string[] {
  return elements
    .filter((element) => element.group === "nodes" && !element.classes?.some((c) => c === "part" || c === "kind-hidden"))
    .sort((a, b) => {
      const size = Number(b.data.size ?? b.data.degree ?? 0) - Number(a.data.size ?? a.data.degree ?? 0);
      return size || String(a.data.label ?? "").localeCompare(String(b.data.label ?? ""));
    })
    .map((element) => String(element.data.id));
}
