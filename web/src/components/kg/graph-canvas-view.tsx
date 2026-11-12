"use client";

import { useTranslations } from "next-intl";
import { useCallback, useMemo } from "react";

import { displayName, orderNodes } from "@/lib/kg/graph";
import { MORE, neighbourhoodGraph } from "@/lib/kg/neighbourhood-graph";
import type { Neighbourhood } from "@/lib/kg/types";

import type { ExplorerLayout } from "./explorer-layout";
import SigmaCanvas from "./sigma-canvas";
import { useCanvasTheme } from "./use-canvas-theme";

/**
 * A node's neighbourhood on its page, drawn by the canvas every view of the graph shares (./sigma-canvas.tsx). The
 * neighbour table beside it carries the same data and the same selection, and this view only mirrors it. Keyboard:
 * arrows step through the nodes (the table's order), Home goes back to the node shown, Enter opens the selected node.
 * The node's groups of direct neighbours that hold more than the view draws end in one hollow "+N kind" node each, as
 * the table's "show more" does: choosing it opens the group.
 */
export type GraphCanvasProps = {
  view: Neighbourhood;
  selected: string;
  layout: ExplorerLayout;
  label: string;
  describedBy: string;
  onSelect: (id: string) => void;
  onOpen: (id: string) => void;
  /** Show the whole of a group of the node's direct neighbours, by its key. */
  onOpenGroup: (key: string) => void;
};

export default function GraphCanvasView({
  view,
  selected,
  layout,
  label,
  describedBy,
  onSelect,
  onOpen,
  onOpenGroup,
}: GraphCanvasProps) {
  const t = useTranslations("kg.explorer");
  const theme = useCanvasTheme();
  const graph = useMemo(() => neighbourhoodGraph(view, theme), [view, theme]);
  const order = useMemo(() => orderNodes(view.nodes).map((node) => node.id), [view.nodes]);
  const byId = useMemo(() => new Map(view.nodes.map((node) => [node.id, node])), [view.nodes]);
  const select = useCallback(
    (id: string | null) => {
      if (id?.startsWith(MORE)) onOpenGroup(id.slice(MORE.length));
      else onSelect(id ?? view.focus);
    },
    [onOpenGroup, onSelect, view.focus],
  );
  const activate = useCallback((id: string) => (id.startsWith(MORE) ? undefined : onOpen(id)), [onOpen]);
  const describe = useCallback(
    (id: string) => {
      const node = byId.get(id);
      if (!node) return null;
      return t("tipNode", { kind: node.kind, count: node.degree ?? 0 });
    },
    [byId, t],
  );
  const keys = useMemo(() => ({ Home: () => onSelect(view.focus) }), [onSelect, view.focus]);
  return (
    <div className="relative h-[28rem] overflow-hidden rounded-md border bg-background xl:h-[32rem]">
      <SigmaCanvas
        graph={graph}
        theme={theme}
        layout={layout}
        root={view.focus}
        selected={selected}
        order={order}
        describe={describe}
        keys={keys}
        label={label || displayName(byId.get(view.focus) ?? { id: view.focus })}
        describedBy={describedBy}
        testId="kg-canvas"
        onSelect={select}
        onActivate={activate}
      />
    </div>
  );
}
