import type { NodeHoverDrawingFunction, NodeLabelDrawingFunction } from "sigma/rendering";
import type { NodeDisplayData } from "sigma/types";

import type { CanvasEdge, CanvasNode } from "@/lib/kg/canvas-graph";
import type { Theme } from "@/lib/kg/canvas-theme";

import { LABEL_GAP, labelOnLeft, labelWidth } from "./canvas-labels";

/**
 * How the graph canvases (./sigma-canvas.tsx) draw a node's label and the node pointed at or selected, on sigma's 2D
 * layers above the WebGL discs, in the theme's colours and font. A label goes right of its disc, or left of it near
 * the canvas's right edge (canvas-labels.ts chooses which show with the same rule).
 */

/** The attributes the reducers hand the label and hover drawers, beside sigma's display data. */
export type Drawn = NodeDisplayData & Partial<Pick<CanvasNode, "badge" | "ringColor" | "ring">> & { detail?: string };

function drawPill(context: CanvasRenderingContext2D, theme: Theme, text: string, x: number, y: number, size: number, font: string) {
  const height = size + 4;
  context.font = `600 ${size}px ${font}`;
  const width = context.measureText(text).width + 8;
  context.beginPath();
  context.roundRect(x, y - height / 2, width, height, height / 2);
  context.fillStyle = theme.sunken;
  context.fill();
  context.strokeStyle = theme.border;
  context.lineWidth = 1;
  context.stroke();
  context.fillStyle = theme.muted;
  context.fillText(text, x + 4, y + size / 3);
  return width;
}

/** A node's name beside it, and its badge (an entity's code links) as a small pill after the name. */
export function labelDrawer(theme: Theme): NodeLabelDrawingFunction<CanvasNode, CanvasEdge> {
  return (context, data, settings) => {
    if (!data.label) return;
    const size = settings.labelSize;
    const { badge } = data as Drawn;
    const span = labelWidth(data.label, badge, settings.labelFont);
    const x = labelOnLeft(data.x, data.size, span, context.canvas.clientWidth || Infinity)
      ? data.x - data.size - LABEL_GAP - span
      : data.x + data.size + LABEL_GAP;
    context.font = `${settings.labelWeight} ${size}px ${settings.labelFont}`;
    context.fillStyle = theme.foreground;
    context.fillText(data.label, x, data.y + size / 3);
    if (!badge) return;
    const width = context.measureText(data.label).width;
    drawPill(context, theme, badge, x + width + 4, data.y, Math.max(size - 2, 9), settings.labelFont);
  };
}

/** The node pointed at or selected: a ring in its colour, its name and badge in a box, and what it is under them. */
export function hoverDrawer(theme: Theme): NodeHoverDrawingFunction<CanvasNode, CanvasEdge> {
  return (context, data, settings) => {
    const size = settings.labelSize;
    const small = Math.max(size - 2, 9);
    const { badge, detail } = data as Drawn;
    const label = data.label ?? "";
    context.font = `${settings.labelWeight} ${size}px ${settings.labelFont}`;
    const nameWidth = label ? context.measureText(label).width : 0;
    context.font = `600 ${small}px ${settings.labelFont}`;
    const badgeWidth = label && badge ? context.measureText(badge).width + 12 : 0;
    context.font = `${small}px ${settings.labelFont}`;
    const detailWidth = detail ? context.measureText(detail).width : 0;
    const width = Math.max(nameWidth + badgeWidth, detailWidth) + 10;
    const height = size + 8 + (detail ? small + 4 : 0);
    const onLeft = labelOnLeft(data.x, data.size, width, context.canvas.clientWidth || Infinity);
    const left = onLeft ? data.x - data.size - 2 - width : data.x + data.size + 2;
    const top = data.y - (size + 8) / 2;
    context.fillStyle = theme.surface;
    context.strokeStyle = theme.border;
    context.lineWidth = 1;
    if (label || detail) {
      context.beginPath();
      context.roundRect(left, top, width, height, 4);
      context.fill();
      context.stroke();
    }
    context.beginPath();
    context.arc(data.x, data.y, data.size + 3, 0, Math.PI * 2);
    context.fillStyle = theme.surface;
    context.fill();
    context.strokeStyle = data.color;
    context.lineWidth = 2;
    context.stroke();
    if (label) {
      context.font = `${settings.labelWeight} ${size}px ${settings.labelFont}`;
      context.fillStyle = theme.foreground;
      context.fillText(label, left + 5, data.y + size / 3);
      if (badge) drawPill(context, theme, badge, left + 5 + nameWidth + 4, data.y, small, settings.labelFont);
    }
    if (detail) {
      context.font = `${small}px ${settings.labelFont}`;
      context.fillStyle = theme.muted;
      context.fillText(detail, left + 5, data.y + size / 3 + small + 4);
    }
  };
}
