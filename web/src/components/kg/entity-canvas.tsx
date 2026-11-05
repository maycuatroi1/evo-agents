"use client";

import forceAtlas2 from "graphology-layout-forceatlas2";
import { Maximize, Minus, Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { type KeyboardEvent, useEffect, useRef } from "react";
import Sigma from "sigma";
import type { NodeHoverDrawingFunction } from "sigma/rendering";
import type { EdgeDisplayData, NodeDisplayData } from "sigma/types";

import { Button } from "@/components/ui/button";
import { buildGraph, type EntityEdgeAttributes, type EntityNodeAttributes, type WorkingSet } from "@/lib/kg/entity-graph";

import { readTheme, type Theme } from "./graph-theme";

/**
 * The Entities view's canvas: sigma.js 3 (WebGL) over a graphology graph built by lib/kg/entity-graph.ts, laid out by
 * ForceAtlas2. Loaded with next/dynamic and no server rendering, since sigma needs a window and WebGL. One sigma per
 * mount; a new working set is a new graph laid out from the places the last one had, so an opened neighbourhood grows
 * beside the node it came from.
 *
 * Pointer: a click selects a node or a relation, a double click opens a node's neighbours, the wheel zooms and a drag
 * pans. Pointing at a node lights it and its neighbours and names their relations; pointing at a relation names it.
 * Keyboard, on the focused canvas: + and - zoom, 0 fits, Escape clears the selection. The rail and the inspector list
 * the same entities and relations for a screen reader.
 */
type Graph = ReturnType<typeof buildGraph>;

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
};

/** ForceAtlas2 rounds: more for a small graph, fewer once places come from the graph before. */
function iterations(order: number, settled: boolean): number {
  if (settled) return 40;
  return order > 600 ? 120 : order > 200 ? 200 : 300;
}

function hoverDrawer(theme: Theme): NodeHoverDrawingFunction<EntityNodeAttributes, EntityEdgeAttributes> {
  return (context, data, settings) => {
    const size = settings.labelSize;
    context.font = `${settings.labelWeight} ${size}px ${settings.labelFont}`;
    const label = data.label ?? "";
    const width = label ? context.measureText(label).width + 10 : 0;
    const height = size + 8;
    context.fillStyle = theme.surface;
    context.strokeStyle = theme.border;
    context.lineWidth = 1;
    context.beginPath();
    if (label) context.roundRect(data.x + data.size + 2, data.y - height / 2, width, height, 4);
    context.fill();
    if (label) context.stroke();
    context.beginPath();
    context.arc(data.x, data.y, data.size + 3, 0, Math.PI * 2);
    context.fillStyle = theme.surface;
    context.fill();
    context.strokeStyle = data.color;
    context.lineWidth = 2;
    context.stroke();
    if (label) {
      context.fillStyle = theme.foreground;
      context.fillText(label, data.x + data.size + 7, data.y + size / 3);
    }
  };
}

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
}: EntityCanvasProps) {
  const t = useTranslations("kg.explorer");
  const { resolvedTheme } = useTheme();
  const box = useRef<HTMLDivElement>(null);
  const sigma = useRef<Sigma<EntityNodeAttributes, EntityEdgeAttributes> | null>(null);
  const graph = useRef<Graph | null>(null);
  // What the reducers read: kept in refs so pointing at a node refreshes the canvas without a React render.
  const view = useRef({ hoveredNode: null as string | null, hoveredEdge: null as string | null, selectedNode, selectedEdge, allEdgeLabels, theme: null as Theme | null });
  const handlers = useRef({ onSelectNode, onSelectEdge, onExpand });
  useEffect(() => {
    handlers.current = { onSelectNode, onSelectEdge, onExpand };
  }, [onSelectNode, onSelectEdge, onExpand]);

  // One sigma for the life of the canvas.
  useEffect(() => {
    const container = box.current;
    if (!container) return;
    const theme = readTheme();
    view.current.theme = theme;
    const renderer = new Sigma<EntityNodeAttributes, EntityEdgeAttributes>(buildGraph({ nodes: [], edges: [] }, { dark: false, edgeColour: theme.border }), container, {
      renderEdgeLabels: true,
      enableEdgeEvents: true,
      hideEdgesOnMove: false,
      labelDensity: 0.5,
      labelGridCellSize: 90,
      labelRenderedSizeThreshold: 7,
      labelSize: 12,
      labelWeight: "500",
      edgeLabelSize: 11,
      zIndex: true,
      minCameraRatio: 0.05,
      maxCameraRatio: 8,
      allowInvalidContainer: true,
      nodeReducer: (node, data) => {
        const state = view.current;
        const focus = state.hoveredNode ?? state.selectedNode;
        const out: Partial<NodeDisplayData> = { ...data };
        if (node === state.selectedNode || node === state.hoveredNode) {
          out.highlighted = true;
          out.zIndex = 2;
          out.forceLabel = true;
        }
        const g = graph.current;
        if (focus && g && g.hasNode(focus) && node !== focus && !g.areNeighbors(focus, node)) {
          out.color = state.theme?.border ?? data.color;
          out.label = "";
          out.zIndex = 0;
        } else if (focus && g && node !== focus) {
          out.forceLabel = true;
          out.zIndex = 1;
        }
        if (state.selectedEdge && g && g.hasEdge(state.selectedEdge) && g.hasExtremity(state.selectedEdge, node)) {
          out.forceLabel = true;
          out.highlighted = true;
        }
        return out;
      },
      edgeReducer: (edge, data) => {
        const state = view.current;
        const g = graph.current;
        const focus = state.hoveredNode ?? state.selectedNode;
        const touches = Boolean(focus && g && g.hasNode(focus) && g.hasExtremity(edge, focus));
        const pointed = edge === state.hoveredEdge || edge === state.selectedEdge;
        const out: Partial<EdgeDisplayData> = { ...data };
        if (pointed || touches) {
          out.color = state.theme?.foreground ?? data.color;
          out.size = data.size + (pointed ? 1.5 : 0.5);
          out.zIndex = 1;
        } else if (focus && g && g.hasNode(focus)) {
          out.hidden = true;
        }
        out.label = pointed || touches || state.allEdgeLabels ? data.label : "";
        return out;
      },
    });
    renderer.on("enterNode", ({ node }) => {
      view.current.hoveredNode = node;
      container.style.cursor = "pointer";
      renderer.refresh({ skipIndexation: true });
    });
    renderer.on("leaveNode", () => {
      view.current.hoveredNode = null;
      container.style.cursor = "";
      renderer.refresh({ skipIndexation: true });
    });
    renderer.on("enterEdge", ({ edge }) => {
      view.current.hoveredEdge = edge;
      container.style.cursor = "pointer";
      renderer.refresh({ skipIndexation: true });
    });
    renderer.on("leaveEdge", () => {
      view.current.hoveredEdge = null;
      container.style.cursor = "";
      renderer.refresh({ skipIndexation: true });
    });
    renderer.on("clickNode", ({ node }) => handlers.current.onSelectNode(node));
    renderer.on("clickEdge", ({ edge }) => handlers.current.onSelectEdge(edge));
    renderer.on("clickStage", () => handlers.current.onSelectNode(null));
    renderer.on("doubleClickNode", (payload) => {
      payload.preventSigmaDefault();
      handlers.current.onExpand(payload.node);
    });
    sigma.current = renderer;
    return () => {
      renderer.kill();
      sigma.current = null;
    };
  }, []);

  // A new working set or theme: a new graph from the places of the last one, laid out, then drawn.
  useEffect(() => {
    const renderer = sigma.current;
    if (!renderer) return;
    const theme = readTheme();
    view.current.theme = theme;
    const previous = new Map<string, { x: number; y: number }>();
    graph.current?.forEachNode((node, attributes) => previous.set(node, { x: attributes.x, y: attributes.y }));
    const next = buildGraph(set, { dark: resolvedTheme === "dark", edgeColour: theme.border, previous });
    const settled = next.order > 0 && next.everyNode((node) => previous.has(node));
    if (next.order > 1) {
      const fresh = next.filterNodes((node) => !previous.has(node)).length;
      forceAtlas2.assign(next, {
        iterations: iterations(next.order, settled || fresh < next.order / 4),
        settings: { ...forceAtlas2.inferSettings(next), barnesHutOptimize: next.order > 300 },
        getEdgeWeight: "size",
      });
    }
    renderer.setSettings({
      labelFont: theme.font,
      edgeLabelFont: theme.font,
      labelColor: { color: theme.foreground },
      edgeLabelColor: { color: theme.muted },
      defaultDrawNodeHover: hoverDrawer(theme),
    });
    graph.current = next;
    renderer.setGraph(next);
    if (!settled) void renderer.getCamera().animatedReset({ duration: 300 });
  }, [set, resolvedTheme]);

  // Selection and the edge labels switch: what the reducers read.
  useEffect(() => {
    Object.assign(view.current, { selectedNode, selectedEdge, allEdgeLabels });
    sigma.current?.refresh({ skipIndexation: true });
  }, [selectedNode, selectedEdge, allEdgeLabels]);

  useEffect(() => {
    const renderer = sigma.current;
    if (!renderer || !centre) return;
    const shown = renderer.getNodeDisplayData(centre.id);
    if (shown) void renderer.getCamera().animate({ x: shown.x, y: shown.y, ratio: Math.min(renderer.getCamera().ratio, 0.6) }, { duration: 400 });
  }, [centre]);

  const zoom = (how: "in" | "out" | "fit") => {
    const camera = sigma.current?.getCamera();
    if (!camera) return;
    if (how === "in") void camera.animatedZoom({ duration: 200 });
    else if (how === "out") void camera.animatedUnzoom({ duration: 200 });
    else void camera.animatedReset({ duration: 300 });
  };
  const onKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    const action = { "+": "in", "=": "in", "-": "out", "0": "fit" }[event.key] as "in" | "out" | "fit" | undefined;
    if (action) zoom(action);
    else if (event.key === "Escape") handlers.current.onSelectNode(null);
    else return;
    event.preventDefault();
  };

  return (
    <div className="absolute inset-0">
      <div
        ref={box}
        className="absolute inset-0 rounded-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none focus-visible:ring-inset"
        role="application"
        aria-roledescription={t("canvasRole")}
        aria-label={label}
        aria-describedby={describedBy}
        tabIndex={0}
        onKeyDown={onKey}
        data-testid="kg-entity-canvas"
        data-nodes={set.nodes.length}
        data-edges={set.edges.length}
      />
      <div className="absolute top-3 right-3 flex flex-col overflow-hidden rounded-md border bg-card shadow-raised">
        {(
          [
            ["in", Plus, t("zoomIn")],
            ["out", Minus, t("zoomOut")],
            ["fit", Maximize, t("fit")],
          ] as const
        ).map(([how, Icon, text]) => (
          <Button
            key={how}
            type="button"
            variant="ghost"
            size="icon-sm"
            className="cursor-pointer rounded-none max-md:size-11 [&:not(:last-child)]:border-b"
            aria-label={text}
            title={text}
            onClick={() => zoom(how)}
          >
            <Icon aria-hidden="true" />
          </Button>
        ))}
      </div>
    </div>
  );
}
