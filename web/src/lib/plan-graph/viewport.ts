import type { PlanGraph } from "./layout";

/** Where the plan graph's canvas opens and what Fit shows. Pure: no React, no DOM. */

export const FIT = { maxZoom: 1, padding: 0.06 };

/** Below this zoom a step's 12 px title is too small to read, so the canvas opens no smaller and starts at the top. */
export const OPENING_MIN_ZOOM = 0.6;

export type Viewport = { x: number; y: number; zoom: number };

/**
 * Where the canvas opens: the whole plan fitted (centred, at most zoom 1) when that reads at OPENING_MIN_ZOOM or more;
 * otherwise at that zoom with the first row at the top and the plan's first step on it centred across, and the rest reached by panning, zooming out
 * or Fit. Pure, from the layout's size and the canvas's.
 */
export function openingViewport(graph: Pick<PlanGraph, "nodes" | "width" | "height">, frame: { width: number; height: number }): Viewport {
  const padX = frame.width * FIT.padding;
  const padY = frame.height * FIT.padding;
  if (graph.nodes.length === 0 || graph.width <= 0 || graph.height <= 0) return { x: padX, y: padY, zoom: 1 };
  const fit = Math.min(FIT.maxZoom, (frame.width - 2 * padX) / graph.width, (frame.height - 2 * padY) / graph.height);
  if (fit >= OPENING_MIN_ZOOM) {
    return { x: (frame.width - graph.width * fit) / 2, y: (frame.height - graph.height * fit) / 2, zoom: fit };
  }
  const zoom = OPENING_MIN_ZOOM;
  const wide = graph.width * zoom;
  if (wide <= frame.width - 2 * padX) return { x: (frame.width - wide) / 2, y: padY, zoom };
  // The plan's first step on the top row (nodes are in plan order), where a reader starts.
  const start = graph.nodes.find((node) => node.row === 0) ?? graph.nodes[0];
  const centred = frame.width / 2 - (start.x + start.width / 2) * zoom;
  // Never past either side of the drawing.
  const x = Math.min(padX, Math.max(frame.width - padX - wide, centred));
  return { x, y: padY, zoom };
}
