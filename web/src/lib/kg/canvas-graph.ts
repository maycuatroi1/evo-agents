import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";

import type { Theme } from "./canvas-theme";

/**
 * The knowledge graph's canvases apart from the renderer: what a node and an edge carry on every canvas (the explorer's
 * scopes, a node's page, the Entities view), the layouts the canvases offer, and how a new graph takes the places the
 * last one had, so the reader keeps their bearings when a view grows. components/kg/sigma-canvas.tsx draws it with
 * sigma.js. Every node is a disc in its kind's colour; a ring marks the node a view grew from, a node expanded, a hub,
 * and a node that stands for more neighbours (hollow). Pure graphology, so it runs under vitest's jsdom.
 */
export type CanvasLayout = "force" | "radial" | "tree";

export type CanvasNode = {
  label: string;
  kind: string;
  x: number;
  y: number;
  /** The disc's radius on screen, in pixels, at the camera's first zoom. */
  size: number;
  color: string;
  /** A ring inside the disc's edge, in `ringColor`, `ring` of the radius wide; 0 for none (node-ring.ts). */
  ringColor: string;
  ring: number;
  /** What ranks the node for labels and for the radial layout's rings: a cluster's members, a node's edges. */
  weight: number;
  /** The node this one came from: it starts beside it. */
  anchor?: string;
  /** Drawn after the label as a small pill: an entity's code links. */
  badge?: string;
  hidden?: boolean;
};

export type CanvasEdge = {
  label: string;
  size: number;
  color: string;
  /** The sigma program: an arrow for an edge with a direction, a line otherwise. */
  type: "line" | "arrow";
  /** Shown only while one of its ends is lit: a cluster's weaker links. */
  weak?: boolean;
  /** Named only while one of its ends is lit, even with every relation named: an entity's link to code. */
  quiet?: boolean;
  hidden?: boolean;
};

export type CanvasGraph = Graph<CanvasNode, CanvasEdge>;

export function canvasGraph(): CanvasGraph {
  return new Graph<CanvasNode, CanvasEdge>({ type: "mixed", multi: true });
}

/** Ring widths, as a share of the radius: the view's own node and an expanded one, a hub, a hollow "+N" node. */
export const RING = { strong: 0.3, soft: 0.22, hollow: 0.34 } as const;

function parseHex(colour: string): [number, number, number] | null {
  const hex = colour.trim().replace(/^#/, "");
  const full = hex.length === 3 ? [...hex].map((c) => c + c).join("") : hex.slice(0, 6);
  if (!/^[0-9a-f]{6}$/i.test(full)) return null;
  return [0, 2, 4].map((at) => Number.parseInt(full.slice(at, at + 2), 16)) as [number, number, number];
}

/** `colour` taken `amount` of the way to `towards` (hex colours): a faded disc or line that stays opaque. */
export function mix(colour: string, towards: string, amount: number): string {
  const a = parseHex(colour);
  const b = parseHex(towards);
  if (!a || !b) return colour;
  const t = Math.min(1, Math.max(0, amount));
  return `#${a.map((value, i) => Math.round(value + (b[i]! - value) * t).toString(16).padStart(2, "0")).join("")}`;
}

/** The colour of an edge at rest on every canvas: the page's subtle text, most of the way to the surface. */
export function restEdgeColour(theme: Pick<Theme, "subtle" | "surface">): string {
  return mix(theme.subtle, theme.surface, 0.55);
}

/** A place in [-1, 1]² picked from the id, so the same graph starts the layout from the same places on every visit. */
export function seedPosition(id: string): { x: number; y: number } {
  let a = 2166136261;
  let b = 5381;
  for (const char of id) {
    a = Math.imul(a ^ char.charCodeAt(0), 16777619) >>> 0;
    b = ((b << 5) + b + char.charCodeAt(0)) >>> 0;
  }
  return { x: (a / 0xffffffff) * 2 - 1, y: (b / 0xffffffff) * 2 - 1 };
}

export type Place = { x: number; y: number };

/** The scale of the graph laid out before, from the corners of `places`; 100 for none. */
function spanOf(places: ReadonlyMap<string, Place>): { span: number; centre: Place } {
  if (places.size === 0) return { span: 100, centre: { x: 0, y: 0 } };
  let [minX, minY, maxX, maxY] = [Infinity, Infinity, -Infinity, -Infinity];
  for (const { x, y } of places.values()) {
    minX = Math.min(minX, x);
    minY = Math.min(minY, y);
    maxX = Math.max(maxX, x);
    maxY = Math.max(maxY, y);
  }
  return { span: Math.max(maxX - minX, maxY - minY, 100), centre: { x: (minX + maxX) / 2, y: (minY + maxY) / 2 } };
}

/**
 * Gives every node of `graph` its first place: the one it had in the graph drawn before (`previous`), else beside its
 * anchor or a neighbour drawn before, else a place seeded by its id. Returns the ids new to the view, in the graph's
 * order; the canvas lays the view out again when there are any.
 */
export function placeNodes(graph: Graph<CanvasNode, CanvasEdge>, previous: ReadonlyMap<string, Place>): string[] {
  const { span, centre } = spanOf(previous);
  const near = span * 0.04;
  const fresh: string[] = [];
  graph.forEachNode((node) => {
    const known = previous.get(node);
    if (known) graph.mergeNodeAttributes(node, { x: known.x, y: known.y });
    else fresh.push(node);
  });
  for (const node of fresh) {
    const { anchor } = graph.getNodeAttributes(node);
    const from = anchor && previous.has(anchor) ? anchor : graph.neighbors(node).find((other) => previous.has(other));
    const seed = seedPosition(node);
    const at = from ? previous.get(from)! : null;
    graph.mergeNodeAttributes(
      node,
      at ? { x: at.x + seed.x * near, y: at.y + seed.y * near } : { x: centre.x + (seed.x * span) / 2, y: centre.y + (seed.y * span) / 2 },
    );
  }
  return fresh;
}

/** Room between two discs side by side, in pixels at the first zoom. */
const GAP = 22;

const byWeight = (graph: Graph<CanvasNode, CanvasEdge>) => (a: string, b: string) =>
  graph.getNodeAttribute(b, "weight") - graph.getNodeAttribute(a, "weight") || (a < b ? -1 : a > b ? 1 : 0);

/** Steps of every node from `root`, the edges taken either way; nodes out of its reach are left out. */
function stepsFrom(graph: Graph<CanvasNode, CanvasEdge>, root: string): Map<string, number> {
  const out = new Map([[root, 0]]);
  const queue = [root];
  for (let at = 0; at < queue.length; at += 1) {
    const node = queue[at]!;
    const next = graph.neighbors(node).filter((other) => !out.has(other)).sort(byWeight(graph));
    for (const other of next) {
      out.set(other, out.get(node)! + 1);
      queue.push(other);
    }
  }
  return out;
}

/**
 * The ring of each node in the radial layout: its steps from `root` when there is one (a node's view, a node's page),
 * else one of four bands of weight with the heaviest in the middle (the overview's and a cluster's circles).
 */
export function radialLevels(graph: Graph<CanvasNode, CanvasEdge>, root: string | null): Map<string, number> {
  const out = new Map<string, number>();
  if (root && graph.hasNode(root)) {
    const steps = stepsFrom(graph, root);
    const beyond = Math.max(0, ...steps.values()) + 1;
    graph.forEachNode((node) => out.set(node, steps.get(node) ?? beyond));
    return out;
  }
  const heaviest = Math.max(1, ...graph.mapNodes((_, attributes) => attributes.weight));
  graph.forEachNode((node, attributes) => out.set(node, Math.min(3, Math.floor(((heaviest - attributes.weight) / heaviest) * 4))));
  return out;
}

/** Rings around the middle, the first ring alone in the middle when it holds one node; each ring wide enough for its discs. */
export function radialPositions(graph: Graph<CanvasNode, CanvasEdge>, root: string | null): Map<string, Place> {
  const levels = radialLevels(graph, root);
  const rings = new Map<number, string[]>();
  for (const [node, level] of levels) rings.set(level, [...(rings.get(level) ?? []), node]);
  const size = (node: string) => graph.getNodeAttribute(node, "size");
  const out = new Map<string, Place>();
  let radius = 0;
  let inner = 0;
  for (const level of [...rings.keys()].sort((a, b) => a - b)) {
    const nodes = rings.get(level)!.sort(byWeight(graph));
    const widest = Math.max(...nodes.map(size));
    if (out.size === 0 && nodes.length === 1) {
      out.set(nodes[0]!, { x: 0, y: 0 });
      inner = widest;
      continue;
    }
    const around = nodes.reduce((sum, node) => sum + 2 * size(node) + GAP, 0);
    radius = Math.max(radius + inner + widest + 2 * GAP, around / (2 * Math.PI));
    let angle = -Math.PI / 2;
    for (const node of nodes) {
      const share = ((2 * size(node) + GAP) / around) * 2 * Math.PI;
      out.set(node, { x: radius * Math.cos(angle + share / 2), y: radius * Math.sin(angle + share / 2) });
      angle += share;
    }
    inner = widest;
  }
  return out;
}

/**
 * Rows by steps from `root` (else from the heaviest node), each row centred under the one before, a node beside the
 * others its parent brought in; nodes out of reach make a last row.
 */
export function treePositions(graph: Graph<CanvasNode, CanvasEdge>, root: string | null): Map<string, Place> {
  const out = new Map<string, Place>();
  if (graph.order === 0) return out;
  const start = root && graph.hasNode(root) ? root : graph.nodes().sort(byWeight(graph))[0]!;
  const steps = stepsFrom(graph, start);
  const beyond = Math.max(0, ...steps.values()) + 1;
  const rows = new Map<number, string[]>();
  // The search's order of visit keeps the children of one parent together in their row.
  const visited = [...steps.keys(), ...graph.nodes().filter((node) => !steps.has(node))];
  for (const node of visited) {
    const row = steps.get(node) ?? beyond;
    rows.set(row, [...(rows.get(row) ?? []), node]);
  }
  const size = (node: string) => graph.getNodeAttribute(node, "size");
  const tallest = Math.max(...graph.mapNodes((node) => size(node)));
  const rowGap = 2 * tallest + 4 * GAP;
  for (const [row, nodes] of rows) {
    const width = nodes.reduce((sum, node) => sum + 2 * size(node) + GAP, 0);
    let x = -width / 2;
    for (const node of nodes) {
      out.set(node, { x: x + size(node) + GAP / 2, y: -row * rowGap });
      x += 2 * size(node) + GAP;
    }
  }
  return out;
}

/** ForceAtlas2 rounds: more for a small graph, fewer once places come from the graph before. */
export function forceIterations(order: number, settled: boolean): number {
  if (settled) return 40;
  return order > 600 ? 120 : order > 200 ? 200 : 300;
}

/** ForceAtlas2 from the places the nodes have now; discs keep apart by their sizes. */
export function forcePositions(graph: Graph<CanvasNode, CanvasEdge>, settled: boolean): Map<string, Place> {
  if (graph.order < 2) return new Map(graph.mapNodes((node) => [node, { x: 0, y: 0 }] as const));
  const positions = forceAtlas2(graph, {
    iterations: forceIterations(graph.order, settled),
    settings: { ...forceAtlas2.inferSettings(graph), adjustSizes: true, barnesHutOptimize: graph.order > 300 },
    getEdgeWeight: "size",
  });
  return new Map(Object.entries(positions));
}

/** Where the nodes go for `layout`. `settled`: most places come from the graph before, so the force layout runs short. */
export function layoutPositions(
  graph: Graph<CanvasNode, CanvasEdge>,
  layout: CanvasLayout,
  { root, settled }: { root: string | null; settled: boolean },
): Map<string, Place> {
  if (layout === "radial") return radialPositions(graph, root);
  if (layout === "tree") return treePositions(graph, root);
  return forcePositions(graph, settled);
}
