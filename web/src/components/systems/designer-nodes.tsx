"use client";

import {
  BaseEdge,
  type Edge,
  EdgeLabelRenderer,
  type EdgeProps,
  getBezierPath,
  Handle,
  type Node,
  type NodeProps,
  Position,
} from "@xyflow/react";
import { ArrowUpCircle, Bot, Brain, CircleAlert, Database, LogIn, type LucideIcon, Network, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";

import type { EdgeKind, NodeKind, SpecNode, SystemProblem } from "@/lib/systems";
import type { NodeState } from "@/lib/systems-observe";
import { NODE_HEIGHT, NODE_WIDTH } from "@/lib/systems-layout";
import { cn } from "@/lib/utils";

/**
 * The boxes and lines of the designer's canvas. A box says what the node is (its kind's icon and word), its name and
 * what it names (the profile and its pinned version, or the graph); its border turns red for an error and amber for a
 * warning the hub found, and a pin older than the profile's latest version offers Upgrade. While a line is dragged,
 * the boxes it may end at show the kind of edge it would make; the others fade.
 *
 * In Observe mode a box says its state (idle, working, waiting, failed) where the problems would be, and its numbers
 * under its name; a line says how much work crossed it, lights while work goes through it, and one the design lacks
 * is dashed in the warning colour.
 */

export const KIND_ICONS: Record<NodeKind, LucideIcon> = { entry: LogIn, agent: Bot, kg: Database, curator: Brain, orchestrator: Network };

export type SystemNodeData = {
  node: SpecNode;
  title: string;
  level: "error" | "warning" | null;
  problems: readonly SystemProblem[];
  /** The latest version of the pinned profile, when it is newer than the pin. */
  newer: number | null;
  /** The profile was deleted, or is not in the project. */
  missing: boolean;
  selected: boolean;
  /** While a line is dragged: the kind of edge a drop here makes, "none" for no edge, null when no line is dragged. */
  drop: EdgeKind | "none" | null;
  canChange: boolean;
  onUpgrade: (id: string, version: number) => void;
  /** Observe mode: the state of the node now, its numbers in words, and whether the design lacks it. */
  observed?: { state: NodeState; numbers: string; ghost: boolean };
};
export type SystemFlowNode = Node<SystemNodeData, "system">;

export type SystemEdgeData = {
  kind: EdgeKind;
  description: string;
  level: "error" | "warning" | null;
  selected: boolean;
  /** The words of the edge for a screen reader: its kind, its ends and its problems. */
  label: string;
  onSelect: () => void;
  /** Observe mode: how much work crossed it, whether work goes through it now, and whether the design lacks it. */
  observed?: { count: number; lit: boolean; offDesign: boolean };
};
export type SystemFlowEdge = Edge<SystemEdgeData, "system">;

const HANDLE = "!size-2.5 !rounded-full !border-2 !border-card !bg-neutral-solid hover:!bg-brand";

/** The handles every box declares, so React Flow draws lines before it has measured the box. */
export const NODE_HANDLES = [
  { type: "target" as const, position: Position.Left, x: 0, y: NODE_HEIGHT / 2, width: 1, height: 1 },
  { type: "source" as const, position: Position.Right, x: NODE_WIDTH, y: NODE_HEIGHT / 2, width: 1, height: 1 },
];

const STATE_TONE: Record<NodeState, string> = {
  idle: "bg-neutral-solid",
  working: "bg-running motion-safe:animate-pulse",
  waiting: "bg-attention",
  failed: "bg-danger",
};

/** The state of a node in Observe mode: a dot in its tone and the word. */
function NodeStateBadge({ state }: { state: NodeState }) {
  const t = useTranslations("systems.observe");
  return (
    <span className={cn("ml-auto inline-flex shrink-0 items-center gap-1 text-[11px] font-medium", state === "failed" ? "text-danger" : state === "waiting" ? "text-attention" : state === "working" ? "text-running" : "text-muted-foreground")} data-testid="system-node-state" data-state={state}>
      <span className={cn("size-2 rounded-full", STATE_TONE[state])} aria-hidden="true" />
      {t(`states.${state}`)}
    </span>
  );
}

export function SystemNode({ data }: NodeProps<SystemFlowNode>) {
  const t = useTranslations("systems.designer");
  const { node, title, level, newer, missing, selected, drop, canChange, observed } = data;
  const Icon = KIND_ICONS[node.kind];
  const detail =
    node.kind === "kg"
      ? t("graphOf", { graph: node.graph ?? "default" })
      : node.profile
        ? node.version
          ? t("pinned", { handle: node.profile, version: node.version })
          : `@${node.profile}`
        : t(`kindHint.${node.kind}`);
  return (
    <div
      className={cn(
        "relative flex h-full w-full flex-col justify-center gap-1 rounded-md border bg-card px-3 py-2 shadow-raised transition-[opacity,border-color,box-shadow] duration-fast ease-standard",
        observed
          ? observed.ghost
            ? "border-dashed border-attention"
            : observed.state === "failed"
              ? "border-danger"
              : observed.state === "working"
                ? "border-running"
                : "border-border-strong"
          : level === "error"
            ? "border-danger"
            : level === "warning"
              ? "border-attention"
              : "border-border-strong",
        selected && "ring-2 ring-brand",
        drop === "none" && "opacity-40",
        drop !== null && drop !== "none" && "border-brand bg-brand-soft",
      )}
      data-testid="system-node"
      data-node={node.id}
      data-kind={node.kind}
      data-level={level ?? ""}
      data-selected={selected ? "true" : "false"}
      data-drop={drop ?? ""}
    >
      <Handle type="target" position={Position.Left} className={HANDLE} isConnectable={canChange} />
      <div className="flex min-w-0 items-center gap-2">
        <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <span className="shrink-0 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">{t(`kinds.${node.kind}`)}</span>
        {observed ? (
          <NodeStateBadge state={observed.state} />
        ) : level ? (
          level === "error" ? (
            <CircleAlert className="ml-auto size-4 shrink-0 text-danger" aria-label={t("hasErrors")} />
          ) : (
            <TriangleAlert className="ml-auto size-4 shrink-0 text-attention" aria-label={t("hasWarnings")} />
          )
        ) : null}
      </div>
      <span className="truncate text-sm font-medium text-foreground" title={title}>
        {title}
      </span>
      {observed ? (
        <span className="truncate text-xs text-muted-foreground tabular-nums" data-testid="system-node-numbers">
          {observed.ghost ? t("notInDesign") : observed.numbers}
        </span>
      ) : null}
      <div className={cn("flex min-w-0 items-center gap-2", observed && "hidden")}>
        <span className={cn("truncate font-mono text-xs", missing ? "text-danger" : "text-muted-foreground")}>{missing ? t("missing") : detail}</span>
        {newer !== null && canChange ? (
          <button
            type="button"
            className="nodrag nopan ml-auto inline-flex shrink-0 items-center gap-1 rounded-sm px-1 text-xs font-medium text-brand hover:bg-brand-soft focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            onClick={(event) => {
              event.stopPropagation();
              data.onUpgrade(node.id, newer);
            }}
            aria-label={t("upgradeTo", { version: newer })}
            data-testid="system-node-upgrade"
          >
            <ArrowUpCircle className="size-3.5" aria-hidden="true" />
            {t("upgradeShort", { version: newer })}
          </button>
        ) : newer !== null ? (
          <span className="ml-auto shrink-0 text-xs text-attention">{t("newerShort", { version: newer })}</span>
        ) : null}
      </div>
      {drop !== null && drop !== "none" ? (
        <span className="absolute -top-2.5 left-3 rounded-sm bg-brand px-1.5 text-[11px] font-medium text-on-brand" data-testid="system-node-drop">
          {t(`edgeKinds.${drop}`)}
        </span>
      ) : null}
      <Handle type="source" position={Position.Right} className={HANDLE} isConnectable={canChange} />
    </div>
  );
}

const DASH: Partial<Record<EdgeKind, string>> = { optimizes: "6 4", escalates: "2 4", uses_kg: "8 3" };

export function SystemEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, markerEnd, data }: EdgeProps<SystemFlowEdge>) {
  const t = useTranslations("systems.designer");
  const [path, labelX, labelY] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition });
  if (!data) return null;
  const seen = data.observed;
  const colour = seen?.offDesign
    ? "var(--attention)"
    : seen?.lit
      ? "var(--running)"
      : data.level === "error"
        ? "var(--danger)"
        : data.level === "warning"
          ? "var(--attention)"
          : data.selected
            ? "var(--brand)"
            : "var(--neutral-solid)";
  const width = seen?.lit ? 2.5 : data.selected ? 2 : 1.5;
  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} interactionWidth={16} className={cn(seen?.lit && "system-edge-lit")} style={{ stroke: colour, strokeWidth: width, strokeDasharray: seen?.offDesign ? "5 4" : seen?.lit ? "8 6" : DASH[data.kind] }} />
      <EdgeLabelRenderer>
        <button
          type="button"
          onClick={data.onSelect}
          aria-label={data.label}
          className={cn(
            "nodrag nopan pointer-events-auto absolute max-w-48 truncate rounded-sm border bg-card px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground shadow-raised focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
            data.level === "error" && "border-danger text-danger",
            data.level === "warning" && "border-attention text-attention",
            data.selected && "border-brand text-foreground",
            seen?.offDesign && "border-dashed border-attention text-attention",
            seen?.lit && !seen.offDesign && "border-running text-running",
          )}
          style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
          title={data.description || undefined}
          data-testid="system-edge"
          data-edge={id}
          data-kind={data.kind}
          data-level={data.level ?? ""}
          data-count={seen ? seen.count : undefined}
          data-lit={seen ? String(seen.lit) : undefined}
          data-off-design={seen ? String(seen.offDesign) : undefined}
        >
          {seen?.offDesign ? t("offDesignEdge") : t(`edgeKinds.${data.kind}`)}
          {seen ? (
            <span className="ml-1 rounded-sm bg-muted px-1 font-semibold text-foreground tabular-nums" data-testid="system-edge-count">
              {seen.count}
            </span>
          ) : data.description ? (
            <span className="font-normal">: {data.description}</span>
          ) : null}
        </button>
      </EdgeLabelRenderer>
    </>
  );
}
