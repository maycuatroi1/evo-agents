"use client";

import "@xyflow/react/dist/base.css";

import { Background, BackgroundVariant, MarkerType, ReactFlow, useReactFlow } from "@xyflow/react";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo } from "react";

import type { Spec, SpecNode } from "@/lib/systems";
import type { EdgeActivity, ExtraEdge, Numbers, NodeState } from "@/lib/systems-observe";
import { NODE_HEIGHT, NODE_WIDTH } from "@/lib/systems-layout";

import { NODE_HANDLES, SystemEdge, type SystemFlowEdge, type SystemFlowNode, SystemNode } from "./designer-nodes";

const NODE_TYPES = { system: SystemNode };
const EDGE_TYPES = { system: SystemEdge };
const NO_UPGRADE = () => undefined;

export type Observed = {
  states: ReadonlyMap<string, NodeState>;
  numbers: ReadonlyMap<string, Numbers>;
  queries: ReadonlyMap<string, number>;
  traces: number;
  edges: ReadonlyMap<number, EdgeActivity>;
  extra: readonly ExtraEdge[];
  ghosts: readonly { id: string; position: { x: number; y: number } }[];
};

type Props = { spec: Spec; observed: Observed; selected: string | null; onSelect: (node: string | null) => void };

/**
 * The canvas of Observe mode: the saved revision's boxes and lines as the designer draws them, not to be moved or
 * joined, each box with its state and numbers and each line with what crossed it; the edges the design lacks, and the
 * agents outside it they reach, are drawn too.
 */
export function ObserveCanvas({ spec, observed, selected, onSelect }: Props) {
  const t = useTranslations("systems.designer");
  const o = useTranslations("systems.observe");
  const format = useFormatter();
  const flow = useReactFlow<SystemFlowNode, SystemFlowEdge>();

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

  const nodes = useMemo<SystemFlowNode[]>(() => {
    const placed = spec.nodes.map((node): SystemFlowNode => {
      const title = node.label || (node.kind === "kg" ? (node.graph ?? "default") : node.profile ? `@${node.profile}` : t(`kinds.${node.kind}`));
      const state = observed.states.get(node.id) ?? "idle";
      const numbers = numbersOf(node);
      return {
        id: node.id,
        type: "system",
        position: node.position,
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        handles: NODE_HANDLES,
        draggable: false,
        connectable: false,
        selected: selected === node.id,
        ariaLabel: o("nodeLabel", { kind: t(`kinds.${node.kind}`), name: title, state: o(`states.${state}`), numbers }),
        data: { node, title, level: null, problems: [], newer: null, missing: false, selected: selected === node.id, drop: null, canChange: false, onUpgrade: NO_UPGRADE, observed: { state, numbers, ghost: false } },
      };
    });
    const ghosts = observed.ghosts.map(({ id, position }): SystemFlowNode => {
      const node: SpecNode = { id, kind: "agent", position, profile: id.replace(/^@/, "") };
      return {
        id,
        type: "system",
        position,
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        handles: NODE_HANDLES,
        draggable: false,
        connectable: false,
        ariaLabel: o("ghostLabel", { name: id }),
        data: { node, title: id, level: null, problems: [], newer: null, missing: false, selected: false, drop: null, canChange: false, onUpgrade: NO_UPGRADE, observed: { state: observed.states.get(id) ?? "idle", numbers: "", ghost: true } },
      };
    });
    return [...placed, ...ghosts];
  }, [spec.nodes, observed, selected, numbersOf, o, t]);

  const edges = useMemo<SystemFlowEdge[]>(() => {
    const marker = { type: MarkerType.ArrowClosed, width: 16, height: 16 };
    const designed = spec.edges.map((edge, index): SystemFlowEdge => {
      const seen = observed.edges.get(index) ?? { count: 0, lit: false, offDesign: 0 };
      return {
        id: `e${index}`,
        type: "system",
        source: edge.from,
        target: edge.to,
        focusable: false,
        markerEnd: marker,
        data: {
          kind: edge.kind,
          description: edge.description,
          level: null,
          selected: false,
          label: o("edgeLabel", { kind: t(`edgeKinds.${edge.kind}`), from: edge.from, to: edge.to, count: seen.count }),
          onSelect: () => onSelect(edge.to),
          observed: { count: seen.count, lit: seen.lit, offDesign: false },
        },
      };
    });
    const extra = observed.extra.map(
      (edge): SystemFlowEdge => ({
        id: `x-${edge.from}-${edge.to}`,
        type: "system",
        source: edge.from,
        target: edge.to,
        focusable: false,
        markerEnd: marker,
        data: {
          kind: "interacts",
          description: "",
          level: null,
          selected: false,
          label: o("offDesignLabel", { from: edge.from, to: edge.to, count: edge.count }),
          onSelect: () => onSelect(edge.to),
          observed: { count: edge.count, lit: edge.lit, offDesign: true },
        },
      }),
    );
    return [...designed, ...extra];
  }, [spec.edges, observed, onSelect, o, t]);

  return (
    <ReactFlow<SystemFlowNode, SystemFlowEdge>
      nodes={nodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      edgeTypes={EDGE_TYPES}
      minZoom={0.25}
      maxZoom={1.75}
      fitView
      fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
      nodesDraggable={false}
      nodesConnectable={false}
      deleteKeyCode={null}
      selectionKeyCode={null}
      multiSelectionKeyCode={null}
      zoomOnDoubleClick={false}
      onNodeClick={(_, node) => onSelect(node.id)}
      onPaneClick={() => onSelect(null)}
      onInit={() => void flow.fitView({ padding: 0.2, maxZoom: 1 })}
      ariaLabelConfig={{
        "node.a11yDescription.default": o("a11yNode"),
        "node.a11yDescription.keyboardDisabled": o("a11yNode"),
        "edge.a11yDescription.default": o("a11yNode"),
      }}
      aria-label={o("canvas")}
    >
      <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="var(--border-strong)" />
    </ReactFlow>
  );
}
