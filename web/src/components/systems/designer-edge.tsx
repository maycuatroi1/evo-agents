"use client";

import { BaseEdge, type Edge, EdgeLabelRenderer, type EdgeProps, type InternalNode, useInternalNode } from "@xyflow/react";
import { useTranslations } from "next-intl";

import type { EdgeKind } from "@/lib/systems";
import type { Ghost } from "@/lib/system-diff";
import { NODE_SIZE, ROUND_KINDS } from "@/lib/systems-layout";
import { cn } from "@/lib/utils";

import type { SystemFlowNode } from "./designer-nodes";

/**
 * The lines of the designer's canvas (web/DESIGN.md, Agent systems). A line runs from centre to centre and ends on the
 * shape (on the circle of an agent), coloured and dashed by its kind as the legend names them; two lines between the
 * same nodes bow apart. A line says its kind and description only while it or one of its ends has the focus, is
 * selected, has a problem or is part of a proposal; otherwise a dot at its middle selects it.
 *
 * In Observe mode a line says how much work crossed it, lights while work goes through it, and one the design lacks is
 * dashed in the warning colour.
 */

/** What an edge of each kind looks like: its colour and its dash (none for a solid line). */
export const EDGE_LOOK: Record<EdgeKind, { colour: string; dash?: string }> = {
  interacts: { colour: "var(--muted-foreground)" },
  orchestrates: { colour: "var(--chart-4)" },
  escalates: { colour: "var(--chart-4)", dash: "1.5 4" },
  uses_kg: { colour: "var(--chart-3)", dash: "6 4" },
  optimizes: { colour: "var(--neutral-solid)", dash: "2 4" },
};

export type SystemEdgeData = {
  kind: EdgeKind;
  description: string;
  level: "error" | "warning" | null;
  selected: boolean;
  /** The words of the edge for a screen reader: its kind, its ends and its problems. */
  label: string;
  onSelect: () => void;
  /** How far the line bows to its left, in px: lines between the same two nodes bow apart. */
  bend?: number;
  /** It or one of its ends has the focus: the line says its kind and description. */
  emphasis?: boolean;
  /** Another node or edge has the pointer: this one fades. */
  dimmed?: boolean;
  /** Observe mode: how much work crossed it, whether work goes through it now, and whether the design lacks it. */
  observed?: { count: number; lit: boolean; offDesign: boolean };
  /** What a proposal of @builder does to the edge, when the canvas draws one. */
  ghost?: Ghost;
};
export type SystemFlowEdge = Edge<SystemEdgeData, "system">;

type Point = { x: number; y: number };
type Anchor = Point & { width: number; height: number; round: boolean };

function anchorOf(node: InternalNode<SystemFlowNode> | undefined): Anchor | null {
  if (!node) return null;
  const size = NODE_SIZE[node.data.node.kind];
  const width = node.measured.width ?? node.width ?? size.width;
  const height = node.measured.height ?? node.height ?? size.height;
  const { x, y } = node.internals.positionAbsolute;
  return { x: x + width / 2, y: y + height / 2, width, height, round: ROUND_KINDS.has(node.data.node.kind) };
}

/** Where a line from the centre of `anchor` toward `toward` leaves the shape, `gap` px out from its edge. */
function exit(anchor: Anchor, toward: Point, gap: number): Point {
  const dx = toward.x - anchor.x;
  const dy = toward.y - anchor.y;
  const length = Math.hypot(dx, dy);
  if (length < 1) return { x: anchor.x, y: anchor.y };
  if (anchor.round) {
    const radius = anchor.width / 2 + gap;
    return { x: anchor.x + (dx / length) * radius, y: anchor.y + (dy / length) * radius };
  }
  const scale = Math.min((anchor.width / 2 + gap) / Math.max(Math.abs(dx), 1e-6), (anchor.height / 2 + gap) / Math.max(Math.abs(dy), 1e-6));
  return { x: anchor.x + dx * scale, y: anchor.y + dy * scale };
}

const ARROW_LENGTH = 9;
const ARROW_HALF = 4.5;

/**
 * The line from `source` to `target`: straight from shape to shape, or bowed `bend` px to its left; the middle of the
 * line for its label, and its arrowhead at the target along the line's last direction.
 */
export function floatingLine(source: Anchor, target: Anchor, bend: number) {
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  const length = Math.hypot(dx, dy) || 1;
  // A quadratic curve's middle lies halfway to its control point, so the control stands twice the bend out.
  const control = { x: (source.x + target.x) / 2 - (dy / length) * bend * 2, y: (source.y + target.y) / 2 + (dx / length) * bend * 2 };
  const start = exit(source, bend ? control : target, source.round ? 6 : 3);
  const end = exit(target, bend ? control : source, target.round ? 6 : 3);
  const from = bend ? control : start;
  const ax = end.x - from.x;
  const ay = end.y - from.y;
  const al = Math.hypot(ax, ay) || 1;
  const ux = ax / al;
  const uy = ay / al;
  const base = { x: end.x - ux * ARROW_LENGTH, y: end.y - uy * ARROW_LENGTH };
  const round = (value: number) => Math.round(value * 10) / 10;
  return {
    path: bend
      ? `M${round(start.x)},${round(start.y)} Q${round(control.x)},${round(control.y)} ${round(base.x)},${round(base.y)}`
      : `M${round(start.x)},${round(start.y)} L${round(base.x)},${round(base.y)}`,
    middle: bend ? { x: (start.x + 2 * control.x + end.x) / 4, y: (start.y + 2 * control.y + end.y) / 4 } : { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 },
    arrow: `M${round(end.x)},${round(end.y)} L${round(base.x - uy * ARROW_HALF)},${round(base.y + ux * ARROW_HALF)} L${round(base.x + uy * ARROW_HALF)},${round(base.y - ux * ARROW_HALF)} Z`,
  };
}

export function SystemEdge({ id, source, target, data }: EdgeProps<SystemFlowEdge>) {
  const t = useTranslations("systems.designer");
  const from = anchorOf(useInternalNode<SystemFlowNode>(source));
  const to = anchorOf(useInternalNode<SystemFlowNode>(target));
  if (!data || !from || !to) return null;
  const { path, middle, arrow } = floatingLine(from, to, data.bend ?? 0);
  const seen = data.observed;
  const ghost = data.ghost ?? null;
  const look = EDGE_LOOK[data.kind];
  const colour = seen?.offDesign
    ? "var(--attention)"
    : seen?.lit
      ? "var(--running)"
      : ghost === "added" || ghost === "changed"
        ? "var(--brand)"
        : data.level === "error"
          ? "var(--danger)"
          : data.level === "warning"
            ? "var(--attention)"
            : data.selected
              ? "var(--brand)"
              : look.colour;
  const width = seen?.lit ? 2.5 : data.selected || data.emphasis ? 2 : 1.5;
  const dash = seen?.offDesign ? "5 4" : seen?.lit ? "8 6" : ghost !== null ? "4 4" : look.dash;
  const opacity = data.dimmed ? 0.15 : ghost === "added" ? 0.6 : ghost === "removed" ? 0.35 : undefined;
  const words = Boolean(data.emphasis || data.selected || data.level || ghost !== null || seen?.offDesign);
  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        interactionWidth={16}
        className={cn(seen?.lit && "system-edge-lit")}
        style={{ stroke: colour, strokeWidth: width, strokeDasharray: dash, opacity, transition: "opacity 120ms" }}
      />
      <path d={arrow} fill={colour} stroke={colour} strokeWidth={1} strokeLinejoin="round" style={{ opacity, transition: "opacity 120ms" }} pointerEvents="none" />
      <EdgeLabelRenderer>
        <button
          type="button"
          onClick={data.onSelect}
          aria-label={data.label}
          title={words ? undefined : data.description || data.label}
          className={cn(
            "nodrag nopan pointer-events-auto absolute cursor-pointer transition-opacity duration-fast focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
            words || seen
              ? "max-w-48 truncate rounded-sm border bg-card px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground"
              : "grid size-5 place-items-center rounded-full",
            words && data.level === "error" && "border-danger text-danger",
            words && data.level === "warning" && "border-attention text-attention",
            words && data.selected && "border-brand text-foreground",
            seen?.offDesign && "border-dashed border-attention text-attention",
            seen?.lit && !seen.offDesign && "border-running text-running",
            ghost !== null && "border-dashed",
            ghost === "added" && "border-brand opacity-60",
            ghost === "removed" && "line-through opacity-35",
            data.dimmed && "opacity-15",
          )}
          style={{ transform: `translate(-50%, -50%) translate(${middle.x}px, ${middle.y}px)` }}
          data-testid="system-edge"
          data-edge={id}
          data-kind={data.kind}
          data-level={data.level ?? ""}
          data-words={words ? "true" : "false"}
          data-count={seen ? seen.count : undefined}
          data-lit={seen ? String(seen.lit) : undefined}
          data-off-design={seen ? String(seen.offDesign) : undefined}
          data-ghost={ghost ?? ""}
        >
          {words ? (
            <>
              {seen?.offDesign ? t("offDesignEdge") : t(`edgeKinds.${data.kind}`)}
              {seen ? (
                <span className="ml-1 rounded-sm bg-muted px-1 font-semibold text-foreground tabular-nums" data-testid="system-edge-count">
                  {seen.count}
                </span>
              ) : data.description ? (
                <span className="font-normal">: {data.description}</span>
              ) : null}
            </>
          ) : seen ? (
            <span className="font-semibold text-foreground tabular-nums" data-testid="system-edge-count">
              {seen.count}
            </span>
          ) : (
            <span className="size-2 rounded-full ring-2 ring-background" style={{ background: colour }} aria-hidden="true" />
          )}
        </button>
      </EdgeLabelRenderer>
    </>
  );
}
