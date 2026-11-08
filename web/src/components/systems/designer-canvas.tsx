"use client";

import "@xyflow/react/dist/base.css";

import { Background, BackgroundVariant, type Connection, ConnectionMode, type NodeChange, Panel, ReactFlow, useReactFlow } from "@xyflow/react";
import { useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";

import type { SpecDiff } from "@/lib/system-diff";
import { type EdgeKind, type Join, joins, outdated, type PlacedProblems, type Position, worst } from "@/lib/systems";
import { bendsOf, focusOf, type Line, type Pointer } from "@/lib/systems-canvas";
import { drawnAt, NODE_SIZE, ROUND_KINDS, slotOf } from "@/lib/systems-layout";

import { SystemEdge, type SystemFlowEdge } from "./designer-edge";
import { handlesOf, type SystemFlowNode, SystemNode } from "./designer-nodes";
import { SystemLegend } from "./system-legend";
import type { Designer } from "./use-designer";

const NODE_TYPES = { system: SystemNode };
const EDGE_TYPES = { system: SystemEdge };

/** The join a drag from `source` to `target` makes: the one in the drag's direction first, else the reverse. */
export function joinOf(options: readonly Join[], source: string): Join | null {
  return options.find((join) => join.from === source) ?? options[0] ?? null;
}

/** The node under a point of the screen, for a line dropped on a box rather than on its handle. */
function nodeAt(x: number, y: number): string | null {
  for (const element of document.elementsFromPoint(x, y)) {
    const box = element.closest(".react-flow__node");
    const id = box?.getAttribute("data-id");
    if (id) return id;
  }
  return null;
}

type Props = {
  designer: Designer;
  placed: PlacedProblems;
  canChange: boolean;
  /** A join made by a drag, to add to the draft. */
  onJoin: (join: Join) => void;
  onUpgrade: (id: string, version: number) => void;
  /** A proposal of @builder to draw instead of the draft: read-only, what it adds or drops faded and dashed. */
  preview?: SpecDiff | null;
};

/**
 * The canvas of the designer, drawn by React Flow from the draft: a drag moves a box (one undo step when it ends), a
 * drag from a box's handle to another box joins them with the one edge their kinds take, a click selects a box or an
 * edge's label, a click on the empty canvas clears the selection. The pointer on a node or a line fades what it does
 * not touch, and the lines it touches, or the selection's, say their words. With a `preview`, it draws the proposal
 * instead and takes no change.
 */
export function DesignerCanvas({ designer, placed: drafted, canChange: mayChange, onJoin, onUpgrade, preview = null }: Props) {
  const t = useTranslations("systems.designer");
  const flow = useReactFlow<SystemFlowNode, SystemFlowEdge>();
  const { spec, selection, latest, select, change } = designer;
  const [moving, setMoving] = useState<Map<string, Position>>(new Map());
  const [from, setFrom] = useState<string | null>(null);
  const [pointer, setPointer] = useState<Pointer>(null);
  const [showOptimizes, setShowOptimizes] = useState(false);
  const faces = useMemo(() => new Map(designer.agents.filter((agent) => agent.deleted_at === null).map((agent) => [agent.handle, agent])), [designer.agents]);
  const known = designer.agents.length > 0;
  const canChange = mayChange && preview === null;
  const placed = useMemo<PlacedProblems>(() => (preview ? { system: [], nodes: new Map(), edges: new Map() } : drafted), [preview, drafted]);
  const shownNodes = useMemo(() => preview?.nodes ?? spec.nodes.map((node) => ({ node, ghost: null })), [preview, spec.nodes]);
  const shownEdges = useMemo(() => preview?.edges ?? spec.edges.map((edge) => ({ edge, ghost: null })), [preview, spec.edges]);
  const lines = useMemo<Line[]>(() => shownEdges.map(({ edge }, index) => ({ id: `e${index}`, source: edge.from, target: edge.to })), [shownEdges]);
  const bends = useMemo(() => bendsOf(lines), [lines]);
  // The pointer fades what it does not touch, except while a line is dragged or a node moved; the selection only
  // brings its lines' words out.
  const hovered = useMemo(() => (from === null && moving.size === 0 ? focusOf(lines, pointer) : null), [from, moving.size, lines, pointer]);
  const focus = useMemo(() => {
    if (hovered) return hovered;
    if (preview !== null || selection === null) return null;
    return focusOf(lines, "node" in selection ? { node: selection.node } : { line: `e${selection.edge}` });
  }, [hovered, preview, selection, lines]);
  const optimized = useMemo(() => new Set(shownEdges.filter(({ edge }) => edge.kind === "optimizes").map(({ edge }) => edge.to)), [shownEdges]);
  const kinds = useMemo(() => new Set<EdgeKind>(shownEdges.map(({ edge }) => edge.kind)), [shownEdges]);

  const nodes = useMemo<SystemFlowNode[]>(
    () =>
      shownNodes.map(({ node, ghost }) => {
        const problems = placed.nodes.get(node.id) ?? [];
        const face = node.profile ? (faces.get(node.profile) ?? null) : null;
      // An agent without a label goes by its profile's name: its @handle is already the pin under it.
      const title = node.label || (node.kind === "kg" ? (node.graph ?? "default") : (face?.name ?? (node.profile ? `@${node.profile}` : t(`kinds.${node.kind}`))));
        const drop = from === null ? null : from === node.id ? null : (joinOf(joins(spec, from, node.id), from)?.kind ?? "none");
        return {
          id: node.id,
          type: "system",
          position: moving.get(node.id) ?? drawnAt(node.kind, node.position),
          width: NODE_SIZE[node.kind].width,
          height: NODE_SIZE[node.kind].height,
          handles: handlesOf(node.kind),
          className: ROUND_KINDS.has(node.kind) ? "system-node-round" : undefined,
          draggable: canChange,
          connectable: canChange,
          selected: preview === null && selection !== null && "node" in selection && selection.node === node.id,
          ariaLabel: t("nodeLabel", { kind: t(`kinds.${node.kind}`), name: title, problems: problems.length }),
          data: {
            node,
            title,
            level: worst(problems),
            problems,
            newer: outdated(node, latest),
            // A proposal names the profiles it makes: none of its nodes is missing one yet.
            missing: preview === null && known && Boolean(node.profile) && !faces.has(node.profile ?? ""),
            face,
            optimized: optimized.has(node.id),
            dimmed: hovered ? !hovered.nodes.has(node.id) : false,
            selected: preview === null && selection !== null && "node" in selection && selection.node === node.id,
            drop,
            canChange,
            onUpgrade,
            ghost,
          },
        };
      }),
    [shownNodes, preview, spec, placed, moving, from, selection, latest, faces, known, canChange, onUpgrade, optimized, hovered, t],
  );

  const edges = useMemo<SystemFlowEdge[]>(
    () =>
      shownEdges.map(({ edge, ghost }, index) => {
        const problems = placed.edges.get(index) ?? [];
        const selected = preview === null && selection !== null && "edge" in selection && selection.edge === index;
        const id = `e${index}`;
        return {
          id,
          type: "system",
          source: edge.from,
          target: edge.to,
          selected,
          focusable: false,
          // The Curator's lines stay undrawn (its mark on a node says it optimizes it) unless asked, in focus, selected,
          // wrong or part of a proposal.
          hidden: edge.kind === "optimizes" && !showOptimizes && !focus?.lines.has(id) && !selected && !problems.length && ghost === null,
          data: {
            kind: edge.kind,
            description: edge.description,
            level: worst(problems),
            selected,
            label: t("edgeLabel", { kind: t(`edgeKinds.${edge.kind}`), from: edge.from, to: edge.to, problems: problems.length }),
            onSelect: () => (preview === null ? select({ edge: index }) : undefined),
            bend: bends.get(id) ?? 0,
            emphasis: focus?.lines.has(id) ?? false,
            dimmed: hovered ? !hovered.lines.has(id) : false,
            ghost,
          },
        };
      }),
    [shownEdges, preview, placed, selection, select, bends, focus, hovered, showOptimizes, t],
  );

  const onNodesChange = useCallback((changes: NodeChange<SystemFlowNode>[]) => {
    setMoving((current) => {
      let next: Map<string, Position> | null = null;
      for (const item of changes) {
        if (item.type !== "position" || !item.position || !item.dragging) continue;
        next ??= new Map(current);
        next.set(item.id, item.position);
      }
      return next ?? current;
    });
  }, []);

  const commitMove = useCallback(() => {
    if (!moving.size) return;
    const placedNow = moving;
    change((current) => ({
      ...current,
      nodes: current.nodes.map((node) => {
        const drawn = placedNow.get(node.id);
        return drawn ? { ...node, position: slotOf(node.kind, drawn) } : node;
      }),
    }));
    setMoving(new Map());
  }, [moving, change]);

  const join = useCallback(
    (source: string, target: string) => {
      const made = joinOf(joins(spec, source, target), source);
      if (made) onJoin(made);
    },
    [spec, onJoin],
  );

  return (
    <ReactFlow<SystemFlowNode, SystemFlowEdge>
      nodes={nodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      edgeTypes={EDGE_TYPES}
      connectionMode={ConnectionMode.Loose}
      connectionRadius={36}
      minZoom={0.25}
      maxZoom={1.75}
      fitView
      fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
      nodesDraggable={canChange}
      nodesConnectable={canChange}
      deleteKeyCode={null}
      selectionKeyCode={null}
      multiSelectionKeyCode={null}
      zoomOnDoubleClick={false}
      onNodesChange={onNodesChange}
      onNodeDragStop={commitMove}
      onNodeClick={(_, node) => (preview === null ? select({ node: node.id }) : undefined)}
      onEdgeClick={(_, edge) => (preview === null ? select({ edge: Number(edge.id.slice(1)) }) : undefined)}
      onPaneClick={() => select(null)}
      onNodeMouseEnter={(_, node) => setPointer({ node: node.id })}
      onNodeMouseLeave={() => setPointer(null)}
      onEdgeMouseEnter={(_, edge) => setPointer({ line: edge.id })}
      onEdgeMouseLeave={() => setPointer(null)}
      isValidConnection={(connection) => joins(spec, connection.source, connection.target).length > 0}
      onConnectStart={(_, params) => setFrom(params.nodeId)}
      onConnect={(connection: Connection) => {
        setFrom(null);
        join(connection.source, connection.target);
      }}
      onConnectEnd={(event, state) => {
        setFrom(null);
        if (state.isValid || !state.fromNode) return;
        const point = "changedTouches" in event ? event.changedTouches[0] : event;
        const target = point ? nodeAt(point.clientX, point.clientY) : null;
        if (target) join(state.fromNode.id, target);
      }}
      onInit={() => void flow.fitView({ padding: 0.2, maxZoom: 1 })}
      ariaLabelConfig={{
        "node.a11yDescription.default": t("a11yNode"),
        "node.a11yDescription.keyboardDisabled": t("a11yNode"),
        "edge.a11yDescription.default": t("a11yEdge"),
      }}
      aria-label={t("canvas")}
    >
      <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="var(--border-strong)" />
      <Panel position="bottom-left">
        <SystemLegend kinds={kinds} showOptimizes={showOptimizes} onShowOptimizes={setShowOptimizes} />
      </Panel>
    </ReactFlow>
  );
}
