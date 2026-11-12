import { type CanvasGraph, canvasGraph, mix, RING, restEdgeColour } from "./canvas-graph";
import type { Theme } from "./canvas-theme";
import { displayName, kindTone, shortLabel } from "./graph";
import type { Neighbourhood } from "./types";

/** The id prefix of the node that stands for what a group of direct neighbours holds beyond those drawn. */
export const MORE = "more\u0000";

/**
 * A node's neighbourhood (its page's graph) as the graph the canvas draws, the way the explorer draws a node's view: a
 * disc in each node's kind tone, the node shown larger with a ring in the brand colour, a hub with a soft ring, and per
 * group of direct neighbours that holds more than the view draws, one hollow "+N kind" node; its relations are arrows.
 */
export function neighbourhoodGraph(view: Neighbourhood, theme: Theme): CanvasGraph {
  const graph = canvasGraph();
  const rest = restEdgeColour(theme);
  for (const node of view.nodes) {
    const focus = node.id === view.focus;
    graph.addNode(node.id, {
      label: shortLabel(displayName(node)),
      kind: node.kind,
      x: 0,
      y: 0,
      size: focus ? 13 : 8,
      color: theme.tones[kindTone(node.kind)],
      ringColor: focus ? theme.brand : node.hub ? theme.muted : theme.surface,
      ring: focus ? RING.strong : node.hub ? RING.soft : 0,
      weight: focus ? Number.MAX_SAFE_INTEGER : (node.degree ?? 0),
      anchor: focus ? undefined : view.focus,
    });
  }
  for (const edge of view.edges) {
    if (!graph.hasNode(edge.src) || !graph.hasNode(edge.dst) || graph.hasEdge(edge.id)) continue;
    graph.addDirectedEdgeWithKey(edge.id, edge.src, edge.dst, { label: edge.rel, size: 1.2, color: rest, type: "arrow" });
  }
  if (!graph.hasNode(view.focus)) return graph;
  for (const group of view.groups) {
    const hidden = group.total - group.shown.length;
    if (hidden <= 0) continue;
    const id = `${MORE}${group.key}`;
    graph.addNode(id, {
      label: `+${hidden} ${group.kind}`,
      kind: group.kind,
      x: 0,
      y: 0,
      size: 8,
      color: theme.surface,
      ringColor: theme.subtle,
      ring: RING.hollow,
      weight: 0,
      anchor: view.focus,
    });
    const [source, target] = group.direction === "out" ? [view.focus, id] : [id, view.focus];
    graph.addDirectedEdgeWithKey(`${id}\u0000edge`, source, target, {
      label: group.rel,
      size: 1.2,
      color: mix(theme.subtle, theme.surface, 0.3),
      type: "arrow",
    });
  }
  return graph;
}
