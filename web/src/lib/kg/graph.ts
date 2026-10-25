import type { GraphEdge, GraphNode, Neighbourhood, NeighbourGroup, Relation } from "./types";

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

/**
 * Prose in blue, code in emerald, plans in violet, requirements, tickets and a canvas's claims in amber, the rest in
 * slate (a signal key among them, a measure from outside like a source).
 */
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
  Claim: { tone: 2, shape: "hexagon" },
  Source: { tone: 5, shape: "hexagon" },
  Person: { tone: 5, shape: "triangle" },
  SignalKey: { tone: 5, shape: "diamond" },
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

/** File names that say little alone: the label keeps their folder ("kg/page.tsx"). */
const GENERIC_FILES = new Set(["page.tsx", "layout.tsx", "loading.tsx", "route.ts", "index.ts", "index.tsx", "__init__.py", "README.md"]);

/**
 * The label the explorer draws for a node: a path's last part (its folder too when the file's name is generic), so a
 * view of one folder's files does not repeat the folder on every label; the whole name is in the tooltip and the
 * inspector. Anything else as it is, shortened.
 */
export function nodeLabel(name: string): string {
  const parts = name.split("/").filter(Boolean);
  if (parts.length < 2) return shortLabel(name);
  const last = parts[parts.length - 1]!;
  return shortLabel(GENERIC_FILES.has(last) ? `${parts[parts.length - 2]}/${last}` : last);
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

/**
 * The neighbours of `id` the table lists, in groups by edge type, direction and kind of the node at the other end.
 * For the node shown, the groups are the API's: each lists the neighbours the view holds, the most connected first,
 * and says how many more it has (`hidden`), which opening the group shows. For another node selected in the graph,
 * the groups are of the edges the view holds, and hide nothing.
 */
export type RowGroup = Pick<NeighbourGroup, "key" | "rel" | "direction" | "kind" | "total"> & {
  rows: NeighbourRow[];
  hidden: number;
};

export function groupKey(direction: Direction, rel: string, kind: string): string {
  return `${direction}:${rel}:${kind}`;
}

export function neighbourGroups(view: Pick<Neighbourhood, "focus" | "nodes" | "edges" | "groups">, id: string): RowGroup[] {
  const rows = neighboursOf(view, id);
  const byKey = new Map<string, NeighbourRow[]>();
  for (const row of rows) {
    const key = groupKey(row.direction, row.edge.rel, row.node.kind);
    byKey.set(key, [...(byKey.get(key) ?? []), row]);
  }
  if (id === view.focus && view.groups.length > 0) {
    return view.groups.map((group) => {
      const found = byKey.get(group.key) ?? [];
      const place = new Map(group.shown.map((node, index) => [node, index]));
      const listed = found
        .filter((row) => place.has(row.node.id))
        .sort((a, b) => (place.get(a.node.id) ?? 0) - (place.get(b.node.id) ?? 0));
      return { ...group, rows: listed, hidden: Math.max(0, group.total - group.shown.length) };
    });
  }
  return [...byKey.entries()]
    .map(([key, listed]) => {
      const [first] = listed;
      return {
        key,
        rel: first.edge.rel,
        direction: first.direction,
        kind: first.node.kind,
        total: new Set(listed.map((row) => row.node.id)).size,
        rows: listed,
        hidden: 0,
      };
    })
    .sort(
      (a, b) =>
        collator.compare(a.rel, b.rel) ||
        (a.direction === b.direction ? 0 : a.direction === "out" ? -1 : 1) ||
        collator.compare(a.kind, b.kind),
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
