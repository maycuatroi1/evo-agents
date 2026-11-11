"use client";

import "@xyflow/react/dist/base.css";

import { Background, BackgroundVariant, Panel, ReactFlow, useReactFlow } from "@xyflow/react";
import { useFormatter, useTranslations } from "next-intl";
import { useEffect, useMemo, useState } from "react";

import type { Agent } from "@/lib/agents";
import type { EdgeKind, Spec, SpecNode } from "@/lib/systems";
import { bendsOf, focusOf, type Line, type Pointer } from "@/lib/systems-canvas";
import { drawnAt, NODE_SIZE, ROUND_KINDS } from "@/lib/systems-layout";
import { type EdgeActivity, edgeKey, type ExtraEdge, type Numbers, type NodeState } from "@/lib/systems-observe";
import type { WorkPath } from "@/lib/systems-work";

import { SystemEdge, type SystemFlowEdge } from "./designer-edge";
import { handlesOf, type SystemFlowNode, SystemNode } from "./designer-nodes";
import { SystemLegend } from "./system-legend";

const NODE_TYPES = { system: SystemNode };
const EDGE_TYPES = { system: SystemEdge };
const NO_UPGRADE = () => undefined;
/**
 * Room around the drawing when it fits the view: the words under a node and its numbers hang below and beside it. The
 * fit counts every node by its declared size: the nodes are drawn anew each second, which drops React Flow's measured
 * size of all but the newest and would fit the view to those alone. No node of Observe is hidden.
 */
const FIT = { padding: { top: "32px", bottom: "72px", left: "96px", right: "96px" }, maxZoom: 1, includeHiddenNodes: true } as const;

export type Observed = {
  states: ReadonlyMap<string, NodeState>;
  numbers: ReadonlyMap<string, Numbers>;
  queries: ReadonlyMap<string, number>;
  traces: number;
  edges: ReadonlyMap<number, EdgeActivity>;
  extra: readonly ExtraEdge[];
  ghosts: readonly { id: string; position: { x: number; y: number } }[];
  /** The path of the work selected: what is off it fades, its lines stand out. Null draws every work. */
  path: WorkPath | null;
};

type Props = {
  spec: Spec;
  observed: Observed;
  /** The project's agents, for the avatars of the profiles the nodes pin. */
  agents: readonly Agent[];
  selected: string | null;
  onSelect: (node: string | null) => void;
  /** What the canvas shows, for a screen reader. */
  label?: string;
};

/**
 * The canvas of Observe mode: the saved revision's shapes and lines as the designer draws them, not to be moved or
 * joined, each node with its state and numbers and each line with what crossed it; the edges the design lacks, and the
 * agents outside it they reach, are drawn too. The pointer on a node or a line fades what it does not touch.
 */
export function ObserveCanvas({ spec, observed, agents, selected, onSelect, label }: Props) {
  const t = useTranslations("systems.designer");
  const o = useTranslations("systems.observe");
  const format = useFormatter();
  const flow = useReactFlow<SystemFlowNode, SystemFlowEdge>();
  const [pointer, setPointer] = useState<Pointer>(null);
  const [showOptimizes, setShowOptimizes] = useState(false);
  const faces = useMemo(() => new Map(agents.filter((agent) => agent.deleted_at === null).map((agent) => [agent.handle, agent])), [agents]);

  const numbersOf = useMemo(
    () => (node: SpecNode) => {
      if (node.kind === "entry") return o("traces", { count: observed.traces });
      if (node.kind === "kg") return o("queries", { count: observed.queries.get(node.id) ?? 0 });
      const found = observed.numbers.get(node.id);
      if (!found?.count) return o("noRuns");
      return o("numbers", {
        runs: found.count,
        seconds: found.p50Seconds === null ? "-" : format.number(found.p50Seconds, { maximumFractionDigits: 1 }),
        usd: format.number(found.usd, { style: "currency", currency: "USD", maximumFractionDigits: 2 }),
      });
    },
    [observed, o, format],
  );

  const lines = useMemo<Line[]>(
    () => [
      ...spec.edges.map((edge, index) => ({ id: `e${index}`, source: edge.from, target: edge.to })),
      ...observed.extra.map((edge) => ({ id: `x-${edge.from}-${edge.to}`, source: edge.from, target: edge.to })),
    ],
    [spec.edges, observed.extra],
  );
  const bends = useMemo(() => bendsOf(lines), [lines]);
  const hovered = useMemo(() => focusOf(lines, pointer), [lines, pointer]);
  const focus = useMemo(() => hovered ?? focusOf(lines, selected ? { node: selected } : null), [hovered, lines, selected]);
  const optimized = useMemo(() => new Set(spec.edges.filter((edge) => edge.kind === "optimizes").map((edge) => edge.to)), [spec.edges]);
  const kinds = useMemo(() => new Set<EdgeKind>(spec.edges.map((edge) => edge.kind)), [spec.edges]);

  const titles = useMemo(
    () =>
      new Map(
        spec.nodes.map((node) => {
          const face = node.profile ? faces.get(node.profile) : undefined;
          // An agent without a label goes by its profile's name: its @handle is already the pin under it.
          return [node.id, node.label || (node.kind === "kg" ? (node.graph ?? "default") : (face?.name ?? (node.profile ? `@${node.profile}` : t(`kinds.${node.kind}`))))];
        }),
      ),
    [spec.nodes, faces, t],
  );
  const titleOf = useMemo(() => (id: string) => titles.get(id) ?? faces.get(id.replace(/^@/, ""))?.name ?? id, [titles, faces]);
  const path = observed.path;
  /** Off the path of the work selected, or away from the pointer. */
  const nodeDimmed = (id: string) => (hovered ? !hovered.nodes.has(id) : false) || (path !== null && !path.nodes.has(id));
  const lineDimmed = (id: string, from: string, to: string) => (hovered ? !hovered.lines.has(id) : false) || (path !== null && !path.edges.has(edgeKey(from, to)));

  const nodes = useMemo<SystemFlowNode[]>(() => {
    const placed = spec.nodes.map((node): SystemFlowNode => {
      const face = node.profile ? (faces.get(node.profile) ?? null) : null;
      const title = titleOf(node.id);
      const state = observed.states.get(node.id) ?? "idle";
      const numbers = numbersOf(node);
      return {
        id: node.id,
        type: "system",
        position: drawnAt(node.kind, node.position),
        width: NODE_SIZE[node.kind].width,
        height: NODE_SIZE[node.kind].height,
        handles: handlesOf(node.kind),
        className: ROUND_KINDS.has(node.kind) ? "system-node-round" : undefined,
        draggable: false,
        connectable: false,
        selected: selected === node.id,
        ariaLabel: o("nodeLabel", { kind: t(`kinds.${node.kind}`), name: title, state: o(`states.${state}`), numbers }),
        data: {
          node,
          title,
          level: null,
          problems: [],
          newer: null,
          missing: false,
          selected: selected === node.id,
          drop: null,
          canChange: false,
          onUpgrade: NO_UPGRADE,
          face,
          optimized: optimized.has(node.id),
          dimmed: nodeDimmed(node.id),
          observed: { state, numbers, ghost: false },
        },
      };
    });
    const ghosts = observed.ghosts.map(({ id, position }): SystemFlowNode => {
      const node: SpecNode = { id, kind: "agent", position, profile: id.replace(/^@/, "") };
      return {
        id,
        type: "system",
        position: drawnAt("agent", position),
        width: NODE_SIZE.agent.width,
        height: NODE_SIZE.agent.height,
        handles: handlesOf("agent"),
        className: "system-node-round",
        draggable: false,
        connectable: false,
        ariaLabel: o("ghostLabel", { name: titleOf(id) }),
        data: {
          node,
          title: titleOf(id),
          level: null,
          problems: [],
          newer: null,
          missing: false,
          selected: false,
          drop: null,
          canChange: false,
          onUpgrade: NO_UPGRADE,
          face: faces.get(node.profile ?? "") ?? null,
          dimmed: nodeDimmed(id),
          observed: { state: observed.states.get(id) ?? "idle", numbers: "", ghost: true },
        },
      };
    });
    return [...placed, ...ghosts];
    // nodeDimmed reads hovered and observed.path only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spec.nodes, observed, selected, numbersOf, faces, optimized, hovered, titleOf, o, t]);

  const edges = useMemo<SystemFlowEdge[]>(() => {
    const designed = spec.edges.map((edge, index): SystemFlowEdge => {
      const id = `e${index}`;
      const seen = observed.edges.get(index) ?? { count: 0, lit: false, offDesign: 0 };
      return {
        id,
        type: "system",
        source: edge.from,
        target: edge.to,
        focusable: false,
        // The Curator's lines stay undrawn unless asked, or while one of their ends has the focus.
        hidden: edge.kind === "optimizes" && !showOptimizes && !focus?.lines.has(id),
        data: {
          kind: edge.kind,
          description: edge.description,
          level: null,
          selected: false,
          label: o("edgeLabel", { kind: t(`edgeKinds.${edge.kind}`), from: titleOf(edge.from), to: titleOf(edge.to), count: seen.count }),
          onSelect: () => onSelect(edge.to),
          bend: bends.get(id) ?? 0,
          emphasis: (focus?.lines.has(id) ?? false) || (path?.edges.has(edgeKey(edge.from, edge.to)) ?? false),
          dimmed: lineDimmed(id, edge.from, edge.to),
          observed: { count: seen.count, lit: seen.lit, offDesign: false },
        },
      };
    });
    const extra = observed.extra.map((edge): SystemFlowEdge => {
      const id = `x-${edge.from}-${edge.to}`;
      return {
        id,
        type: "system",
        source: edge.from,
        target: edge.to,
        focusable: false,
        data: {
          kind: "interacts",
          description: "",
          level: null,
          selected: false,
          label: o("offDesignLabel", { from: titleOf(edge.from), to: titleOf(edge.to), count: edge.count }),
          onSelect: () => onSelect(edge.to),
          bend: bends.get(id) ?? 0,
          emphasis: (focus?.lines.has(id) ?? false) || (path?.edges.has(edgeKey(edge.from, edge.to)) ?? false),
          dimmed: lineDimmed(id, edge.from, edge.to),
          observed: { count: edge.count, lit: edge.lit, offDesign: true },
        },
      };
    });
    return [...designed, ...extra];
    // lineDimmed reads hovered and observed.path only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spec.edges, observed, onSelect, bends, focus, hovered, showOptimizes, titleOf, o, t]);

  // Fit the drawing again when what it shows changes: another work's path, or a node outside the design that appeared.
  const drawn = `${path ? [...path.nodes].sort().join(",") : "*"}|${nodes.length}`;
  useEffect(() => {
    const frame = requestAnimationFrame(() => void flow.fitView(FIT));
    return () => cancelAnimationFrame(frame);
  }, [drawn, flow]);

  return (
    <ReactFlow<SystemFlowNode, SystemFlowEdge>
      nodes={nodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      edgeTypes={EDGE_TYPES}
      minZoom={0.25}
      maxZoom={1.75}
      fitView
      fitViewOptions={FIT}
      nodesDraggable={false}
      nodesConnectable={false}
      deleteKeyCode={null}
      selectionKeyCode={null}
      multiSelectionKeyCode={null}
      zoomOnDoubleClick={false}
      onNodeClick={(_, node) => onSelect(node.id)}
      onPaneClick={() => onSelect(null)}
      onNodeMouseEnter={(_, node) => setPointer({ node: node.id })}
      onNodeMouseLeave={() => setPointer(null)}
      onEdgeMouseEnter={(_, edge) => setPointer({ line: edge.id })}
      onEdgeMouseLeave={() => setPointer(null)}
      onInit={() => void flow.fitView(FIT)}
      ariaLabelConfig={{
        "node.a11yDescription.default": o("a11yNode"),
        "node.a11yDescription.keyboardDisabled": o("a11yNode"),
        "edge.a11yDescription.default": o("a11yNode"),
      }}
      aria-label={label ?? o("canvas")}
    >
      <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="var(--border-strong)" />
      <Panel position="bottom-left">
        <SystemLegend kinds={kinds} showOptimizes={showOptimizes} onShowOptimizes={setShowOptimizes} />
      </Panel>
    </ReactFlow>
  );
}
