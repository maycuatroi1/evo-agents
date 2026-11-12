import type Graph from "graphology";
import type { EdgeDisplayData } from "sigma/types";

import type { CanvasEdge, CanvasNode } from "@/lib/kg/canvas-graph";
import type { Theme } from "@/lib/kg/canvas-theme";

import type { Drawn } from "./canvas-drawers";

/**
 * What a graph canvas (./sigma-canvas.tsx) draws of each node and edge for what the reader points at and selects: the
 * node under the pointer (else the one a list points at, else the one selected) lights with its neighbours and their
 * relations, which are named; the rest dims and its edges hide. Only the labels canvas-labels.ts chose show.
 */
export type ViewState = {
  hovered: string | null;
  hoveredEdge: string | null;
  selected: string | null;
  highlight: string | null;
  selectedEdge: string | null;
  allEdgeLabels: boolean;
  theme: Theme;
};

type DrawnGraph = Graph<CanvasNode, CanvasEdge> | null;

function focusOf(state: ViewState, g: Graph<CanvasNode, CanvasEdge>): string | null {
  const pointed = state.hovered ?? state.highlight ?? state.selected;
  return pointed && g.hasNode(pointed) && !g.getNodeAttribute(pointed, "hidden") ? pointed : null;
}

export function reduceNode(
  node: string,
  attributes: CanvasNode,
  state: ViewState,
  g: DrawnGraph,
  labels: ReadonlySet<string> | null,
  describe: ((id: string) => string | null) | undefined,
): Partial<Drawn> {
  const out: Partial<Drawn> = { ...attributes };
  if (attributes.hidden || !g) return { ...out, hidden: true };
  const focus = focusOf(state, g);
  if (node === state.selected || node === state.hovered) {
    out.highlighted = true;
    out.zIndex = 2;
    out.forceLabel = true;
  }
  if (focus && node !== focus && !g.areNeighbors(focus, node)) {
    out.color = state.theme.border;
    out.ringColor = state.theme.border;
    out.label = "";
    out.badge = undefined;
    out.zIndex = 0;
  } else if (focus && node !== focus) {
    out.forceLabel = true;
    out.zIndex = 1;
  }
  if (state.selectedEdge && g.hasEdge(state.selectedEdge) && g.hasExtremity(state.selectedEdge, node)) {
    out.forceLabel = true;
    out.highlighted = true;
  }
  if (labels && !labels.has(node) && node !== state.selected && node !== state.hovered) {
    out.label = "";
    out.badge = undefined;
  } else if (labels) {
    out.forceLabel = true;
  }
  if (node === state.hovered) out.detail = describe?.(node) ?? undefined;
  return out;
}

export function reduceEdge(edge: string, attributes: CanvasEdge, state: ViewState, g: DrawnGraph): Partial<EdgeDisplayData & CanvasEdge> {
  const out: Partial<EdgeDisplayData & CanvasEdge> = { ...attributes };
  if (!g || !g.hasEdge(edge)) return out;
  const [source, target] = g.extremities(edge);
  if (attributes.hidden || g.getNodeAttribute(source, "hidden") || g.getNodeAttribute(target, "hidden")) return { ...out, hidden: true };
  const focus = focusOf(state, g);
  const touches = focus === source || focus === target;
  const pointed = edge === state.hoveredEdge || edge === state.selectedEdge;
  if (pointed || touches) {
    out.color = state.theme.brand;
    out.size = attributes.size + (pointed ? 1.5 : 0.5);
    out.zIndex = 1;
  } else if (focus || attributes.weak) {
    out.hidden = true;
  }
  out.label = pointed || touches || (state.allEdgeLabels && !attributes.weak && !attributes.quiet) ? attributes.label : "";
  return out;
}
