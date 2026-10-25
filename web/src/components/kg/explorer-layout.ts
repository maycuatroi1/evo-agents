import type cytoscape from "cytoscape";

import type { ExplorerElement, Scope } from "@/lib/kg/explorer";

/**
 * What the explorer's canvas does to Cytoscape, apart from React: the layouts of each scope, fitting the view, making
 * the canvas hold the elements a scope draws (in place, so nodes keep their places), lighting what the reader looks
 * at, and choosing which labels show.
 */
export type ExplorerLayout = "force" | "radial" | "tree";

/** The layouts each scope offers: a tree needs a node to grow from. */
export const SCOPE_LAYOUTS: Record<Scope["kind"], readonly ExplorerLayout[]> = {
  overview: ["force", "radial"],
  cluster: ["force", "radial"],
  node: ["force", "radial", "tree"],
};

const FIT_PADDING = 40;
const MAX_FIT_ZOOM = 1.6;

export function fit(cy: cytoscape.Core, animate: boolean): void {
  const shown = cy.elements(":visible");
  if (shown.empty()) return;
  if (animate) {
    cy.animate({ fit: { eles: shown, padding: FIT_PADDING } }, { duration: 300 });
    return;
  }
  cy.fit(shown, FIT_PADDING);
  if (cy.zoom() > MAX_FIT_ZOOM) {
    cy.zoom(MAX_FIT_ZOOM);
    cy.center(shown);
  }
}

/** Distance of each node from `root` in steps, for the radial layout of a node's working set. */
function depths(cy: cytoscape.Core, root: string | null): Map<string, number> {
  const out = new Map<string, number>();
  const start = root ? cy.getElementById(root) : cy.collection();
  if (start.empty()) return out;
  cy.elements().breadthFirstSearch({
    roots: start,
    directed: false,
    visit: (node, _edge, _previous, _index, depth) => {
      out.set(node.id(), depth);
    },
  });
  return out;
}

export function layoutOptions(
  kind: Scope["kind"],
  layout: ExplorerLayout,
  cy: cytoscape.Core,
  { incremental, animate, root }: { incremental: boolean; animate: boolean; root: string | null },
): cytoscape.LayoutOptions {
  const common = { animate, animationDuration: 350, fit: false, padding: FIT_PADDING } as const;
  // Circles of the overview and of a cluster's parts stand by their size; the nodes of a node's view by their steps.
  const bySize = kind !== "node";
  if (layout === "radial") {
    const depth = depths(cy, root);
    const size = (node: cytoscape.NodeSingular) => Number(node.data("size") ?? node.data("degree") ?? 0);
    const largest = Math.max(1, ...cy.nodes().map(size));
    return {
      name: "concentric",
      ...common,
      concentric: (node: cytoscape.NodeSingular) => (bySize ? size(node) : 100 - (depth.get(node.id()) ?? 99)),
      levelWidth: () => (bySize ? largest / 4 : 1),
      minNodeSpacing: bySize ? 28 : 18,
      startAngle: (3 / 2) * Math.PI,
    } as cytoscape.LayoutOptions;
  }
  if (layout === "tree") {
    return { name: "breadthfirst", ...common, roots: root ? [root] : undefined, directed: false, spacingFactor: 1.15 };
  }
  // Labels count in each node's size; they keep their size on screen whatever the zoom (labelScale), and declutter
  // chooses which show.
  return {
    name: "fcose",
    ...common,
    quality: bySize ? "proof" : "default",
    randomize: !incremental,
    nodeDimensionsIncludeLabels: true,
    packComponents: true,
    nodeRepulsion: () => 4500,
    // Two circles joined by a link sit their radii apart plus a gap, so large clusters do not pile on each other.
    idealEdgeLength: (edge: cytoscape.EdgeSingular) =>
      edge.hasClass("link")
        ? (Number(edge.source().data("diameter") ?? 0) + Number(edge.target().data("diameter") ?? 0)) / 2 + 30
        : edge.hasClass("pair")
          ? 45
          : 80,
    edgeElasticity: () => 0.45,
    nodeSeparation: 75,
    gravity: bySize ? 1 : 0.4,
    numIter: 2500,
    tile: true,
    tilingPaddingVertical: 12,
    tilingPaddingHorizontal: 12,
  } as cytoscape.LayoutOptions;
}

/** Data without the keys Cytoscape refuses to change in place, and without empty values. */
function cleanData(data: ExplorerElement["data"], existing: boolean): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(data)) {
    if (value === undefined) continue;
    if (existing && (key === "parent" || key === "source" || key === "target" || key === "id")) continue;
    out[key] = value;
  }
  return out;
}

/** Makes the canvas hold exactly `elements`; returns the nodes added, placed beside the node they came from. */
export function sync(cy: cytoscape.Core, elements: readonly ExplorerElement[]): cytoscape.NodeCollection {
  const wanted = new Map(elements.map((element) => [String(element.data.id), element]));
  const additions: cytoscape.ElementDefinition[] = [];
  cy.batch(() => {
    cy.elements()
      .filter((element) => !wanted.has(element.id()))
      .remove();
    for (const element of elements) {
      const found = cy.getElementById(String(element.data.id));
      if (found.nonempty()) {
        found.data(cleanData(element.data, true));
        found.classes(element.classes ?? []);
        continue;
      }
      const anchor = element.data.anchor ? cy.getElementById(String(element.data.anchor)) : null;
      const at = anchor?.nonempty() ? anchor.position() : null;
      const angle = Math.random() * Math.PI * 2;
      additions.push({
        group: element.group,
        data: cleanData(element.data, false) as cytoscape.ElementDataDefinition,
        classes: element.classes ?? [],
        position: element.group === "nodes" && at ? { x: at.x + 60 * Math.cos(angle), y: at.y + 60 * Math.sin(angle) } : undefined,
      });
    }
  });
  // Parents before their children, nodes before edges.
  additions.sort((a, b) => rank(a) - rank(b));
  return cy.add(additions).nodes();
}

function rank(element: cytoscape.ElementDefinition): number {
  if (element.group === "edges") return 2;
  return element.data.parent ? 1 : 0;
}

/** Lights what the reader looks at: `focus` with its edges and neighbours, `selected` ringed; the rest dims. */
export function light(cy: cytoscape.Core, selected: string | null, focus: string | null): void {
  cy.batch(() => {
    cy.elements().removeClass("dim hl reveal labelled chosen");
    if (selected) cy.getElementById(selected).addClass("chosen labelled");
    if (!focus) return;
    const node = cy.getElementById(focus);
    if (node.empty() || !node.isNode()) return;
    const near = node.closedNeighborhood();
    near.nodes().addClass("labelled");
    node.connectedEdges().addClass("hl reveal");
    cy.elements().not(near).addClass("dim");
  });
}

export function inView(cy: cytoscape.Core, id: string): boolean {
  const node = cy.getElementById(id);
  if (node.empty()) return true;
  const { x, y } = node.position();
  const box = cy.extent();
  return x >= box.x1 && x <= box.x2 && y >= box.y1 && y <= box.y2;
}

/** What the labels' sizes are multiplied by so they keep their size on screen at `zoom` (within 0.25 and 4). */
export function labelScale(zoom: number): number {
  return Math.min(4, Math.max(0.25, 1 / zoom));
}


type Box = { x1: number; y1: number; x2: number; y2: number };

function overlaps(a: Box, b: Box, pad: number): boolean {
  return !(a.x2 + pad < b.x1 || a.x1 - pad > b.x2 || a.y2 + pad < b.y1 || a.y1 - pad > b.y2);
}

/**
 * Chooses which labels show, the more important first: the labels of `forced` (the node selected or under the
 * pointer) always, then those `light` lit (its neighbours) and the node a view started from, then the largest or most
 * linked. A label shows when it covers no label already shown and no node already shown with its label; such a node
 * is then drawn above the others (`z`, highest first), so neither its label nor its circle hides under a node drawn
 * after it. The others are `crowded`. Labels keep their size on screen (labelScale), so zooming in makes room and more
 * of them show. A label off the screen takes no room; a dimmed one shows nothing and takes none either. Returns how
 * many are crowded; call it again after a zoom, a pan, a new layout or a change of what is lit.
 */
export function declutter(cy: cytoscape.Core, forced: readonly (string | null)[]): number {
  const pinned = new Set(forced.filter((id): id is string => Boolean(id)));
  const pad = 3 / cy.zoom();
  const view = cy.extent();
  const nodes = cy.nodes(":visible").filter((node) => !node.hasClass("dim"));
  const rank = (node: cytoscape.NodeSingular) =>
    pinned.has(node.id()) ? 2 : node.hasClass("labelled") || node.hasClass("seed") ? 1 : 0;
  const weight = (node: cytoscape.NodeSingular) => Number(node.data("size") ?? node.data("degree") ?? 0);
  const ordered = (nodes.toArray() as cytoscape.NodeSingular[]).sort(
    (a, b) => rank(b) - rank(a) || weight(b) - weight(a) || (a.id() < b.id() ? -1 : 1),
  );
  const shown: { node: cytoscape.NodeSingular; label: Box; body: Box }[] = [];
  const crowded: cytoscape.NodeSingular[] = [];
  for (const node of ordered) {
    if (!String(node.data("label") ?? "")) continue;
    const label = node.boundingBox({ includeNodes: false, includeEdges: false, includeLabels: true, includeOverlays: false });
    if (!overlaps(label, view, 0)) continue;
    const covers = shown.some((other) => overlaps(label, other.label, pad) || overlaps(label, other.body, pad));
    if (covers && !pinned.has(node.id())) crowded.push(node);
    else shown.push({ node, label, body: node.boundingBox({ includeLabels: false, includeOverlays: false }) });
  }
  cy.batch(() => {
    cy.nodes().removeClass("crowded").removeData("z");
    for (const node of crowded) node.addClass("crowded");
    shown.forEach(({ node }, i) => node.data("z", shown.length - i));
  });
  return crowded.length;
}
