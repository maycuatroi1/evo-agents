"use client";

import "@xyflow/react/dist/base.css";

import { Background, BackgroundVariant, ReactFlow, ReactFlowProvider, useReactFlow } from "@xyflow/react";
import { Maximize, Minus, Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { type KeyboardEvent, useCallback, useMemo, useState } from "react";

import { TONE_TEXT } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { layoutPlanGraph, type PlanGraph } from "@/lib/plan-graph/layout";
import { STEP_STATUSES, stepLabel } from "@/lib/plans";
import { cn } from "@/lib/utils";

import { stepLook, useStepStatusText } from "./status";
import { GRAPH_FRAME, type StepGraphProps } from "./step-graph-frame";
import { LineSample, StepEdge, type StepFlowEdge, type StepFlowNode, StepNode } from "./step-graph-parts";

/**
 * The Graph view of a plan's steps: the layout of lib/plan-graph drawn by React Flow, read only. A legend above the
 * canvas counts the steps by status and the Ready ones, says what solid and dashed lines mean and how many
 * dependencies are not drawn. The canvas fits the whole plan when it opens, never past zoom 1; a drag pans it, the
 * wheel scrolls the page, and the buttons zoom and fit. Loaded on demand (step-graph-lazy.tsx).
 */

const NODE_TYPES = { step: StepNode };
const EDGE_TYPES = { step: StepEdge };
const FIT = { maxZoom: 1, padding: 0.06 };

const reducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function Legend({ graph }: { graph: PlanGraph }) {
  const t = useTranslations("plans.graph");
  const status = useStepStatusText();
  const notDrawn = graph.edges.filter((edge) => edge.kind !== "drawn").length;
  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-b px-3 py-2 text-xs text-muted-foreground"
      data-testid="graph-legend"
    >
      {STEP_STATUSES.map((group) => {
        const look = stepLook(group);
        const count = graph.nodes.filter((node) => node.step.group === group).length;
        return (
          <span key={group} className="inline-flex items-center gap-1.5" data-testid={`graph-count-${group}`}>
            <look.icon className={cn("size-3.5", TONE_TEXT[look.tone])} aria-hidden="true" />
            <span className="font-medium text-foreground tabular-nums">{count}</span>
            {status(group)}
          </span>
        );
      })}
      <span className="inline-flex items-center gap-1.5" data-testid="graph-count-ready">
        <span className="font-medium text-foreground tabular-nums">{graph.nodes.filter((node) => node.ready).length}</span>
        {t("ready")}
      </span>
      <span className="inline-flex items-center gap-1.5">
        <LineSample dashed={false} />
        {t("lineDone")}
      </span>
      <span className="inline-flex items-center gap-1.5">
        <LineSample dashed />
        {t("lineNotDone")}
      </span>
      {notDrawn ? (
        <span title={t("notDrawnHint")} data-testid="graph-not-drawn">
          {t("notDrawn", { count: notDrawn })}
        </span>
      ) : null}
    </div>
  );
}

function Zoom() {
  const t = useTranslations("plans.graph");
  const flow = useReactFlow();
  const options = () => ({ duration: reducedMotion() ? 0 : 180 });
  const buttons = [
    { label: t("zoomIn"), icon: Plus, run: () => flow.zoomIn(options()) },
    { label: t("zoomOut"), icon: Minus, run: () => flow.zoomOut(options()) },
    { label: t("fit"), icon: Maximize, run: () => flow.fitView({ ...FIT, ...options() }) },
  ];
  return (
    <div className="absolute top-2 right-2 z-10 flex gap-1 rounded-md border bg-card/95 p-0.5 shadow-raised">
      {buttons.map(({ label, icon: Icon, run }) => (
        <Button
          key={label}
          type="button"
          variant="ghost"
          size="icon-sm"
          className="cursor-pointer max-md:size-11"
          aria-label={label}
          title={label}
          onClick={() => void run()}
        >
          <Icon aria-hidden="true" />
        </Button>
      ))}
    </div>
  );
}

function Graph({ steps, matching }: StepGraphProps) {
  const t = useTranslations("plans.graph");
  const status = useStepStatusText();
  const graph = useMemo(() => layoutPlanGraph(steps), [steps]);
  const [selected, setSelected] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  const keyOf = useMemo(() => new Map(graph.nodes.map((node) => [`s${node.index}`, node.key])), [graph]);
  const related = useMemo(() => {
    const node = graph.nodes.find((n) => n.key === selected);
    return node ? new Set([node.key, ...node.ancestors, ...node.descendants]) : null;
  }, [graph, selected]);
  const outside = useCallback(
    (key: string) => Boolean((related && !related.has(key)) || (matching && !matching.has(key))),
    [related, matching],
  );

  const nodes = useMemo<StepFlowNode[]>(
    () =>
      graph.nodes.map((node) => {
        const title = stepLabel(node.step) || t("untitled");
        const words = [title, status(node.step.group, node.step.rawStatus)];
        if (node.ready) words.push(t("ready"));
        return {
          id: `s${node.index}`,
          type: "step",
          position: { x: node.x, y: node.y },
          width: node.width,
          height: node.height,
          draggable: false,
          connectable: false,
          selectable: false,
          ariaLabel: t("stepLabel", { key: node.key, words: words.join(", ") }),
          domAttributes: { title: `${node.key} ${title}` },
          data: { node, title, selected: node.key === selected, dimmed: outside(node.key) },
        };
      }),
    [graph, selected, outside, t, status],
  );
  const edges = useMemo<StepFlowEdge[]>(
    () =>
      graph.edges
        .filter((edge) => edge.kind === "drawn" && edge.fromIndex !== null && edge.points.length > 1)
        .map((edge) => ({
          id: `e${edge.fromIndex}-${edge.toIndex}`,
          type: "step",
          source: `s${edge.fromIndex}`,
          target: `s${edge.toIndex}`,
          selectable: false,
          focusable: false,
          data: {
            points: edge.points,
            done: edge.fromDone,
            lit: selected !== null && (edge.from === selected || edge.to === selected),
            dimmed: outside(edge.from) || outside(edge.to),
            from: edge.from,
            to: edge.to,
          },
        })),
    [graph, selected, outside],
  );

  const toggle = (key: string | undefined) => {
    if (key) setSelected((current) => (current === key ? null : key));
  };
  // Enter or Space on a focused step selects it, as a click does (React Flow's own keys move nodes, off here).
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const id = (event.target as HTMLElement).closest(".react-flow__node")?.getAttribute("data-id");
    if (!id) return;
    event.preventDefault();
    toggle(keyOf.get(id));
  };

  return (
    <div className={GRAPH_FRAME} data-testid="step-graph">
      <Legend graph={graph} />
      <div
        className="relative min-h-0 flex-1 bg-background [&_.react-flow__node:focus-visible]:rounded-md [&_.react-flow__node:focus-visible]:outline-2 [&_.react-flow__node:focus-visible]:outline-offset-2 [&_.react-flow__node:focus-visible]:outline-ring"
        onKeyDown={onKeyDown}
        data-testid="step-graph-canvas"
        data-ready={ready ? "true" : "false"}
        data-selected={selected ?? ""}
      >
        <ReactFlow<StepFlowNode, StepFlowEdge>
          nodes={nodes}
          edges={edges}
          nodeTypes={NODE_TYPES}
          edgeTypes={EDGE_TYPES}
          fitView
          fitViewOptions={FIT}
          minZoom={0.2}
          maxZoom={2}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          disableKeyboardA11y
          panOnDrag
          panOnScroll={false}
          zoomOnScroll={false}
          zoomOnDoubleClick={false}
          preventScrolling={false}
          onInit={() => setReady(true)}
          onNodeClick={(_, node) => toggle(node.data.node.key)}
          onPaneClick={() => setSelected(null)}
          ariaLabelConfig={{
            "node.a11yDescription.default": t("a11yStep"),
            "node.a11yDescription.keyboardDisabled": t("a11yStep"),
            "edge.a11yDescription.default": t("a11yEdge"),
          }}
          aria-label={t("canvas")}
        >
          <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="var(--border-strong)" />
          <Zoom />
        </ReactFlow>
      </div>
    </div>
  );
}

export default function StepGraph(props: StepGraphProps) {
  return (
    <ReactFlowProvider>
      <Graph {...props} />
    </ReactFlowProvider>
  );
}
