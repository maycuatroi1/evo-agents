"use client";

import { useTranslations } from "next-intl";
import { useCallback, useMemo } from "react";

import { type ExplorerElement, type Scope, stepOrder } from "@/lib/kg/explorer";
import { explorerGraph } from "@/lib/kg/explorer-graph";
import { parseMoreId } from "@/lib/kg/explorer-working";

import type { ExplorerLayout } from "./explorer-layout";
import SigmaCanvas from "./sigma-canvas";
import { useCanvasTheme } from "./use-canvas-theme";

/**
 * The explorer's canvas: the scope's elements (lib/kg/explorer.ts) drawn by the canvas every view of the graph shares
 * (./sigma-canvas.tsx). One canvas per scope (the explorer keys this component by scope); inside a scope the graph
 * changes in place, so nodes the reader expands appear beside the node they came from and the rest keep their places.
 *
 * A click selects, a double click or Enter activates (opens a cluster, expands a node); a click on a "+N" node shows
 * the group's other neighbours. Backspace goes up a scope, Delete hides a node where hiding applies.
 */
export type ExplorerCanvasProps = {
  scope: Scope;
  elements: readonly ExplorerElement[];
  selected: string | null;
  /** A node the rail points at (hovered in a list); lit like the one under the pointer. */
  highlight: string | null;
  layout: ExplorerLayout;
  /** The node the tree and radial layouts start from. */
  root: string | null;
  label: string;
  describedBy: string;
  onSelect: (id: string | null) => void;
  onActivate: (id: string) => void;
  onBack: () => void;
  onHide?: (id: string) => void;
};

export default function ExplorerCanvasView({
  scope,
  elements,
  selected,
  highlight,
  layout,
  root,
  label,
  describedBy,
  onSelect,
  onActivate,
  onBack,
  onHide,
}: ExplorerCanvasProps) {
  const t = useTranslations("kg.explorer");
  const theme = useCanvasTheme();
  const graph = useMemo(() => explorerGraph(elements, theme), [elements, theme]);
  const order = useMemo(() => stepOrder(elements), [elements]);
  const byId = useMemo(() => new Map(elements.map((element) => [String(element.data.id), element])), [elements]);
  const select = useCallback((id: string | null) => (id && parseMoreId(id) ? onActivate(id) : onSelect(id)), [onActivate, onSelect]);
  const activate = useCallback((id: string) => (parseMoreId(id) ? undefined : onActivate(id)), [onActivate]);
  const describe = useCallback(
    (id: string) => {
      const element = byId.get(id);
      if (!element) return null;
      const { data } = element;
      const classes = element.classes ?? [];
      if (classes.includes("cluster")) return t("tipCluster", { count: Number(data.size ?? 0) });
      if (classes.includes("more")) return t("tipMore", { rel: String(data.rel ?? "") });
      return t("tipNode", { kind: String(data.kind ?? ""), count: Number(data.degree ?? 0) });
    },
    [byId, t],
  );
  const keys = useMemo(
    () => ({
      Backspace: onBack,
      Delete: () => {
        if (selected && onHide) onHide(selected);
      },
    }),
    [onBack, onHide, selected],
  );
  return (
    <div className="relative h-full min-h-0 w-full overflow-hidden">
      <SigmaCanvas
        graph={graph}
        theme={theme}
        layout={layout}
        root={root}
        selected={selected}
        highlight={highlight}
        order={order}
        describe={describe}
        keys={keys}
        label={label}
        describedBy={describedBy}
        testId="kg-explorer-canvas"
        data={{ scope: scope.kind, nodes: elements.filter((element) => element.group === "nodes").length }}
        onSelect={select}
        onActivate={activate}
      />
    </div>
  );
}
