"use client";

import { Handle, type Node, type NodeProps, Position } from "@xyflow/react";
import { ArrowUpCircle, Bot, CircleAlert, createLucideIcon, type LucideIcon, MessageCircle, MoonStar, Network, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { AgentAvatar } from "@/components/data/agent-avatar";
import type { Agent } from "@/lib/agents";
import type { EdgeKind, NodeKind, SpecNode, SystemProblem } from "@/lib/systems";
import type { NodeState } from "@/lib/systems-observe";
import type { Ghost } from "@/lib/system-diff";
import { NODE_SIZE, ROUND_KINDS } from "@/lib/systems-layout";
import { cn } from "@/lib/utils";

/**
 * The shapes and lines of the designer's canvas (web/DESIGN.md, Agent systems). Each kind has a shape of its own: an
 * agent is a circle with the avatar of its profile and its name and pin under it, the orchestrator a larger circle
 * marked with `Network`, a knowledge graph a tile with the graph glyph, the entry a pill, the Curator a dashed square.
 * The lines between them are designer-edge.tsx.
 *
 * A problem the hub found turns the shape's edge red for an error and amber for a warning, with its mark on the shape;
 * a pin older than the profile's latest version offers Upgrade. While a line is dragged, the nodes it may end at show
 * the kind of edge it would make; the others fade. Drawing a proposal of @builder, what it adds is faded with a dashed
 * edge and line, what it drops fainter still, and what it changes is dashed.
 *
 * In Observe mode a node says its state (idle, working, waiting, failed) and its numbers; a working agent's circle
 * turns.
 */

/** A knowledge graph: a hub joined to three nodes, two of which know each other. On Lucide's 24 px grid. */
export const KnowledgeGraph = createLucideIcon("knowledge-graph", [
  ["circle", { cx: "12", cy: "12", r: "2.5", key: "hub" }],
  ["circle", { cx: "4.5", cy: "6", r: "2", key: "left" }],
  ["circle", { cx: "19.5", cy: "6", r: "2", key: "right" }],
  ["circle", { cx: "12", cy: "20.5", r: "2", key: "low" }],
  ["path", { d: "M10.05 10.44 6.06 7.25", key: "hub-left" }],
  ["path", { d: "M13.95 10.44 17.94 7.25", key: "hub-right" }],
  ["path", { d: "M12 14.5v4", key: "hub-low" }],
  ["path", { d: "M6.5 6h11", key: "left-right" }],
]);

export const KIND_ICONS: Record<NodeKind, LucideIcon> = { entry: MessageCircle, agent: Bot, kg: KnowledgeGraph, curator: MoonStar, orchestrator: Network };

/** The face of the profile an agent or the orchestrator pins. */
export type NodeFace = Pick<Agent, "name" | "handle" | "color" | "icon_url">;

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
  /** The profile behind an agent or the orchestrator, for its avatar; null when it is not known. */
  face?: NodeFace | null;
  /** A Curator optimizes it. */
  optimized?: boolean;
  /** Another node or edge has the pointer: this one fades. */
  dimmed?: boolean;
  /** Observe mode: the state of the node now, its numbers in words, and whether the design lacks it. */
  observed?: { state: NodeState; numbers: string; ghost: boolean };
  /** What a proposal of @builder does to the node, when the canvas draws one. */
  ghost?: Ghost;
};
export type SystemFlowNode = Node<SystemNodeData, "system">;

const HANDLE =
  "!size-2.5 !rounded-full !border-2 !border-card !bg-neutral-solid hover:!bg-brand opacity-0 transition-opacity duration-fast group-hover/node:opacity-100 group-data-[selected=true]/node:opacity-100";
const STILL_HANDLE = "!size-px !min-h-0 !min-w-0 !border-0 !bg-transparent opacity-0";

/** The handles a node of `kind` declares, so React Flow draws lines before it has measured the node. */
export function handlesOf(kind: NodeKind) {
  const { width, height } = NODE_SIZE[kind];
  return [
    { type: "target" as const, position: Position.Left, x: 0, y: height / 2, width: 1, height: 1 },
    { type: "source" as const, position: Position.Right, x: width, y: height / 2, width: 1, height: 1 },
  ];
}

const STATE_TONE: Record<NodeState, string> = {
  idle: "bg-neutral-solid",
  working: "bg-running motion-safe:animate-pulse",
  waiting: "bg-attention",
  failed: "bg-danger",
};

/** The state of a node in Observe mode: a dot in its tone and the word. */
function NodeStateBadge({ state, className }: { state: NodeState; className?: string }) {
  const t = useTranslations("systems.observe");
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1 text-[11px] font-medium",
        state === "failed" ? "text-danger" : state === "waiting" ? "text-attention" : state === "working" ? "text-running" : "text-muted-foreground",
        className,
      )}
      data-testid="system-node-state"
      data-state={state}
    >
      <span className={cn("size-2 rounded-full", STATE_TONE[state])} aria-hidden="true" />
      {t(`states.${state}`)}
    </span>
  );
}

/** The mark of a problem the hub found on the node. */
function ProblemMark({ level }: { level: "error" | "warning" }) {
  const t = useTranslations("systems.designer");
  return level === "error" ? (
    <CircleAlert className="size-4 shrink-0 text-danger" aria-label={t("hasErrors")} />
  ) : (
    <TriangleAlert className="size-4 shrink-0 text-attention" aria-label={t("hasWarnings")} />
  );
}

const BADGE = "absolute grid size-5 place-items-center rounded-full border border-border-strong bg-card";

/** The Curator's mark on a node it optimizes, so its lines need not be drawn to say so. */
function OptimizedMark({ className }: { className: string }) {
  const t = useTranslations("systems.designer");
  return (
    <span className={cn(BADGE, "text-muted-foreground", className)} role="img" aria-label={t("optimizedBadge")} title={t("optimizedBadge")} data-testid="system-node-optimized">
      <MoonStar className="size-3" aria-hidden="true" />
    </span>
  );
}

/** The edge of a shape: its tone by problem or state, the selection ring, and how a drag or a proposal marks it. */
function shapeLook(data: SystemNodeData): string {
  const { observed, level, selected, drop, ghost = null } = data;
  const tone = observed
    ? observed.ghost
      ? "border-dashed border-attention"
      : { failed: "border-danger", working: "border-running", waiting: "border-attention", idle: "border-border-strong" }[observed.state]
    : level === "error"
      ? "border-danger"
      : level === "warning"
        ? "border-attention"
        : "border-border-strong";
  return cn(
    "transition-[border-color,background-color,box-shadow] duration-fast ease-standard",
    tone,
    selected && "ring-2 ring-brand ring-offset-2 ring-offset-background",
    drop !== null && drop !== "none" && "border-brand bg-brand-soft",
    ghost !== null && "border-dashed",
    (ghost === "added" || ghost === "changed") && "border-brand",
  );
}

/** The profile's pin under an agent's name, or its graph under a graph's, with Upgrade when the pin is behind. */
function Detail({ data, className }: { data: SystemNodeData; className?: string }) {
  const t = useTranslations("systems.designer");
  const { node, newer, missing, canChange } = data;
  const detail =
    node.kind === "kg"
      ? t("graphOf", { graph: node.graph ?? "default" })
      : node.profile
        ? node.version
          ? t("pinned", { handle: node.profile, version: node.version })
          : `@${node.profile}`
        : t(`kindHint.${node.kind}`);
  return (
    <span className={cn("flex min-w-0 max-w-full items-center gap-1.5", className)}>
      <span className={cn("truncate font-mono text-[11px]", missing ? "text-danger" : "text-muted-foreground")}>{missing ? t("missing") : detail}</span>
      {newer !== null && canChange ? (
        <button
          type="button"
          className="nodrag nopan inline-flex shrink-0 cursor-pointer items-center gap-0.5 rounded-sm px-1 text-[11px] font-medium text-brand hover:bg-brand-soft focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          onClick={(event) => {
            event.stopPropagation();
            data.onUpgrade(node.id, newer);
          }}
          aria-label={t("upgradeTo", { version: newer })}
          data-testid="system-node-upgrade"
        >
          <ArrowUpCircle className="size-3" aria-hidden="true" />
          {t("upgradeShort", { version: newer })}
        </button>
      ) : newer !== null ? (
        <span className="shrink-0 text-[11px] text-attention">{t("newerShort", { version: newer })}</span>
      ) : null}
    </span>
  );
}

/** A node's name, then its state (when `withState`) and numbers in Observe mode, or its detail otherwise. */
function Words({ data, centred, withState }: { data: SystemNodeData; centred: boolean; withState: boolean }) {
  const t = useTranslations("systems.designer");
  const { title, observed, ghost = null } = data;
  return (
    <>
      <span className={cn("max-w-full truncate text-[13px] leading-snug font-medium text-foreground", ghost === "removed" && "line-through")} title={title}>
        {title}
      </span>
      {observed ? (
        <>
          {withState ? <NodeStateBadge state={observed.state} /> : null}
          <span className="max-w-full truncate text-[11px] text-muted-foreground tabular-nums" data-testid="system-node-numbers">
            {observed.ghost ? t("notInDesign") : observed.numbers}
          </span>
        </>
      ) : (
        <Detail data={data} className={centred ? "justify-center" : undefined} />
      )}
    </>
  );
}

/** An agent or the orchestrator: its avatar in a circle, the name and pin under it (over it for the orchestrator). */
function RoundShape({ data }: { data: SystemNodeData }) {
  const { node, face, observed, level } = data;
  const Icon = KIND_ICONS[node.kind];
  const above = node.kind === "orchestrator";
  return (
    <>
      <div className={cn("relative size-full rounded-full border-2 bg-card p-[3px]", shapeLook(data))}>
        {face ? (
          <AgentAvatar agent={face} size="xl" className="size-full" />
        ) : (
          <span className="grid size-full place-items-center rounded-full bg-muted text-muted-foreground">
            <Icon className="size-5" aria-hidden="true" />
          </span>
        )}
        {observed?.state === "working" && !observed.ghost ? (
          <span className="pointer-events-none absolute -inset-1.5 rounded-full border-2 border-dashed border-running motion-safe:animate-[spin_8s_linear_infinite]" aria-hidden="true" />
        ) : null}
      </div>
      {node.kind === "orchestrator" ? (
        <span className={cn(BADGE, "-right-1 -bottom-1 text-chart-4")} aria-hidden="true">
          <Network className="size-3" />
        </span>
      ) : null}
      {data.optimized ? <OptimizedMark className="-top-1 -left-1" /> : null}
      {level && !observed ? (
        <span className={cn(BADGE, "-top-1 -right-1")}>
          <ProblemMark level={level} />
        </span>
      ) : null}
      <div
        className={cn(
          "absolute left-1/2 flex w-max max-w-[200px] -translate-x-1/2 flex-col items-center gap-0.5 rounded-sm bg-background/90 px-1.5 py-0.5 text-center",
          above ? "bottom-full mb-2.5" : "top-full mt-2.5",
        )}
      >
        <Words data={data} centred withState />
      </div>
    </>
  );
}

/** A knowledge graph: a tile with the graph glyph, its name and the graph it names. */
function GraphShape({ data }: { data: SystemNodeData }) {
  const { observed, level } = data;
  return (
    <>
      <div className={cn("flex size-full items-center gap-2.5 rounded-lg border bg-card px-2.5", shapeLook(data))}>
        <span className="grid size-8 shrink-0 place-items-center rounded-md bg-chart-3/12 text-chart-3" aria-hidden="true">
          <KnowledgeGraph className="size-[18px]" />
        </span>
        <span className="flex min-w-0 flex-1 flex-col">
          <Words data={data} centred={false} withState={false} />
        </span>
        {observed ? <NodeStateBadge state={observed.state} className="self-start pt-1.5" /> : level ? <ProblemMark level={level} /> : null}
      </div>
      {data.optimized ? <OptimizedMark className="-top-2 -left-2" /> : null}
    </>
  );
}

/** The entry: a pill where a person's message comes in. */
function EntryShape({ data }: { data: SystemNodeData }) {
  const { observed, level } = data;
  return (
    <div className={cn("flex size-full items-center gap-2 rounded-full border bg-card pr-3 pl-3.5", shapeLook(data))}>
      <MessageCircle className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <span className="flex min-w-0 flex-1 flex-col">
        <Words data={data} centred={false} withState={false} />
      </span>
      {observed ? <NodeStateBadge state={observed.state} /> : level ? <ProblemMark level={level} /> : null}
    </div>
  );
}

/** The Curator: a dashed square that watches the system, its name and what it does beside it. */
function CuratorShape({ data }: { data: SystemNodeData }) {
  const { observed, level } = data;
  return (
    <>
      <div className={cn("grid size-full place-items-center rounded-xl border border-dashed bg-card", shapeLook(data))}>
        <MoonStar className="size-5 text-muted-foreground" aria-hidden="true" />
      </div>
      {level && !observed ? (
        <span className={cn(BADGE, "-top-1.5 -right-1.5")}>
          <ProblemMark level={level} />
        </span>
      ) : null}
      <div className="absolute top-1/2 left-full ml-2.5 flex w-max max-w-[180px] -translate-y-1/2 flex-col items-start gap-0.5 rounded-sm bg-background/90 px-1.5 py-0.5">
        <Words data={data} centred={false} withState />
      </div>
    </>
  );
}

const SHAPES: Record<NodeKind, (props: { data: SystemNodeData }) => ReactNode> = {
  agent: RoundShape,
  orchestrator: RoundShape,
  kg: GraphShape,
  entry: EntryShape,
  curator: CuratorShape,
};

export function SystemNode({ data }: NodeProps<SystemFlowNode>) {
  const t = useTranslations("systems.designer");
  const { node, level, selected, drop, canChange, dimmed, ghost = null } = data;
  const Shape = SHAPES[node.kind];
  return (
    <div
      className={cn(
        "group/node relative size-full transition-opacity duration-fast ease-standard",
        ghost === "added" && "opacity-60",
        ghost === "removed" && "opacity-35",
        drop === "none" && "opacity-40",
        dimmed && "opacity-30",
      )}
      data-testid="system-node"
      data-node={node.id}
      data-kind={node.kind}
      data-level={level ?? ""}
      data-selected={selected ? "true" : "false"}
      data-dimmed={dimmed ? "true" : "false"}
      data-drop={drop ?? ""}
      data-ghost={ghost ?? ""}
      data-avatar={ROUND_KINDS.has(node.kind) ? (data.face ? "profile" : "none") : undefined}
    >
      <Handle type="target" position={Position.Left} className={canChange ? HANDLE : STILL_HANDLE} isConnectable={canChange} />
      <Shape data={data} />
      {drop !== null && drop !== "none" ? (
        <span
          className="absolute -top-6 left-1/2 z-10 -translate-x-1/2 rounded-sm bg-brand px-1.5 text-[11px] font-medium whitespace-nowrap text-on-brand"
          data-testid="system-node-drop"
        >
          {t(`edgeKinds.${drop}`)}
        </span>
      ) : null}
      <Handle type="source" position={Position.Right} className={canChange ? HANDLE : STILL_HANDLE} isConnectable={canChange} />
    </div>
  );
}
