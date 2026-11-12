"use client";

import { useTranslations } from "next-intl";
import { useCallback, useMemo } from "react";

import { buildGraph, isCodeNode, type WorkingSet } from "@/lib/kg/entity-graph";

import SigmaCanvas from "./sigma-canvas";
import { useCanvasTheme } from "./use-canvas-theme";

/**
 * The Entities view's canvas: the working set's entities and relations (lib/kg/entity-graph.ts) drawn by the canvas
 * every view of the graph shares (./sigma-canvas.tsx), laid out by ForceAtlas2. Loaded with next/dynamic and no server
 * rendering, since sigma needs a window and WebGL. An opened neighbourhood grows beside the node it came from.
 *
 * A click selects an entity or a relation, a double click or Enter opens an entity's neighbours. With code drawn, a
 * Directory, File or Symbol is a disc in its kind's tone, as in the explorer; a click on one opens its node page. An
 * entity with code links carries their count after its name.
 */
export type EntityCanvasProps = {
  set: WorkingSet;
  selectedNode: string | null;
  selectedEdge: string | null;
  /** Name every relation on the canvas, not only those of what is pointed at or selected. */
  allEdgeLabels: boolean;
  /** A node to bring to the middle of the view, with a counter so asking twice moves twice. */
  centre: { id: string; nonce: number } | null;
  label: string;
  describedBy: string;
  onSelectNode: (id: string | null) => void;
  onSelectEdge: (id: string | null) => void;
  onExpand: (id: string) => void;
  /** A code node clicked: open its page of the graph. */
  onOpenCode: (id: string) => void;
};

export default function EntityCanvas({
  set,
  selectedNode,
  selectedEdge,
  allEdgeLabels,
  centre,
  label,
  describedBy,
  onSelectNode,
  onSelectEdge,
  onExpand,
  onOpenCode,
}: EntityCanvasProps) {
  const t = useTranslations("kg.explorer");
  const theme = useCanvasTheme();
  const graph = useMemo(() => buildGraph(set, theme), [set, theme]);
  const order = useMemo(
    () => [...set.nodes].sort((a, b) => b.docs - a.docs || a.name.localeCompare(b.name)).map((node) => node.id),
    [set.nodes],
  );
  const code = useCallback((id: string) => graph.hasNode(id) && isCodeNode(graph.getNodeAttributes(id)), [graph]);
  const select = useCallback(
    (id: string | null) => {
      if (id && code(id)) onOpenCode(id);
      else onSelectNode(id);
    },
    [code, onOpenCode, onSelectNode],
  );
  const activate = useCallback((id: string) => (code(id) ? undefined : onExpand(id)), [code, onExpand]);
  const describe = useCallback(
    (id: string) => {
      if (!graph.hasNode(id)) return null;
      const node = graph.getNodeAttributes(id);
      return node.isCode ? t("tipCode", { kind: node.kind }) : t("tipEntity", { kind: node.kind, count: node.docs });
    },
    [graph, t],
  );
  return (
    <SigmaCanvas
      graph={graph}
      theme={theme}
      layout="force"
      root={null}
      selected={selectedNode}
      selectedEdge={selectedEdge}
      allEdgeLabels={allEdgeLabels}
      order={order}
      centre={centre}
      describe={describe}
      label={label}
      describedBy={describedBy}
      testId="kg-entity-canvas"
      data={{ nodes: set.nodes.length, edges: set.edges.length, "code-nodes": set.codeNodes?.length ?? 0 }}
      onSelect={select}
      onActivate={activate}
      onSelectEdge={onSelectEdge}
    />
  );
}
