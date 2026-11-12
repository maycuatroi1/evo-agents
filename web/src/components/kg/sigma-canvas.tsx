"use client";

import { MonitorX } from "lucide-react";
import { useTranslations } from "next-intl";
import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import Sigma from "sigma";
import { EdgeArrowProgram, EdgeRectangleProgram } from "sigma/rendering";
import { animateNodes } from "sigma/utils";

import { Skeleton } from "@/components/ui/skeleton";
import { useMediaQuery } from "@/hooks/use-media-query";
import { type CanvasEdge, type CanvasGraph, type CanvasLayout, type CanvasNode, canvasGraph, layoutPositions, type Place, placeNodes } from "@/lib/kg/canvas-graph";
import type { Theme } from "@/lib/kg/canvas-theme";

import { ZoomControls } from "./canvas-controls";
import { hoverDrawer, labelDrawer } from "./canvas-drawers";
import { chooseLabels, LABEL_SIZE, LABEL_WEIGHT } from "./canvas-labels";
import { reduceEdge, reduceNode, type ViewState } from "./canvas-reducers";
import NodeRingProgram from "./node-ring";

/**
 * Every canvas of the knowledge graph: the explorer's scopes (explorer-canvas.tsx), a node's page (graph-canvas.tsx)
 * and the Entities view (entity-canvas.tsx) draw with this one, sigma.js 3 (WebGL) over a graphology graph whose nodes
 * and edges carry lib/kg/canvas-graph.ts's attributes. It needs a window and WebGL, so the canvases load it with
 * next/dynamic and no server rendering. One sigma per mount; a new graph takes the places the last one had, and the
 * view is laid out again (animated, unless the reader asks for less motion) when nodes come in or the layout changes.
 *
 * Pointer: a click selects (a relation too, where the view takes `onSelectEdge`), a double click activates, the wheel
 * zooms and a drag pans. Pointing at a node lights it, its neighbours and their relations, names them, and says what
 * the node is (`describe`); everything else dims. Keyboard, on the focused canvas: arrows step through `order`, Enter
 * activates the selection, Escape clears it, + and - zoom, 0 fits; a view adds its own keys. A canvas says nothing to a
 * screen reader: each view lists the same nodes and relations beside it and follows the same selection.
 */
export type SigmaCanvasProps = {
  graph: CanvasGraph;
  theme: Theme;
  layout: CanvasLayout;
  /** The node the radial and tree layouts start from. */
  root: string | null;
  selected: string | null;
  /** A node a list beside the canvas points at; lit like the one under the pointer. */
  highlight?: string | null;
  selectedEdge?: string | null;
  /** Name every relation, not only those of what is pointed at or selected. */
  allEdgeLabels?: boolean;
  /** The ids the arrow keys step through, in the order a list beside the canvas reads them. */
  order: readonly string[];
  /** A node to bring to the middle of the view, with a counter so asking twice moves twice. */
  centre?: { id: string; nonce: number } | null;
  /** What the node under the pointer is, under its name ("File, 12 edges"). */
  describe?: (id: string) => string | null;
  /** Keys of the view, beside the canvas's own. */
  keys?: Record<string, () => void>;
  label: string;
  describedBy: string;
  testId: string;
  /** More data-* attributes of the canvas, for tests. */
  data?: Record<string, string | number>;
  onSelect: (id: string | null) => void;
  onActivate: (id: string) => void;
  onSelectEdge?: (id: string | null) => void;
};


export default function SigmaCanvas({
  graph,
  theme,
  layout,
  root,
  selected,
  highlight = null,
  selectedEdge = null,
  allEdgeLabels = false,
  order,
  centre = null,
  describe,
  keys,
  label,
  describedBy,
  testId,
  data,
  onSelect,
  onActivate,
  onSelectEdge,
}: SigmaCanvasProps) {
  const t = useTranslations("kg.explorer");
  const box = useRef<HTMLDivElement>(null);
  const sigma = useRef<Sigma<CanvasNode, CanvasEdge> | null>(null);
  // The graph sigma draws now, and the layout and root it was laid out with.
  const drawn = useRef<{ graph: CanvasGraph | null; layout: CanvasLayout | null; root: string | null }>({ graph: null, layout: null, root: null });
  // What the reducers read: kept in refs so pointing at a node refreshes the canvas without a React render.
  const view = useRef<ViewState>({ hovered: null, hoveredEdge: null, selected, highlight, selectedEdge, allEdgeLabels, theme });
  const handlers = useRef({ onSelect, onActivate, onSelectEdge, describe });
  const tapped = useRef(false);
  const stopAnimation = useRef<(() => void) | null>(null);
  // The labels canvas-labels.ts chose to show (null until the first drawing), and how to choose them again.
  const chosen = useRef<Set<string> | null>(null);
  const relabel = useRef<(stale?: boolean) => void>(() => {});
  const reduced = useMediaQuery("(prefers-reduced-motion: reduce)");
  const [status, setStatus] = useState<"drawing" | "ready" | "failed">("drawing");

  useEffect(() => {
    handlers.current = { onSelect, onActivate, onSelectEdge, describe };
  }, [onSelect, onActivate, onSelectEdge, describe]);

  // One sigma for the life of the canvas.
  const edgeEvents = Boolean(onSelectEdge);
  useEffect(() => {
    const container = box.current;
    if (!container) return;
    let renderer: Sigma<CanvasNode, CanvasEdge>;
    try {
      renderer = new Sigma<CanvasNode, CanvasEdge>(canvasGraph(), container, {
        renderEdgeLabels: true,
        enableEdgeEvents: edgeEvents,
        hideEdgesOnMove: false,
        labelDensity: 0.6,
        labelGridCellSize: 90,
        labelRenderedSizeThreshold: 6,
        labelSize: LABEL_SIZE,
        labelWeight: LABEL_WEIGHT,
        edgeLabelSize: 11,
        zIndex: true,
        stagePadding: 40,
        minCameraRatio: 0.05,
        maxCameraRatio: 8,
        allowInvalidContainer: true,
        defaultNodeType: "ring",
        nodeProgramClasses: { ring: NodeRingProgram },
        defaultEdgeType: "line",
        edgeProgramClasses: { line: EdgeRectangleProgram, arrow: EdgeArrowProgram },
        nodeReducer: (node, attributes) =>
          reduceNode(node, attributes, view.current, drawn.current.graph, chosen.current, handlers.current.describe),
        edgeReducer: (edge, attributes) => reduceEdge(edge, attributes, view.current, drawn.current.graph),
      });
    } catch (error) {
      // No WebGL (switched off, or a GPU the browser refuses): the lists beside the canvas carry the same data.
      console.error("kg canvas: sigma could not start", error);
      const frame = requestAnimationFrame(() => setStatus("failed"));
      return () => cancelAnimationFrame(frame);
    }
    let settle: ReturnType<typeof setTimeout> | undefined;
    relabel.current = (stale = false) => {
      const g = drawn.current.graph;
      if (!g) return;
      // After a new graph or moved nodes, a full refresh first: sigma hands back the places it drew last.
      if (stale) renderer.refresh();
      chosen.current = chooseLabels(renderer, g, { ...view.current, font: view.current.theme.font });
      renderer.refresh();
    };
    // Once a zoom or a pan settles, the labels with room show.
    renderer.getCamera().on("updated", () => {
      clearTimeout(settle);
      settle = setTimeout(() => relabel.current(), 90);
    });
    const refresh = () => relabel.current();
    renderer.on("enterNode", ({ node }) => {
      view.current.hovered = node;
      container.style.cursor = "pointer";
      refresh();
    });
    renderer.on("leaveNode", () => {
      view.current.hovered = null;
      container.style.cursor = "";
      refresh();
    });
    renderer.on("enterEdge", ({ edge }) => {
      view.current.hoveredEdge = edge;
      container.style.cursor = "pointer";
      refresh();
    });
    renderer.on("leaveEdge", () => {
      view.current.hoveredEdge = null;
      container.style.cursor = "";
      refresh();
    });
    renderer.on("clickNode", ({ node }) => {
      tapped.current = true;
      handlers.current.onSelect(node);
    });
    renderer.on("clickEdge", ({ edge }) => handlers.current.onSelectEdge?.(edge));
    renderer.on("clickStage", () => {
      handlers.current.onSelect(null);
      handlers.current.onSelectEdge?.(null);
    });
    renderer.on("doubleClickNode", (payload) => {
      payload.preventSigmaDefault();
      handlers.current.onActivate(payload.node);
    });
    // The labels shown, for the tests: sigma's label grid keeps any two from covering each other.
    renderer.on("afterRender", () => {
      container.dataset.labels = String(renderer.getNodeDisplayedLabels().size);
    });
    // The inspector opening or closing, the window or full screen: sigma follows the container's size.
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => relabel.current(true));
    observer?.observe(container);
    sigma.current = renderer;
    return () => {
      clearTimeout(settle);
      relabel.current = () => {};
      chosen.current = null;
      observer?.disconnect();
      stopAnimation.current?.();
      stopAnimation.current = null;
      renderer.kill();
      sigma.current = null;
      drawn.current = { graph: null, layout: null, root: null };
    };
  }, [edgeEvents]);

  // The theme: the drawers, the fonts and the colours the reducers dim and light with.
  useEffect(() => {
    view.current.theme = theme;
    sigma.current?.setSettings({
      labelFont: theme.font,
      edgeLabelFont: theme.font,
      labelColor: { color: theme.foreground },
      edgeLabelColor: { color: theme.muted },
      defaultDrawNodeHover: hoverDrawer(theme),
      defaultDrawNodeLabel: labelDrawer(theme),
    });
  }, [theme]);

  // A new graph, layout or root: the places the last graph had, then a layout when nodes came in or the layout changed.
  useEffect(() => {
    const renderer = sigma.current;
    if (!renderer) return;
    const before = drawn.current;
    const previous = new Map<string, Place>();
    before.graph?.forEachNode((node, attributes) => previous.set(node, { x: attributes.x, y: attributes.y }));
    const first = previous.size === 0;
    const fresh = placeNodes(graph, previous);
    const moved = before.layout !== layout || (layout !== "force" && before.root !== root);
    stopAnimation.current?.();
    stopAnimation.current = null;
    drawn.current = { graph, layout, root };
    if (graph.order === 0 || (!first && fresh.length === 0 && !moved)) {
      renderer.setGraph(graph);
      relabel.current(true);
      return;
    }
    const settled = !first && !moved && fresh.length < graph.order / 4;
    const targets = Object.fromEntries(layoutPositions(graph, layout, { root, settled }));
    const camera = renderer.getCamera();
    if (first || reduced) {
      for (const [node, place] of Object.entries(targets)) graph.mergeNodeAttributes(node, place);
      renderer.setGraph(graph);
      camera.setState({ x: 0.5, y: 0.5, ratio: 1, angle: 0 });
      relabel.current(true);
      if (first) requestAnimationFrame(() => setStatus("ready"));
      return;
    }
    renderer.setGraph(graph);
    stopAnimation.current = animateNodes(graph, targets, { duration: 350, easing: "quadraticInOut" }, () => {
      stopAnimation.current = null;
      relabel.current(true);
      void camera.animatedReset({ duration: 250 }).then(() => relabel.current(true));
    });
  }, [graph, layout, root, reduced]);

  // What the reducers read about the selection and the edge labels.
  useEffect(() => {
    Object.assign(view.current, { selected, highlight, selectedEdge, allEdgeLabels });
    relabel.current();
  }, [selected, highlight, selectedEdge, allEdgeLabels]);

  // A selection made beside the canvas (a list, the keyboard) is brought into view; a click on the canvas is not.
  useEffect(() => {
    const renderer = sigma.current;
    if (!renderer || !selected) return;
    if (tapped.current) {
      tapped.current = false;
      return;
    }
    const shown = renderer.getNodeDisplayData(selected);
    if (!shown) return;
    const at = renderer.framedGraphToViewport(shown);
    const { width, height } = renderer.getDimensions();
    if (at.x < 0 || at.y < 0 || at.x > width || at.y > height) void renderer.getCamera().animate({ x: shown.x, y: shown.y }, { duration: reduced ? 0 : 300 });
  }, [selected, reduced]);

  useEffect(() => {
    const renderer = sigma.current;
    if (!renderer || !centre) return;
    const shown = renderer.getNodeDisplayData(centre.id);
    if (shown) void renderer.getCamera().animate({ x: shown.x, y: shown.y, ratio: Math.min(renderer.getCamera().ratio, 0.6) }, { duration: reduced ? 0 : 400 });
  }, [centre, reduced]);

  const zoom = (how: "in" | "out" | "fit") => {
    const camera = sigma.current?.getCamera();
    if (!camera) return;
    const duration = reduced ? 0 : 200;
    if (how === "in") void camera.animatedZoom({ duration });
    else if (how === "out") void camera.animatedUnzoom({ duration });
    else void camera.animatedReset({ duration });
  };

  const stepTo = (delta: 1 | -1) => {
    if (order.length === 0) return;
    const index = selected ? order.indexOf(selected) : -1;
    const next = index < 0 ? (delta === 1 ? 0 : order.length - 1) : (index + delta + order.length) % order.length;
    onSelect(order[next] ?? null);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget || event.altKey || event.ctrlKey || event.metaKey) return;
    const actions: Record<string, () => void> = {
      ArrowRight: () => stepTo(1),
      ArrowDown: () => stepTo(1),
      ArrowLeft: () => stepTo(-1),
      ArrowUp: () => stepTo(-1),
      Enter: () => {
        if (selected) onActivate(selected);
      },
      Escape: () => {
        onSelect(null);
        onSelectEdge?.(null);
      },
      "+": () => zoom("in"),
      "=": () => zoom("in"),
      "-": () => zoom("out"),
      "0": () => zoom("fit"),
      ...keys,
    };
    const action = actions[event.key];
    if (!action) return;
    event.preventDefault();
    action();
  };

  const attributes = Object.fromEntries(Object.entries(data ?? {}).map(([key, value]) => [`data-${key}`, value]));
  const ready = status === "ready";
  return (
    <div className="absolute inset-0 overflow-hidden">
      {status === "drawing" ? (
        <div className="absolute inset-0 flex items-center justify-center p-6" aria-hidden="true">
          <Skeleton className="h-full w-full rounded-sm opacity-60" />
        </div>
      ) : null}
      {status === "failed" ? (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 p-6 text-center text-sm text-muted-foreground" data-testid="kg-canvas-failed">
          <MonitorX className="size-5" aria-hidden="true" />
          <p className="max-w-sm">{t("noWebgl")}</p>
        </div>
      ) : null}
      <div
        ref={box}
        role="application"
        aria-roledescription={t("canvasRole")}
        aria-label={label}
        aria-describedby={describedBy}
        tabIndex={0}
        onKeyDown={onKeyDown}
        className="absolute inset-0 cursor-grab outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset active:cursor-grabbing"
        data-testid={testId}
        data-ready={ready ? "true" : "false"}
        data-selected={selected ?? ""}
        {...attributes}
      />
      <ZoomControls disabled={!ready} onZoom={zoom} />
    </div>
  );
}
