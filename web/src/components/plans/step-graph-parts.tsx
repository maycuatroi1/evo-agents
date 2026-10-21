"use client";

import { type Edge, type EdgeProps, Handle, type Node, type NodeProps, Position } from "@xyflow/react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { TONE_TEXT } from "@/components/status/status-badge";
import type { GraphNode, Point } from "@/lib/plan-graph/layout";
import { cn } from "@/lib/utils";

import { stepLook } from "./status";

/**
 * The boxes and lines of a plan's Graph view, drawn by React Flow from the layout of lib/plan-graph: no React Flow
 * edge router, since its curves and smooth steps cut through the boxes of steps a long line passes; the line follows
 * the points the layout routed through the gaps between rows.
 */

export type StepNodeData = {
  node: GraphNode;
  title: string;
  selected: boolean;
  /** Outside the selected step's ancestors and descendants, or not kept by the search and repo filter. */
  dimmed: boolean;
};
export type StepFlowNode = Node<StepNodeData, "step">;

export type StepEdgeData = {
  points: Point[];
  /** The step waited for is done: a solid line; otherwise dashed. */
  done: boolean;
  /** Joins the selected step: drawn in brand. */
  lit: boolean;
  dimmed: boolean;
  from: string;
  to: string;
};
export type StepFlowEdge = Edge<StepEdgeData, "step">;

// React Flow needs a handle at each end of a line to draw it; these are invisible and take no pointer.
const HANDLE = "!pointer-events-none !min-h-0 !min-w-0 !border-0 !bg-transparent opacity-0";

/**
 * A step's box: its status icon in the tone Board gives it, the key in mono, the title over at most two lines, and
 * "Ready" once every step it waits for is done. A pending step still waiting has a dashed border; the selected one a
 * brand border on surface-selected.
 */
export function StepNode({ data }: NodeProps<StepFlowNode>) {
  const t = useTranslations("plans.graph");
  const { node, title, selected, dimmed } = data;
  const { group } = node.step;
  const look = stepLook(group);
  const waiting = group === "pending" && !node.ready;
  return (
    <div
      className={cn(
        "flex h-full w-full items-center gap-2 rounded-md border bg-card px-2.5 transition-[opacity,border-color,background-color] duration-fast ease-standard",
        waiting ? "border-dashed border-neutral-solid" : "border-border-strong",
        selected && "border-brand bg-surface-selected ring-1 ring-brand",
        dimmed && !selected && "opacity-40",
      )}
      data-testid="graph-step"
      data-step={node.key}
      data-status={group}
      data-ready={node.ready ? "true" : "false"}
      data-waiting={waiting ? "true" : "false"}
      data-selected={selected ? "true" : "false"}
      data-dimmed={dimmed ? "true" : "false"}
    >
      <Handle type="target" position={Position.Top} isConnectable={false} className={HANDLE} />
      <look.icon className={cn("size-4 shrink-0", TONE_TEXT[look.tone])} aria-hidden="true" />
      <span className="shrink-0 font-mono text-xs font-medium text-muted-foreground tabular-nums">{node.key}</span>
      <span className="line-clamp-2 min-w-0 flex-1 text-xs leading-4 font-medium text-foreground [overflow-wrap:anywhere]">
        {title}
      </span>
      {node.ready ? (
        <Tag className="bg-success-soft text-success" data-testid="graph-ready">
          {t("ready")}
        </Tag>
      ) : null}
      <Handle type="source" position={Position.Bottom} isConnectable={false} className={HANDLE} />
    </div>
  );
}

/** A dependency along its routed points, with an arrowhead where it enters the step that waits. */
export function StepEdge({ data }: EdgeProps<StepFlowEdge>) {
  if (!data || data.points.length < 2) return null;
  const { points, done, lit, dimmed } = data;
  const path = points.map((p, i) => `${i ? "L" : "M"}${p.x} ${p.y}`).join(" ");
  const tip = points[points.length - 1];
  const colour = lit ? "var(--brand)" : "var(--neutral-solid)";
  return (
    <g
      data-testid="graph-edge"
      data-from={data.from}
      data-to={data.to}
      data-done={done ? "true" : "false"}
      data-lit={lit ? "true" : "false"}
      opacity={dimmed && !lit ? 0.35 : 1}
    >
      <path
        d={path}
        fill="none"
        stroke={colour}
        strokeWidth={lit ? 2 : 1.5}
        strokeDasharray={done ? undefined : "5 4"}
        strokeLinejoin="round"
      />
      <path d={`M${tip.x - 4} ${tip.y - 6} L${tip.x} ${tip.y} L${tip.x + 4} ${tip.y - 6} Z`} fill={colour} />
    </g>
  );
}

/** A short sample of a line for the legend: solid for a dependency done, dashed for one not done yet. */
export function LineSample({ dashed }: { dashed: boolean }) {
  return (
    <svg width="20" height="8" viewBox="0 0 20 8" aria-hidden="true" className="shrink-0">
      <path d="M0 4 H20" stroke="var(--neutral-solid)" strokeWidth={1.5} strokeDasharray={dashed ? "5 4" : undefined} />
    </svg>
  );
}
