import dagre from "@dagrejs/dagre";

import type { NodeKind, Position, Spec, SpecNode } from "./systems";

/**
 * The slot every node is laid out in. A spec keeps a node's position as the top-left corner of its slot, and the
 * canvas draws the node's shape in the middle of it, so a spec saved when every node was a 220 x 76 box keeps its
 * places now that each kind has a shape of its own.
 */
export const SLOT_WIDTH = 220;
export const SLOT_HEIGHT = 76;

/** The size the canvas draws each kind at: agents round with their avatar, a graph a tile, the entry a pill. */
export const NODE_SIZE: Record<NodeKind, { width: number; height: number }> = {
  entry: { width: 176, height: 48 },
  agent: { width: 60, height: 60 },
  orchestrator: { width: 68, height: 68 },
  kg: { width: 208, height: 56 },
  curator: { width: 52, height: 52 },
};

/** The kinds drawn as a circle: a line ends on the circle rather than on a box. */
export const ROUND_KINDS: ReadonlySet<NodeKind> = new Set<NodeKind>(["agent", "orchestrator"]);

/** Where the canvas draws a node of `kind` whose slot starts at `slot`. */
export function drawnAt(kind: NodeKind, slot: Position): Position {
  const size = NODE_SIZE[kind];
  return { x: slot.x + (SLOT_WIDTH - size.width) / 2, y: slot.y + (SLOT_HEIGHT - size.height) / 2 };
}

/** The slot of a node of `kind` the canvas draws at `drawn`: the inverse of `drawnAt`. */
export function slotOf(kind: NodeKind, drawn: Position): Position {
  const size = NODE_SIZE[kind];
  return { x: drawn.x - (SLOT_WIDTH - size.width) / 2, y: drawn.y - (SLOT_HEIGHT - size.height) / 2 };
}

/** Between the slots of two ranks of the flow, and between two slots sharing a rank (an agent's name hangs below). */
const RANK_GAP = 110;
const ROW_GAP = 84;
/** From the centre of the flow's top row to the centre of the band above it, and from its bottom row to the band below. */
const ABOVE = 170;
const BELOW = 190;
/** The least distance between the centres of two slots in a band. */
const BAND_STEP = SLOT_WIDTH + 40;

type Centre = { x: number; y: number };

function mean(values: readonly number[]): number | null {
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
}

/**
 * The x of each node of a band, in its order of wish: at least BAND_STEP apart, and as a group centred where the
 * wishes are, so a lone node stands right where it wishes.
 */
function spread(wishes: readonly { id: string; x: number }[]): Map<string, number> {
  const sorted = [...wishes].sort((a, b) => a.x - b.x);
  const xs: number[] = [];
  for (const [index, wish] of sorted.entries()) xs.push(index === 0 ? wish.x : Math.max(wish.x, xs[index - 1] + BAND_STEP));
  const shift = (mean(sorted.map((wish) => wish.x)) ?? 0) - (mean(xs) ?? 0);
  return new Map(sorted.map((wish, index) => [wish.id, xs[index] + shift]));
}

/**
 * Auto layout of a system in three bands. The flow (the entry and the agents, joined by Talks to) runs left to right
 * in the middle, ranked by dagre; the orchestrator stands above the agents it directs and the Curator above the start
 * of the flow; each knowledge graph sits below the agents that read it. Lines that are not the flow then run up or
 * down to their band instead of looping back across it. Positions are the slots' top-left corners, rounded to whole
 * pixels as the spec keeps them.
 */
export function autoLayout(spec: Spec): Map<string, Position> {
  const flow = spec.nodes.filter((node) => node.kind === "entry" || node.kind === "agent");
  const graph = new dagre.graphlib.Graph({ multigraph: true });
  graph.setGraph({ rankdir: "LR", nodesep: ROW_GAP, ranksep: RANK_GAP, marginx: 0, marginy: 0 });
  graph.setDefaultEdgeLabel(() => ({}));
  for (const node of flow) graph.setNode(node.id, { width: SLOT_WIDTH, height: SLOT_HEIGHT });
  spec.edges.forEach((edge, index) => {
    if (edge.kind !== "interacts" || !graph.hasNode(edge.from) || !graph.hasNode(edge.to)) return;
    graph.setEdge(edge.from, edge.to, { weight: 2, minlen: 1 }, `e${index}`);
  });
  dagre.layout(graph);

  const centres = new Map<string, Centre>();
  for (const node of flow) {
    const box = graph.node(node.id);
    centres.set(node.id, { x: box?.x ?? 0, y: box?.y ?? 0 });
  }
  const flowXs = [...centres.values()].map((centre) => centre.x);
  const flowYs = [...centres.values()].map((centre) => centre.y);
  const top = flowYs.length ? Math.min(...flowYs) : 0;
  const bottom = flowYs.length ? Math.max(...flowYs) : 0;
  const middle = mean(flowXs) ?? 0;
  const start = flowXs.length ? Math.min(...flowXs) : 0;
  const end = flowXs.length ? Math.max(...flowXs) : 0;
  const xOf = (ids: readonly string[]) => mean(ids.flatMap((id) => (centres.has(id) ? [centres.get(id)!.x] : [])));

  /** The agents an orchestrator directs: those it orchestrates and those that escalate to it. */
  const directedBy = (node: SpecNode) =>
    spec.edges.flatMap((edge) =>
      edge.kind === "orchestrates" && edge.from === node.id ? [edge.to] : edge.kind === "escalates" && edge.to === node.id ? [edge.from] : [],
    );
  const readersOf = (node: SpecNode) => spec.edges.flatMap((edge) => (edge.kind === "uses_kg" && edge.to === node.id ? [edge.from] : []));

  const above = spec.nodes
    .filter((node) => node.kind === "orchestrator" || node.kind === "curator")
    .map((node) => ({ id: node.id, x: node.kind === "curator" ? start - SLOT_WIDTH / 2 : (xOf(directedBy(node)) ?? middle) }));
  let after = end;
  const below = spec.nodes
    .filter((node) => node.kind === "kg")
    .map((node) => {
      const readers = readersOf(node);
      const x = xOf(readers);
      if (x !== null) return { id: node.id, x };
      after += BAND_STEP;
      return { id: node.id, x: after };
    });

  for (const [id, x] of spread(above)) centres.set(id, { x, y: top - ABOVE });
  for (const [id, x] of spread(below)) centres.set(id, { x, y: bottom + BELOW });

  const placed = new Map<string, Position>();
  for (const node of spec.nodes) {
    const centre = centres.get(node.id) ?? { x: 0, y: 0 };
    placed.set(node.id, { x: Math.round(centre.x - SLOT_WIDTH / 2), y: Math.round(centre.y - SLOT_HEIGHT / 2) });
  }
  return placed;
}

/** The room a node's words take beyond its slot: a name over the orchestrator, under an agent, beside the Curator. */
const WORDS_ABOVE = 40;
const WORDS_BELOW = 56;
const WORDS_BESIDE = 120;

/** The box `spec`'s nodes take on the canvas, their words included, to fit the view to before React Flow has drawn them. */
export function boundsOf(spec: Spec): { x: number; y: number; width: number; height: number } {
  if (!spec.nodes.length) return { x: 0, y: 0, width: SLOT_WIDTH, height: SLOT_HEIGHT };
  const left = Math.min(...spec.nodes.map((node) => node.position.x));
  const top = Math.min(...spec.nodes.map((node) => node.position.y - WORDS_ABOVE));
  const right = Math.max(...spec.nodes.map((node) => node.position.x + SLOT_WIDTH + (node.kind === "curator" ? WORDS_BESIDE : 0)));
  const bottom = Math.max(...spec.nodes.map((node) => node.position.y + SLOT_HEIGHT + WORDS_BELOW));
  return { x: left, y: top, width: right - left, height: bottom - top };
}

/** `spec` with every node at its auto-layout place. */
export function laidOut(spec: Spec): Spec {
  const placed = autoLayout(spec);
  return { ...spec, nodes: spec.nodes.map((node) => ({ ...node, position: placed.get(node.id) ?? node.position })) };
}
