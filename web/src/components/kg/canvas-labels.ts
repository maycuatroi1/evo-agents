import type Graph from "graphology";
import type Sigma from "sigma";

import type { CanvasEdge, CanvasNode } from "@/lib/kg/canvas-graph";

/**
 * Which labels a graph canvas (./sigma-canvas.tsx) shows, so no two ever cover each other: sigma's own label grid only
 * thins them out. Labels keep their size on screen, so zooming in makes room and more of them show.
 */

/** Pixels between a disc and its label, and kept around a label shown. */
export const LABEL_GAP = 3;
const PAD = 3;
/** The size and weight of a label on every canvas (sigma's labelSize and labelWeight). */
export const LABEL_SIZE = 12;
export const LABEL_WEIGHT = "500";

/** A label goes right of its disc, or left when it would run past the canvas's right edge and fits on the left. */
export function labelOnLeft(x: number, radius: number, width: number, canvasWidth: number): boolean {
  return x + radius + LABEL_GAP + width > canvasWidth - 4 && x - radius - LABEL_GAP - width >= 4;
}

let measurer: CanvasRenderingContext2D | null | undefined;

function textWidth(text: string, font: string): number {
  if (measurer === undefined) measurer = document.createElement("canvas").getContext("2d");
  if (!measurer) return text.length * 7;
  measurer.font = font;
  return measurer.measureText(text).width;
}

/** The width of a node's label and badge on screen, as canvas-drawers.ts draws them. */
export function labelWidth(label: string, badge: string | undefined, font: string): number {
  const name = textWidth(label, `${LABEL_WEIGHT} ${LABEL_SIZE}px ${font}`);
  return badge ? name + 4 + textWidth(badge, `600 ${LABEL_SIZE - 2}px ${font}`) + 8 : name;
}

type Box = { x1: number; y1: number; x2: number; y2: number };

function overlaps(a: Box, b: Box): boolean {
  return !(a.x2 + PAD < b.x1 || a.x1 - PAD > b.x2 || a.y2 + PAD < b.y1 || a.y1 - PAD > b.y2);
}

export type LabelState = { selected: string | null; hovered: string | null; highlight: string | null; selectedEdge: string | null; font: string };

/**
 * The nodes whose labels show, the more important first: the node selected or under the pointer, and the ends of the
 * relation selected, always; then, while a node is lit, its neighbours (the rest are dimmed and show none); then the
 * heaviest. A label shows when it covers no label already shown and no disc whose label shows; one wholly off the
 * canvas shows nothing until a pan brings it in. Call it again after a zoom, a pan, a layout or a change of what is lit.
 */
export function chooseLabels(renderer: Sigma<CanvasNode, CanvasEdge>, graph: Graph<CanvasNode, CanvasEdge>, state: LabelState): Set<string> {
  const pointed = state.hovered ?? state.highlight ?? state.selected;
  const focus = pointed && graph.hasNode(pointed) && !graph.getNodeAttribute(pointed, "hidden") ? pointed : null;
  const lit = focus ? new Set([focus, ...graph.neighbors(focus)]) : null;
  const forced = new Set([state.selected, state.hovered, ...(state.selectedEdge && graph.hasEdge(state.selectedEdge) ? graph.extremities(state.selectedEdge) : [])]);
  const { width, height } = renderer.getDimensions();
  const entries: { node: string; x: number; y: number; radius: number; rank: number; weight: number; label: string; badge?: string }[] = [];
  graph.forEachNode((node, attributes) => {
    if (attributes.hidden || !attributes.label || (lit && !lit.has(node))) return;
    const data = renderer.getNodeDisplayData(node);
    if (!data || data.hidden) return;
    const at = renderer.framedGraphToViewport(data);
    const rank = forced.has(node) ? 2 : lit ? 1 : 0;
    entries.push({ node, x: at.x, y: at.y, radius: renderer.scaleSize(data.size), rank, weight: attributes.weight, label: attributes.label, badge: attributes.badge });
  });
  entries.sort((a, b) => b.rank - a.rank || b.weight - a.weight || (a.node < b.node ? -1 : 1));
  const shown = new Set<string>();
  const taken: Box[] = [];
  for (const entry of entries) {
    const span = labelWidth(entry.label, entry.badge, state.font);
    const left = labelOnLeft(entry.x, entry.radius, span, width);
    const x1 = left ? entry.x - entry.radius - LABEL_GAP - span : entry.x + entry.radius + LABEL_GAP;
    const label = { x1, y1: entry.y - LABEL_SIZE / 2 - 1, x2: x1 + span, y2: entry.y + LABEL_SIZE / 2 + 1 };
    const body = { x1: entry.x - entry.radius, y1: entry.y - entry.radius, x2: entry.x + entry.radius, y2: entry.y + entry.radius };
    if (entry.rank < 2) {
      const onScreen = label.x2 > 0 && label.x1 < width && label.y2 > 0 && label.y1 < height;
      if (!onScreen || taken.some((box) => overlaps(box, label))) continue;
    }
    shown.add(entry.node);
    taken.push(label, body);
  }
  return shown;
}
