import type { GraphEdge, GraphNode, Neighbourhood, Relation } from "./types";

/**
 * The graph view's model, independent of the canvas: how each kind of node is drawn (a chart colour and a shape,
 * so colour is never the only signal), the order nodes are stepped through by keyboard, and the neighbours of the
 * selected node that the table beside the canvas lists.
 */

/** The chart tokens of DESIGN.md: 1 blue, 2 amber, 3 emerald, 4 violet, 5 slate. */
export type Tone = 1 | 2 | 3 | 4 | 5;
export type Shape =
  | "ellipse"
  | "round-rectangle"
  | "rectangle"
  | "diamond"
  | "hexagon"
  | "octagon"
  | "triangle"
  | "pentagon"
  | "tag";

export type KindStyle = { tone: Tone; shape: Shape };

/** Prose in blue, code in emerald, plans in violet, requirements and tickets in amber, the rest in slate. */
const KIND_STYLES: Record<string, KindStyle> = {
  Document: { tone: 1, shape: "round-rectangle" },
  Section: { tone: 1, shape: "ellipse" },
  File: { tone: 3, shape: "rectangle" },
  Directory: { tone: 3, shape: "tag" },
  Repo: { tone: 3, shape: "octagon" },
  Symbol: { tone: 3, shape: "ellipse" },
  Commit: { tone: 3, shape: "triangle" },
  Plan: { tone: 4, shape: "hexagon" },
  PlanStep: { tone: 4, shape: "ellipse" },
  Seam: { tone: 4, shape: "octagon" },
  Requirement: { tone: 2, shape: "diamond" },
  UseCase: { tone: 2, shape: "pentagon" },
  Ticket: { tone: 2, shape: "tag" },
  Source: { tone: 5, shape: "hexagon" },
  Person: { tone: 5, shape: "triangle" },
};
const OTHER_KIND: KindStyle = { tone: 5, shape: "ellipse" };

export function kindStyle(kind: string): KindStyle {
  return KIND_STYLES[kind] ?? OTHER_KIND;
}

/** Points of each shape on [-1, 1], as Cytoscape draws them; the legend draws the same outline in SVG. */
export const SHAPE_POINTS: Record<Exclude<Shape, "ellipse" | "round-rectangle" | "rectangle">, string> = {
  diamond: "0,-1 1,0 0,1 -1,0",
  hexagon: "-1,0 -0.5,-0.87 0.5,-0.87 1,0 0.5,0.87 -0.5,0.87",
  octagon: "-0.41,-1 0.41,-1 1,-0.41 1,0.41 0.41,1 -0.41,1 -1,0.41 -1,-0.41",
  triangle: "0,-1 1,1 -1,1",
  pentagon: "0,-1 0.95,-0.31 0.59,0.81 -0.59,0.81 -0.95,-0.31",
  tag: "-1,-1 0.25,-1 1,0 0.25,1 -1,1",
};

export function displayName(node: { id: string; name?: string | null }): string {
  return node.name && node.name.trim() ? node.name : node.id;
}

/** A label short enough to draw under a node. */
export function shortLabel(text: string, max = 28): string {
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}

const collator = new Intl.Collator("vi", { sensitivity: "base", numeric: true });

/** The node itself first, then by step, kind and name: the order the keyboard walks and the table lists. */
export function orderNodes(nodes: readonly GraphNode[]): GraphNode[] {
  return [...nodes].sort(
    (a, b) =>
      a.hop - b.hop ||
      collator.compare(a.kind, b.kind) ||
      collator.compare(displayName(a), displayName(b)) ||
      collator.compare(a.id, b.id),
  );
}

export type Direction = "out" | "in";

export type NeighbourRow = {
  edge: GraphEdge;
  direction: Direction;
  node: GraphNode;
};

/**
 * The edges of the view that touch `id`, each with the node at the other end: what the table beside the canvas
 * lists for the selected node. Sorted by edge type, then direction (outgoing first), then name.
 */
export function neighboursOf(view: Pick<Neighbourhood, "nodes" | "edges">, id: string): NeighbourRow[] {
  const byId = new Map(view.nodes.map((node) => [node.id, node]));
  const rows: NeighbourRow[] = [];
  for (const edge of view.edges) {
    if (edge.src !== id && edge.dst !== id) continue;
    const direction: Direction = edge.src === id ? "out" : "in";
    const other = byId.get(direction === "out" ? edge.dst : edge.src);
    if (other) rows.push({ edge, direction, node: other });
  }
  return rows.sort(
    (a, b) =>
      collator.compare(a.edge.rel, b.edge.rel) ||
      (a.direction === b.direction ? 0 : a.direction === "out" ? -1 : 1) ||
      collator.compare(displayName(a.node), displayName(b.node)) ||
      collator.compare(a.node.id, b.node.id),
  );
}

/** The node after (or before) `current` in `ordered`, wrapping around. */
export function step(ordered: readonly GraphNode[], current: string, delta: 1 | -1): string {
  if (ordered.length === 0) return current;
  const index = ordered.findIndex((node) => node.id === current);
  const next = index < 0 ? 0 : (index + delta + ordered.length) % ordered.length;
  return ordered[next].id;
}

/** Relations of a node, grouped by edge type and direction, in the order of `neighboursOf`. */
export type RelationGroup = { key: string; rel: string; direction: Direction; relations: Relation[] };

export function groupRelations(outgoing: readonly Relation[], incoming: readonly Relation[]): RelationGroup[] {
  const groups = new Map<string, RelationGroup>();
  const add = (relation: Relation, direction: Direction) => {
    const key = `${relation.rel}:${direction}`;
    const group = groups.get(key) ?? { key, rel: relation.rel, direction, relations: [] };
    group.relations.push(relation);
    groups.set(key, group);
  };
  for (const relation of outgoing) add(relation, "out");
  for (const relation of incoming) add(relation, "in");
  return [...groups.values()].sort(
    (a, b) =>
      collator.compare(a.rel, b.rel) || (a.direction === b.direction ? 0 : a.direction === "out" ? -1 : 1),
  );
}

/** The kinds a view shows, in the order of `orderNodes`, for the legend. */
export function kindsOf(nodes: readonly GraphNode[]): string[] {
  return [...new Set(nodes.map((node) => node.kind))].sort((a, b) => collator.compare(a, b));
}
