"use client";

import type cytoscape from "cytoscape";
import { Maximize, Minus, Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { displayName, kindStyle, orderNodes, shortLabel, step } from "@/lib/kg/graph";
import { MAX_HOPS, type Neighbourhood } from "@/lib/kg/types";

import { readTheme, type Theme } from "./graph-theme";

/**
 * The neighbourhood drawn with Cytoscape.js on a canvas, loaded only in the browser. A canvas says nothing to a
 * screen reader: the neighbour table beside it carries the same data and the same selection, and this view only
 * mirrors it. Keyboard: arrows step through the nodes (the table's order), Home goes back to the node shown, Enter
 * opens the selected node, + and - zoom, 0 fits the graph. Colours come from the design tokens and follow the theme.
 * The node's groups of direct neighbours (edge type, direction, kind) that hold more than the view draws end in one
 * dashed node each, "+N kind", as the table's "show more" does: choosing it opens the group. The container is sized,
 * not positioned: Cytoscape gives it `position: relative` in a style of its own, outside the
 * cascade layers, which wins over any utility class.
 */
export type LayoutName = "concentric" | "cose" | "breadthfirst";
export const LAYOUTS: readonly LayoutName[] = ["concentric", "cose", "breadthfirst"];

/** The id of the node that stands for what a group of direct neighbours holds beyond those drawn. */
const MORE = "more\u0000";

function moreElements(view: Neighbourhood): cytoscape.ElementDefinition[] {
  return view.groups.flatMap((group) => {
    const hidden = group.total - group.shown.length;
    if (hidden <= 0) return [];
    const id = `${MORE}${group.key}`;
    const ends = group.direction === "out" ? { source: view.focus, target: id } : { source: id, target: view.focus };
    return [
      {
        group: "nodes",
        data: { id, label: `+${hidden} ${group.kind}`, kind: group.kind, hop: 1, tone: 5, shape: "round-rectangle" },
        classes: ["more"],
      },
      { group: "edges", data: { id: `${id}\u0000edge`, ...ends, rel: group.rel }, classes: ["more"] },
    ];
  });
}

function elements(view: Neighbourhood): cytoscape.ElementDefinition[] {
  const nodes: cytoscape.ElementDefinition[] = view.nodes.map((node) => {
    const { tone, shape } = kindStyle(node.kind);
    return {
      group: "nodes",
      data: { id: node.id, label: shortLabel(displayName(node)), kind: node.kind, hop: node.hop, tone, shape },
      classes: [node.id === view.focus ? "focus" : "", node.hub ? "hub" : ""].filter(Boolean),
    };
  });
  const edges: cytoscape.ElementDefinition[] = view.edges.map((edge) => ({
    group: "edges",
    data: { id: edge.id, source: edge.src, target: edge.dst, rel: edge.rel },
  }));
  return [...nodes, ...edges, ...moreElements(view)];
}

function stylesheet(theme: Theme): cytoscape.StylesheetJson {
  const tones = ([1, 2, 3, 4, 5] as const).map((tone) => ({
    selector: `node[tone = ${tone}]`,
    style: { "background-color": theme.tones[tone] },
  }));
  return [
    {
      selector: "node",
      style: {
        shape: (node: cytoscape.NodeSingular) => node.data("shape") as cytoscape.Css.NodeShape,
        width: 16,
        height: 16,
        label: (node: cytoscape.NodeSingular) => String(node.data("label")),
        "font-family": theme.font,
        "font-size": 10,
        color: theme.foreground,
        "text-valign": "bottom",
        "text-halign": "center",
        "text-margin-y": 4,
        "text-background-color": theme.surface,
        "text-background-opacity": 0.85,
        "text-background-padding": "1px",
        "min-zoomed-font-size": 8,
        "border-width": 1,
        "border-color": theme.surface,
        "overlay-opacity": 0,
      },
    },
    ...tones,
    { selector: "node.hub", style: { "border-width": 2, "border-style": "dashed", "border-color": theme.muted } },
    {
      selector: "node.focus",
      style: { width: 26, height: 26, "border-width": 3, "border-color": theme.brand, "font-weight": 600 },
    },
    {
      selector: "node:selected",
      style: {
        "underlay-color": theme.ring,
        "underlay-opacity": 0.35,
        "underlay-padding": 5,
        "underlay-shape": "ellipse",
        "font-weight": 600,
      },
    },
    {
      selector: "edge",
      style: {
        width: 1.2,
        "curve-style": "bezier",
        "line-color": theme.muted,
        "line-opacity": 0.6,
        "target-arrow-shape": "triangle",
        "target-arrow-color": theme.muted,
        "arrow-scale": 0.7,
        "overlay-opacity": 0,
      },
    },
    {
      selector: "edge.near",
      style: {
        width: 2,
        "line-color": theme.brand,
        "target-arrow-color": theme.brand,
        "line-opacity": 1,
        label: (edge: cytoscape.EdgeSingular) => String(edge.data("rel")),
        "font-family": theme.font,
        "font-size": 9,
        color: theme.foreground,
        "text-rotation": "autorotate",
        "text-background-color": theme.surface,
        "text-background-opacity": 1,
        "text-background-padding": "1px",
        "min-zoomed-font-size": 8,
      },
    },
    {
      selector: "node.more",
      style: {
        width: 22,
        height: 14,
        "background-color": theme.surface,
        "border-width": 1.5,
        "border-style": "dashed",
        "border-color": theme.muted,
        color: theme.muted,
      },
    },
    { selector: "edge.more", style: { "line-style": "dashed" } },
    { selector: "node.faded", style: { opacity: 0.25, "text-opacity": 0 } },
    { selector: "edge.faded", style: { opacity: 0.15 } },
  ];
}

/** Fitting a small graph would blow it up; past this zoom the labels get larger than the page's own text. */
const FIT_ZOOM = 1.25;
const PADDING = 24;

/** Fit the graph in the view, without zooming in past FIT_ZOOM. */
function fitView(cy: cytoscape.Core): void {
  cy.fit(undefined, PADDING);
  if (cy.zoom() > FIT_ZOOM) {
    cy.zoom(FIT_ZOOM);
    cy.center();
  }
}

function layoutOptions(name: LayoutName, focus: string, cy: () => cytoscape.Core | null): cytoscape.LayoutOptions {
  const common = {
    animate: false,
    fit: false,
    padding: PADDING,
    stop: () => {
      const core = cy();
      if (core) fitView(core);
    },
  } as const;
  if (name === "cose") return { name: "cose", ...common, randomize: false, nodeRepulsion: () => 9000, idealEdgeLength: () => 70 };
  if (name === "breadthfirst") return { name: "breadthfirst", ...common, roots: [focus], directed: false, spacingFactor: 1.1 };
  return {
    name: "concentric",
    ...common,
    concentric: (node: cytoscape.NodeSingular) => MAX_HOPS + 1 - Number(node.data("hop")),
    levelWidth: () => 1,
    // Rings of a few nodes need room for their labels; a ring of a hundred is wide enough already.
    minNodeSpacing: (cy()?.nodes().length ?? 0) <= 30 ? 64 : 14,
    startAngle: (3 / 2) * Math.PI,
  };
}

/** Fade what is not the selected node, its edges and its neighbours; give those edges their type as a label. */
function highlight(cy: cytoscape.Core, selected: string): void {
  cy.batch(() => {
    cy.elements().removeClass("faded near");
    cy.elements(":selected").unselect();
    const node = cy.getElementById(selected);
    if (node.empty()) return;
    node.select();
    const near = node.closedNeighborhood();
    node.connectedEdges().addClass("near");
    cy.elements().not(near).addClass("faded");
  });
}

function inView(cy: cytoscape.Core, id: string): boolean {
  const node = cy.getElementById(id);
  if (node.empty()) return true;
  const { x, y } = node.position();
  const box = cy.extent();
  return x >= box.x1 && x <= box.x2 && y >= box.y1 && y <= box.y2;
}

export function GraphCanvas({
  view,
  selected,
  layout,
  label,
  describedBy,
  onSelect,
  onOpen,
  onOpenGroup,
}: {
  view: Neighbourhood;
  selected: string;
  layout: LayoutName;
  label: string;
  describedBy: string;
  onSelect: (id: string) => void;
  onOpen: (id: string) => void;
  /** Show the whole of a group of the node's direct neighbours, by its key. */
  onOpenGroup: (key: string) => void;
}) {
  const t = useTranslations("kg.graph");
  const container = useRef<HTMLDivElement>(null);
  const [cy, setCy] = useState<cytoscape.Core | null>(null);
  const { resolvedTheme } = useTheme();
  const ordered = useMemo(() => orderNodes(view.nodes), [view.nodes]);
  const handlers = useRef({ onSelect, onOpen, onOpenGroup });
  const tapped = useRef(false); // the last selection came from the canvas: do not move the view for it
  const latest = useRef({ layout, selected });
  const applied = useRef<{ cy: cytoscape.Core | null; layout: LayoutName | null }>({ cy: null, layout: null });

  useEffect(() => {
    handlers.current = { onSelect, onOpen, onOpenGroup };
    latest.current = { layout, selected };
  }, [onSelect, onOpen, onOpenGroup, layout, selected]);

  useEffect(() => {
    let cancelled = false;
    let instance: cytoscape.Core | null = null;
    void import("cytoscape").then(({ default: create }) => {
      if (cancelled || !container.current) return;
      instance = create({
        container: container.current,
        elements: elements(view),
        style: stylesheet(readTheme()),
        layout: { name: "preset" }, // the chosen layout runs below, once the handlers are on
        minZoom: 0.15,
        maxZoom: 3,
        boxSelectionEnabled: false,
        selectionType: "single",
      });
      instance.on("tap", "node", (event) => {
        const id = String(event.target.id());
        if (id.startsWith(MORE)) {
          handlers.current.onOpenGroup(id.slice(MORE.length));
          return;
        }
        tapped.current = true;
        handlers.current.onSelect(id);
      });
      instance.on("dbltap", "node", (event) => {
        const id = String(event.target.id());
        if (!id.startsWith(MORE)) handlers.current.onOpen(id);
      });
      const created = instance;
      created.layout(layoutOptions(latest.current.layout, view.focus, () => created)).run();
      highlight(created, latest.current.selected);
      applied.current = { cy: created, layout: latest.current.layout };
      setCy(instance);
    });
    return () => {
      cancelled = true;
      instance?.destroy();
      setCy(null);
    };
  }, [view]);

  useEffect(() => {
    if (!cy) return;
    const frame = requestAnimationFrame(() => cy.style(stylesheet(readTheme())));
    return () => cancelAnimationFrame(frame);
  }, [cy, resolvedTheme]);

  useEffect(() => {
    if (!cy || (applied.current.cy === cy && applied.current.layout === layout)) return;
    cy.layout(layoutOptions(layout, view.focus, () => cy)).run();
    applied.current = { cy, layout };
  }, [cy, layout, view.focus]);

  useEffect(() => {
    if (!cy) return;
    highlight(cy, selected);
    if (!tapped.current && !inView(cy, selected)) cy.center(cy.getElementById(selected));
    tapped.current = false;
  }, [cy, selected]);

  const zoom = (factor: number) => {
    if (!cy) return;
    const { width, height } = { width: cy.width(), height: cy.height() };
    cy.zoom({ level: cy.zoom() * factor, renderedPosition: { x: width / 2, y: height / 2 } });
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const keys: Record<string, () => void> = {
      ArrowRight: () => onSelect(step(ordered, selected, 1)),
      ArrowDown: () => onSelect(step(ordered, selected, 1)),
      ArrowLeft: () => onSelect(step(ordered, selected, -1)),
      ArrowUp: () => onSelect(step(ordered, selected, -1)),
      Home: () => onSelect(view.focus),
      Enter: () => onOpen(selected),
      "+": () => zoom(1.25),
      "=": () => zoom(1.25),
      "-": () => zoom(0.8),
      "0": () => {
        if (cy) fitView(cy);
      },
    };
    const action = keys[event.key];
    if (!action || event.altKey || event.ctrlKey || event.metaKey) return;
    event.preventDefault();
    action();
  };

  return (
    <div className="relative h-[28rem] overflow-hidden rounded-md border bg-background xl:h-[32rem]">
      {!cy ? (
        <div className="absolute inset-0 p-4" aria-hidden="true">
          <Skeleton className="h-full w-full rounded-sm" />
        </div>
      ) : null}
      <div
        ref={container}
        role="application"
        aria-label={label}
        aria-describedby={describedBy}
        tabIndex={0}
        onKeyDown={onKeyDown}
        className="h-full w-full cursor-grab rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
        data-testid="kg-canvas"
        data-ready={cy ? "true" : "false"}
        data-selected={selected}
      />
      <div className="absolute top-2 right-2 flex gap-1 rounded-md border bg-card/95 p-0.5 shadow-raised">
        <Button type="button" variant="ghost" size="icon-sm" className="cursor-pointer" aria-label={t("zoomIn")} title={t("zoomIn")} onClick={() => zoom(1.25)} disabled={!cy}>
          <Plus aria-hidden="true" />
        </Button>
        <Button type="button" variant="ghost" size="icon-sm" className="cursor-pointer" aria-label={t("zoomOut")} title={t("zoomOut")} onClick={() => zoom(0.8)} disabled={!cy}>
          <Minus aria-hidden="true" />
        </Button>
        <Button type="button" variant="ghost" size="icon-sm" className="cursor-pointer" aria-label={t("fit")} title={t("fit")} onClick={() => {
            if (cy) fitView(cy);
          }} disabled={!cy}>
          <Maximize aria-hidden="true" />
        </Button>
      </div>
    </div>
  );
}
