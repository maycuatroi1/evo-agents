"use client";

import forceAtlas2 from "graphology-layout-forceatlas2";
import { Maximize, Minus, Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { type KeyboardEvent, useEffect, useRef } from "react";
import Sigma from "sigma";
import { NodeCircleProgram, type NodeHoverDrawingFunction, type NodeLabelDrawingFunction } from "sigma/rendering";
import type { EdgeDisplayData, NodeDisplayData } from "sigma/types";

import { Button } from "@/components/ui/button";
import { buildGraph, type EntityEdgeAttributes, type EntityNodeAttributes, isCodeNode, type WorkingSet } from "@/lib/kg/entity-graph";

import { readTheme, type Theme } from "./graph-theme";
import NodeSquareProgram from "./node-square";

/**
 * The Entities view's canvas: sigma.js 3 (WebGL) over a graphology graph built by lib/kg/entity-graph.ts, laid out by
 * ForceAtlas2. Loaded with next/dynamic and no server rendering, since sigma needs a window and WebGL. One sigma per
 * mount; a new working set is a new graph laid out from the places the last one had, so an opened neighbourhood grows
 * beside the node it came from.
 *
 * Pointer: a click selects a node or a relation, a double click opens a node's neighbours, the wheel zooms and a drag
 * pans. Pointing at a node lights it and its neighbours and names their relations; pointing at a relation names it.
 * With code drawn, a Directory, File or Symbol is a slate rounded square; a click on one opens its node page. An
 * entity with code links carries their count after its name.
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
  /** A code node clicked: open its page of the graph. */
  onOpenCode: (id: string) => void;
};

/** ForceAtlas2 rounds: more for a small graph, fewer once places come from the graph before. */
function iterations(order: number, settled: boolean): number {
  if (settled) return 40;
  return order > 600 ? 120 : order > 200 ? 200 : 300;
}

/** The custom attributes sigma hands the label and hover drawers, beside its display data. */
type Drawn = { code?: number; type?: string };

/** "</> n": the code links of an entity, drawn after its name. */
function codeBadge(code: number | undefined): string {
  return code ? `</> ${code}` : "";
}

/** The width of a name and its code badge in the current font, and where the badge starts. */
function measure(context: CanvasRenderingContext2D, label: string, badge: string, small: number, settings: { labelWeight: string; labelFont: string }) {
  const name = label ? context.measureText(label).width : 0;
  if (!badge) return { name, badge: 0 };
  context.font = `600 ${small}px ${settings.labelFont}`;
  const width = context.measureText(badge).width + 8;
  return { name, badge: width };
}

function drawBadge(context: CanvasRenderingContext2D, theme: Theme, text: string, x: number, y: number, size: number, font: string) {
  const height = size + 4;
  context.font = `600 ${size}px ${font}`;
  const width = context.measureText(text).width + 8;
  context.beginPath();
  context.roundRect(x, y - height / 2, width, height, height / 2);
  context.fillStyle = theme.sunken;
  context.fill();
  context.strokeStyle = theme.border;
  context.lineWidth = 1;
  context.stroke();
  context.fillStyle = theme.muted;
  context.fillText(text, x + 4, y + size / 3);
}

/** A node's name beside it, and an entity's code links as a small pill after the name. */
function labelDrawer(theme: Theme): NodeLabelDrawingFunction<EntityNodeAttributes, EntityEdgeAttributes> {
  return (context, data, settings) => {
    if (!data.label) return;
    const size = settings.labelSize;
    const small = Math.max(size - 2, 9);
    context.font = `${settings.labelWeight} ${size}px ${settings.labelFont}`;
    context.fillStyle = settings.labelColor.color ?? theme.foreground;
    context.fillText(data.label, data.x + data.size + 3, data.y + size / 3);
    const badge = codeBadge((data as Drawn).code);
    if (!badge) return;
    const width = context.measureText(data.label).width;
    drawBadge(context, theme, badge, data.x + data.size + 3 + width + 4, data.y, small, settings.labelFont);
  };
}

function hoverDrawer(theme: Theme): NodeHoverDrawingFunction<EntityNodeAttributes, EntityEdgeAttributes> {
  return (context, data, settings) => {
    const size = settings.labelSize;
    const small = Math.max(size - 2, 9);
    context.font = `${settings.labelWeight} ${size}px ${settings.labelFont}`;
    const label = data.label ?? "";
    const badge = label ? codeBadge((data as Drawn).code) : "";
    const widths = measure(context, label, badge, small, settings);
    const width = label ? widths.name + 10 + (badge ? widths.badge + 4 : 0) : 0;
    const height = size + 8;
    const square = (data as Drawn).type === "square";
    context.fillStyle = theme.surface;
    context.strokeStyle = theme.border;
    context.lineWidth = 1;
    context.beginPath();
    if (label) context.roundRect(data.x + data.size + 2, data.y - height / 2, width, height, 4);
    context.fill();
    if (label) context.stroke();
    context.beginPath();
    if (square) {
      const side = (data.size + 3) * 1.7;
      context.roundRect(data.x - side / 2, data.y - side / 2, side, side, 3);
    } else {
      context.arc(data.x, data.y, data.size + 3, 0, Math.PI * 2);
    }
    context.fillStyle = theme.surface;
    context.fill();
    context.strokeStyle = data.color;
    context.lineWidth = 2;
    context.stroke();
    if (label) {
      context.font = `${settings.labelWeight} ${size}px ${settings.labelFont}`;
      context.fillStyle = theme.foreground;
      context.fillText(label, data.x + data.size + 7, data.y + size / 3);
      if (badge) drawBadge(context, theme, badge, data.x + data.size + 7 + widths.name + 4, data.y, small, settings.labelFont);
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
  onOpenCode,
}: EntityCanvasProps) {
  const t = useTranslations("kg.explorer");
  const { resolvedTheme } = useTheme();
  const box = useRef<HTMLDivElement>(null);
  const sigma = useRef<Sigma<EntityNodeAttributes, EntityEdgeAttributes> | null>(null);
  const graph = useRef<Graph | null>(null);
  // What the reducers read: kept in refs so pointing at a node refreshes the canvas without a React render.
  const view = useRef({ hoveredNode: null as string | null, hoveredEdge: null as string | null, selectedNode, selectedEdge, allEdgeLabels, theme: null as Theme | null });
  const handlers = useRef({ onSelectNode, onSelectEdge, onExpand, onOpenCode });
  useEffect(() => {
    handlers.current = { onSelectNode, onSelectEdge, onExpand, onOpenCode };
  }, [onSelectNode, onSelectEdge, onExpand, onOpenCode]);

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
      defaultNodeType: "circle",
      nodeProgramClasses: { circle: NodeCircleProgram, square: NodeSquareProgram },
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
        out.label = pointed || touches || (state.allEdgeLabels && !data.code) ? data.label : "";
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
    renderer.on("clickNode", ({ node }) => {
      const g = graph.current;
      if (g?.hasNode(node) && isCodeNode(g.getNodeAttributes(node))) handlers.current.onOpenCode(node);
      else handlers.current.onSelectNode(node);
    });
    renderer.on("clickEdge", ({ edge }) => handlers.current.onSelectEdge(edge));
    renderer.on("clickStage", () => handlers.current.onSelectNode(null));
    renderer.on("doubleClickNode", (payload) => {
      payload.preventSigmaDefault();
      const g = graph.current;
      if (g?.hasNode(payload.node) && isCodeNode(g.getNodeAttributes(payload.node))) return;
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
    const next = buildGraph(set, { dark: resolvedTheme === "dark", edgeColour: theme.border, codeEdgeColour: theme.subtle, previous });
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
      defaultDrawNodeLabel: labelDrawer(theme),
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
        data-code-nodes={set.codeNodes?.length ?? 0}
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
