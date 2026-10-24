"use client";

import type cytoscape from "cytoscape";
import { Maximize, Minus, Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useMediaQuery } from "@/hooks/use-media-query";
import { type ExplorerElement, type Scope, stepOrder } from "@/lib/kg/explorer";
import { parseMoreId } from "@/lib/kg/explorer-working";

import { type ExplorerLayout, fit, inView, layoutOptions, light, sync } from "./explorer-layout";

import { explorerStylesheet } from "./explorer-style";
import { readTheme } from "./graph-theme";

/**
 * The explorer's canvas: Cytoscape.js with the fcose layout, both loaded only in the browser once the canvas mounts.
 * One instance per scope (the explorer keys this component by scope); inside a scope the elements change in place, so
 * nodes the reader expands appear beside the node they came from and the rest keep their places.
 *
 * Pointer: a click selects, a double click activates (opens a cluster, expands a node, shows a group's other
 * neighbours), the wheel zooms, a drag on the background pans and a drag on a node moves it. Hovering a node lights
 * its neighbourhood and names it in a tooltip. Keyboard, on the focused canvas: arrows step through the nodes (largest
 * or most linked first), Enter activates, Backspace goes up a scope, Delete hides a node where hiding applies, Escape
 * clears the selection, + and - zoom, 0 fits. A canvas says nothing to a screen reader: the rail and the inspector
 * list the same clusters, nodes and relations, and follow the same selection.
 */
let registered = false;

type Tip = { id: string; x: number; y: number };

export function ExplorerCanvas({
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
}: {
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
}) {
  const t = useTranslations("kg.explorer");
  const container = useRef<HTMLDivElement>(null);
  const [cy, setCy] = useState<cytoscape.Core | null>(null);
  const [hover, setHover] = useState<Tip | null>(null);
  const [laidOut, setLaidOut] = useState(false);
  const { resolvedTheme } = useTheme();
  const reduced = useMediaQuery("(prefers-reduced-motion: reduce)");
  const handlers = useRef({ onSelect, onActivate });
  const latest = useRef({ layout, root, reduced });
  const tapped = useRef(false);
  const applied = useRef<ExplorerLayout | null>(null);
  const order = useMemo(() => stepOrder(elements), [elements]);
  const kind = scope.kind;

  useEffect(() => {
    handlers.current = { onSelect, onActivate };
    latest.current = { layout, root, reduced };
  }, [onSelect, onActivate, layout, root, reduced]);

  useEffect(() => {
    let cancelled = false;
    let instance: cytoscape.Core | null = null;
    void Promise.all([import("cytoscape"), import("cytoscape-fcose")]).then(([{ default: create }, { default: fcose }]) => {
      if (cancelled || !container.current) return;
      if (!registered) {
        create.use(fcose);
        registered = true;
      }
      instance = create({
        container: container.current,
        style: explorerStylesheet(readTheme(), kind),
        minZoom: 0.05,
        maxZoom: 4,
        boxSelectionEnabled: false,
        autounselectify: true,
        textureOnViewport: false,
        hideEdgesOnViewport: false,
      });
      const at = (node: cytoscape.NodeSingular): Tip => {
        const position = node.renderedPosition();
        return { id: node.id(), x: position.x, y: position.y - node.renderedOuterHeight() / 2 };
      };
      instance.on("tap", "node", (event) => {
        const id = String(event.target.id());
        if (parseMoreId(id)) {
          handlers.current.onActivate(id);
          return;
        }
        tapped.current = true;
        handlers.current.onSelect(id);
      });
      instance.on("tap", (event) => {
        if (event.target === instance) handlers.current.onSelect(null);
      });
      instance.on("dbltap", "node", (event) => {
        const id = String(event.target.id());
        if (!parseMoreId(id)) handlers.current.onActivate(id);
      });
      instance.on("mouseover", "node", (event) => setHover(at(event.target)));
      instance.on("mouseout", "node", () => setHover(null));
      instance.on("viewport drag", () => setHover(null));
      setCy(instance);
    });
    return () => {
      cancelled = true;
      instance?.destroy();
      setCy(null);
    };
  }, [kind]);

  useEffect(() => {
    if (!cy) return;
    const frame = requestAnimationFrame(() => cy.style(explorerStylesheet(readTheme(), kind)));
    return () => cancelAnimationFrame(frame);
  }, [cy, kind, resolvedTheme]);

  // The inspector opening or closing, the window or full screen resize the canvas: keep what was in its middle there.
  useEffect(() => {
    const div = container.current;
    if (!cy || !div || typeof ResizeObserver === "undefined") return;
    let size = { width: div.clientWidth, height: div.clientHeight };
    const observer = new ResizeObserver(() => {
      const next = { width: div.clientWidth, height: div.clientHeight };
      if (next.width === size.width && next.height === size.height) return;
      cy.resize();
      cy.panBy({ x: (next.width - size.width) / 2, y: (next.height - size.height) / 2 });
      size = next;
    });
    observer.observe(div);
    return () => observer.disconnect();
  }, [cy]);

  // The elements change in place; a first drawing, or nodes added, run the layout.
  useEffect(() => {
    if (!cy) return;
    const first = cy.nodes().empty();
    const added = sync(cy, elements);
    if (cy.nodes().empty() || (!first && added.empty())) return;
    const { layout: name, root: start, reduced: still } = latest.current;
    const run = cy.elements().layout(
      layoutOptions(kind, name, cy, { incremental: !first, animate: !first && !still, root: start }),
    );
    run.one("layoutstop", () => {
      if (first) {
        fit(cy, false);
        setLaidOut(true);
      } else if (added.toArray().some((node) => !inView(cy, node.id()))) {
        fit(cy, !still);
      }
    });
    applied.current = name;
    run.run();
  }, [cy, elements, kind]);

  // Another layout picked: lay the whole view out again.
  useEffect(() => {
    if (!cy || cy.nodes().empty() || applied.current === layout) return;
    applied.current = layout;
    const run = cy.elements().layout(layoutOptions(kind, layout, cy, { incremental: false, animate: !reduced, root }));
    run.one("layoutstop", () => fit(cy, !reduced));
    run.run();
  }, [cy, layout, kind, reduced, root]);

  useEffect(() => {
    if (!cy) return;
    light(cy, selected, hover?.id ?? highlight ?? selected);
  }, [cy, selected, hover, highlight, elements]);

  useEffect(() => {
    if (!cy || !selected) return;
    if (!tapped.current && !inView(cy, selected)) cy.animate({ center: { eles: cy.getElementById(selected) } }, { duration: reduced ? 0 : 250 });
    tapped.current = false;
  }, [cy, selected, reduced]);

  const zoom = (factor: number) => {
    if (!cy) return;
    cy.zoom({ level: cy.zoom() * factor, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } });
  };

  const stepTo = (delta: 1 | -1) => {
    if (order.length === 0) return;
    const index = selected ? order.indexOf(selected) : -1;
    const next = index < 0 ? (delta === 1 ? 0 : order.length - 1) : (index + delta + order.length) % order.length;
    onSelect(order[next] ?? null);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const keys: Record<string, () => void> = {
      ArrowRight: () => stepTo(1),
      ArrowDown: () => stepTo(1),
      ArrowLeft: () => stepTo(-1),
      ArrowUp: () => stepTo(-1),
      Enter: () => {
        if (selected) onActivate(selected);
      },
      Backspace: onBack,
      Delete: () => {
        if (selected && onHide) onHide(selected);
      },
      Escape: () => onSelect(null),
      "+": () => zoom(1.25),
      "=": () => zoom(1.25),
      "-": () => zoom(0.8),
      "0": () => {
        if (cy) fit(cy, !reduced);
      },
    };
    const action = keys[event.key];
    if (!action) return;
    event.preventDefault();
    action();
  };

  const tip = hover && cy ? cy.getElementById(hover.id) : null;
  const tipData = tip?.nonempty() ? tip.data() : null;

  return (
    <div className="relative h-full min-h-0 w-full overflow-hidden">
      {!cy || !laidOut ? (
        <div className="absolute inset-0 flex items-center justify-center p-6" aria-hidden="true">
          <Skeleton className="h-full w-full rounded-sm opacity-60" />
        </div>
      ) : null}
      <div
        ref={container}
        role="application"
        aria-roledescription={t("canvasRole")}
        aria-label={label}
        aria-describedby={describedBy}
        tabIndex={0}
        onKeyDown={onKeyDown}
        className="h-full w-full cursor-grab outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset active:cursor-grabbing"
        data-testid="kg-explorer-canvas"
        data-ready={cy && laidOut ? "true" : "false"}
        data-scope={kind}
        data-selected={selected ?? ""}
        data-nodes={elements.filter((element) => element.group === "nodes").length}
      />
      {tipData ? (
        <div
          role="tooltip"
          className="pointer-events-none absolute z-10 max-w-64 -translate-x-1/2 -translate-y-full rounded-sm border bg-popover px-2.5 py-1.5 text-xs shadow-popover"
          style={{ left: hover!.x, top: hover!.y - 8 }}
          data-testid="kg-explorer-tip"
        >
          <p className="font-medium [overflow-wrap:anywhere] text-foreground">{String(tipData.name ?? tipData.label ?? hover!.id)}</p>
          <p className="text-muted-foreground">
            {tip!.hasClass("cluster") || tip!.hasClass("part")
              ? t("tipCluster", { count: Number(tipData.size ?? 0) })
              : tip!.hasClass("more")
                ? t("tipMore", { rel: String(tipData.rel ?? "") })
                : t("tipNode", { kind: String(tipData.kind ?? ""), count: Number(tipData.degree ?? 0) })}
          </p>
        </div>
      ) : null}
      <div className="absolute right-3 bottom-3 flex flex-col gap-px overflow-hidden rounded-sm border bg-card shadow-raised">
        <Button type="button" variant="ghost" size="icon-sm" className="cursor-pointer rounded-none max-md:size-11" aria-label={t("zoomIn")} title={t("zoomIn")} onClick={() => zoom(1.25)} disabled={!cy}>
          <Plus aria-hidden="true" />
        </Button>
        <Button type="button" variant="ghost" size="icon-sm" className="cursor-pointer rounded-none max-md:size-11" aria-label={t("zoomOut")} title={t("zoomOut")} onClick={() => zoom(0.8)} disabled={!cy}>
          <Minus aria-hidden="true" />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          className="cursor-pointer rounded-none max-md:size-11"
          aria-label={t("fit")}
          title={t("fit")}
          onClick={() => {
            if (cy) fit(cy, !reduced);
          }}
          disabled={!cy}
          data-testid="kg-explorer-fit"
        >
          <Maximize aria-hidden="true" />
        </Button>
      </div>
    </div>
  );
}
