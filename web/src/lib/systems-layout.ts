import dagre from "@dagrejs/dagre";

import type { Position, Spec } from "./systems";

/** The size the designer draws a node at, which the layout leaves room for. */
export const NODE_WIDTH = 220;
export const NODE_HEIGHT = 76;

/**
 * Auto layout of a system: dagre ranks the nodes left to right along their edges, so the entry stands first, the
 * agents it talks to after it, and the graphs the agents use after them. An escalates edge points back at the
 * orchestrator, which already orchestrates the agents: it is laid out reversed, so the orchestrator stays before them.
 * Positions are the nodes' top-left corners, rounded to whole pixels as the spec keeps them.
 */
export function autoLayout(spec: Spec): Map<string, Position> {
  const graph = new dagre.graphlib.Graph({ multigraph: true });
  graph.setGraph({ rankdir: "LR", nodesep: 40, ranksep: 90, marginx: 0, marginy: 0 });
  graph.setDefaultEdgeLabel(() => ({}));
  for (const node of spec.nodes) graph.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  spec.edges.forEach((edge, index) => {
    if (!graph.hasNode(edge.from) || !graph.hasNode(edge.to)) return;
    const [from, to] = edge.kind === "escalates" ? [edge.to, edge.from] : [edge.from, edge.to];
    graph.setEdge(from, to, { weight: edge.kind === "optimizes" ? 1 : 2, minlen: 1 }, `e${index}`);
  });
  dagre.layout(graph);
  const placed = new Map<string, Position>();
  for (const node of spec.nodes) {
    const box = graph.node(node.id);
    placed.set(node.id, { x: Math.round((box?.x ?? 0) - NODE_WIDTH / 2), y: Math.round((box?.y ?? 0) - NODE_HEIGHT / 2) });
  }
  return placed;
}

/** `spec` with every node at its auto-layout place. */
export function laidOut(spec: Spec): Spec {
  const placed = autoLayout(spec);
  return { ...spec, nodes: spec.nodes.map((node) => ({ ...node, position: placed.get(node.id) ?? node.position })) };
}
