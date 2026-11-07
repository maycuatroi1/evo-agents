"use client";

import "@xyflow/react/dist/base.css";

import {
  Background,
  BackgroundVariant,
  type Connection,
  ConnectionMode,
  MarkerType,
  type NodeChange,
  ReactFlow,
  useReactFlow,
} from "@xyflow/react";
import { useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";

import type { SpecDiff } from "@/lib/system-diff";
import { type Join, joins, outdated, type PlacedProblems, type Position, worst } from "@/lib/systems";
import { NODE_HEIGHT, NODE_WIDTH } from "@/lib/systems-layout";

import { NODE_HANDLES, SystemEdge, type SystemFlowEdge, type SystemFlowNode, SystemNode } from "./designer-nodes";
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
 * edge's label, a click on the empty canvas clears the selection. With a `preview`, it draws the proposal instead and
 * takes no change.
 */
export function DesignerCanvas({ designer, placed: drafted, canChange: mayChange, onJoin, onUpgrade, preview = null }: Props) {
  const t = useTranslations("systems.designer");
  const flow = useReactFlow<SystemFlowNode, SystemFlowEdge>();
  const { spec, selection, latest, select, change } = designer;
  const [moving, setMoving] = useState<Map<string, Position>>(new Map());
  const [from, setFrom] = useState<string | null>(null);
  const handles = useMemo(() => new Set(designer.agents.filter((agent) => agent.deleted_at === null).map((agent) => agent.handle)), [designer.agents]);
  const known = designer.agents.length > 0;
  const canChange = mayChange && preview === null;
  const placed = useMemo<PlacedProblems>(() => (preview ? { system: [], nodes: new Map(), edges: new Map() } : drafted), [preview, drafted]);
  const shownNodes = useMemo(() => preview?.nodes ?? spec.nodes.map((node) => ({ node, ghost: null })), [preview, spec.nodes]);
  const shownEdges = useMemo(() => preview?.edges ?? spec.edges.map((edge) => ({ edge, ghost: null })), [preview, spec.edges]);

  const nodes = useMemo<SystemFlowNode[]>(
    () =>
      shownNodes.map(({ node, ghost }) => {
        const problems = placed.nodes.get(node.id) ?? [];
        const title = node.label || (node.kind === "kg" ? (node.graph ?? "default") : node.profile ? `@${node.profile}` : t(`kinds.${node.kind}`));
        const drop = from === null ? null : from === node.id ? null : (joinOf(joins(spec, from, node.id), from)?.kind ?? "none");
        return {
          id: node.id,
          type: "system",
          position: moving.get(node.id) ?? node.position,
          width: NODE_WIDTH,
          height: NODE_HEIGHT,
          handles: NODE_HANDLES,
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
            missing: preview === null && known && Boolean(node.profile) && !handles.has(node.profile ?? ""),
            selected: preview === null && selection !== null && "node" in selection && selection.node === node.id,
            drop,
            canChange,
            onUpgrade,
            ghost,
          },
        };
      }),
    [shownNodes, preview, spec, placed, moving, from, selection, latest, handles, known, canChange, onUpgrade, t],
  );

  const edges = useMemo<SystemFlowEdge[]>(
    () =>
      shownEdges.map(({ edge, ghost }, index) => {
        const problems = placed.edges.get(index) ?? [];
        const selected = preview === null && selection !== null && "edge" in selection && selection.edge === index;
        return {
          id: `e${index}`,
          type: "system",
          source: edge.from,
          target: edge.to,
          selected,
          focusable: false,
          markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
          data: {
            kind: edge.kind,
            description: edge.description,
            level: worst(problems),
            selected,
            label: t("edgeLabel", { kind: t(`edgeKinds.${edge.kind}`), from: edge.from, to: edge.to, problems: problems.length }),
            onSelect: () => (preview === null ? select({ edge: index }) : undefined),
            ghost,
          },
        };
      }),
    [shownEdges, preview, placed, selection, select, t],
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
    change((current) => ({ ...current, nodes: current.nodes.map((node) => ({ ...node, position: placedNow.get(node.id) ?? node.position })) }));
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
    </ReactFlow>
  );
}
