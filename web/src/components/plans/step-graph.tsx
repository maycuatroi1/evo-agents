"use client";

import "@xyflow/react/dist/base.css";

import { Background, BackgroundVariant, Position, ReactFlow, ReactFlowProvider, useReactFlow } from "@xyflow/react";
import { useTranslations } from "next-intl";
import { type FocusEvent, type KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { planCriteria, stepCriteria } from "@/lib/plan-graph/criteria";
import { layoutPlanGraph } from "@/lib/plan-graph/layout";
import { arrowDirection, neighbour } from "@/lib/plan-graph/navigate";
import { openingViewport } from "@/lib/plan-graph/viewport";
import { stepLabel } from "@/lib/plans";

import { useStepStatusText } from "./status";
import { Legend, motion, Zoom } from "./step-graph-chrome";
import { GRAPH_FRAME, type StepGraphProps } from "./step-graph-frame";
import { InspectorPanel, InspectorSheet, useWideInspector } from "./step-graph-inspector";
import { StepEdge, type StepFlowEdge, type StepFlowNode, StepNode } from "./step-graph-parts";
import { useSelectedStep } from "./step-view";

/**
 * The Graph view of a plan's steps: the layout of lib/plan-graph drawn by React Flow, read only. A legend above the
 * canvas counts the steps by status and the Ready ones, says what solid and dashed lines mean and how many
 * dependencies are not drawn, with the zoom and fit buttons at its end. The canvas opens on the whole plan, never past
 * zoom 1, unless that would be too small to read: then at zoom 0.6 from the top row (openingViewport), Fit still showing
 * all of it. A drag pans, the wheel scrolls the page. Loaded on demand (step-graph-lazy.tsx).
 *
 * A click, Enter or Space on a step selects it (the URL's `step`): the steps outside its ancestors and descendants dim,
 * and the inspector shows it (step-graph-inspector.tsx). Tab reaches the steps; the arrows move between them, Up and
 * Down to the nearest step of the row above or below, Left and Right along the row; Esc closes the inspector and
 * leaves the focus on the step. A step the focus or the inspector moves to is brought into view, at once under
 * prefers-reduced-motion.
 */

const NODE_TYPES = { step: StepNode };
const EDGE_TYPES = { step: StepEdge };

function Graph({ project, planId, steps, acceptance, matching }: StepGraphProps) {
  const t = useTranslations("plans.graph");
  const status = useStepStatusText();
  const flow = useReactFlow<StepFlowNode, StepFlowEdge>();
  const wide = useWideInspector();
  const canvas = useRef<HTMLDivElement>(null);
  const graph = useMemo(() => layoutPlanGraph(steps), [steps]);
  const criteria = useMemo(() => planCriteria(acceptance), [acceptance]);
  const [asked, select] = useSelectedStep();
  const [ready, setReady] = useState(false);
  // The step whose box gets the focus back when the inspector closes.
  const last = useRef<string | null>(null);

  const current = graph.nodes.find((node) => node.key === asked) ?? null;
  const selected = current?.key ?? null;
  const keyOf = useMemo(() => new Map(graph.nodes.map((node) => [`s${node.index}`, node.key])), [graph]);
  const related = useMemo(
    () => (current ? new Set([current.key, ...current.ancestors, ...current.descendants]) : null),
    [current],
  );
  const outside = useCallback(
    (key: string) => Boolean((related && !related.has(key)) || (matching && !matching.has(key))),
    [related, matching],
  );

  const boxOf = useCallback(
    (key: string) => {
      const node = graph.nodes.find((n) => n.key === key);
      return node ? canvas.current?.querySelector<HTMLElement>(`.react-flow__node[data-id="s${node.index}"]`) : null;
    },
    [graph],
  );
  /** Pans the canvas, keeping its zoom, so the step's box is inside it; nothing when it already is. */
  const reveal = useCallback(
    (key: string) => {
      const node = graph.nodes.find((n) => n.key === key);
      const frame = canvas.current?.getBoundingClientRect();
      if (!node || !frame) return;
      const { x, y, zoom } = flow.getViewport();
      const left = node.x * zoom + x;
      const top = node.y * zoom + y;
      const inside = left >= 0 && top >= 0 && left + node.width * zoom <= frame.width && top + node.height * zoom <= frame.height;
      if (inside) return;
      void flow.setCenter(node.x + node.width / 2, node.y + node.height / 2, { zoom, ...motion(240) });
    },
    [graph, flow],
  );
  const focusStep = useCallback((key: string | null) => (key ? boxOf(key)?.focus({ preventScroll: true }) : undefined), [boxOf]);

  const open = (key: string | undefined) => {
    if (key) select(key);
  };
  /** From the inspector: select the step, bring it into view, and keep the focus where a keyboard user can go on. */
  const jump = (key: string) => {
    open(key);
    reveal(key);
    requestAnimationFrame(() => {
      if (wide) focusStep(key);
      else document.querySelector<HTMLElement>('[data-layout="sheet"] [data-testid="inspector-title"]')?.focus();
    });
  };
  const close = () => {
    select(null);
    if (wide) focusStep(selected);
  };
  useEffect(() => {
    if (selected) last.current = selected;
  }, [selected]);

  // A link to a selected step opens the graph with that step in view.
  const revealed = useRef(false);
  useEffect(() => {
    if (!ready || revealed.current) return;
    revealed.current = true;
    if (selected) requestAnimationFrame(() => reveal(selected));
  }, [ready, selected, reveal]);

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
          // The handles are declared rather than measured: these nodes are rebuilt on every selection without
          // `measured`, which drops the measured handles, and a line waits for them; under load the re-measure could
          // be lost and the graph stayed without lines.
          handles: [
            { type: "target", position: Position.Top, x: node.width / 2, y: 0, width: 1, height: 1 },
            { type: "source", position: Position.Bottom, x: node.width / 2, y: node.height, width: 1, height: 1 },
          ],
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

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const id = (event.target as HTMLElement).closest(".react-flow__node")?.getAttribute("data-id");
    const key = id ? keyOf.get(id) : undefined;
    if (event.key === "Escape" && selected) {
      event.preventDefault();
      close();
      return;
    }
    if (!key) return;
    const direction = arrowDirection(event.key);
    if (direction) {
      event.preventDefault();
      focusStep(neighbour(graph.nodes, key, direction));
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      open(key);
    }
  };
  // React Flow pans to a focused step only with its own keyboard handling, which is off: do it here.
  const onFocus = (event: FocusEvent<HTMLDivElement>) => {
    const id = event.target.closest(".react-flow__node")?.getAttribute("data-id");
    const key = id ? keyOf.get(id) : undefined;
    if (key && event.target.matches(":focus-visible")) reveal(key);
  };

  const inspector = {
    graph,
    project,
    planId,
    node: current,
    criteria: current ? stepCriteria(current.step, criteria) : [],
    onSelect: jump,
    onClose: close,
  };

  return (
    <div className={GRAPH_FRAME} data-testid="step-graph">
      <Legend graph={graph}>
        <Zoom />
      </Legend>
      <div className="flex min-h-0 flex-1">
        <div
          ref={canvas}
          className="plan-graph relative min-h-0 min-w-0 flex-1 bg-background"
          onKeyDown={onKeyDown}
          onFocus={onFocus}
          data-testid="step-graph-canvas"
          data-ready={ready ? "true" : "false"}
          data-selected={selected ?? ""}
        >
          <ReactFlow<StepFlowNode, StepFlowEdge>
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            edgeTypes={EDGE_TYPES}
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
            onInit={(instance) => {
              const frame = canvas.current?.getBoundingClientRect();
              if (frame) void instance.setViewport(openingViewport(graph, frame));
              setReady(true);
            }}
            onNodeClick={(_, node) => open(node.data.node.key)}
            onPaneClick={() => (selected ? select(null) : undefined)}
            ariaLabelConfig={{
              "node.a11yDescription.default": t("a11yStep"),
              "node.a11yDescription.keyboardDisabled": t("a11yStep"),
              "edge.a11yDescription.default": t("a11yEdge"),
            }}
            aria-label={t("canvas")}
          >
            <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="var(--border-strong)" />
          </ReactFlow>
        </div>
        {wide ? <InspectorPanel {...inspector} /> : null}
      </div>
      {wide ? null : <InspectorSheet {...inspector} onClosed={() => focusStep(last.current)} />}
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
