import type cytoscape from "cytoscape";

import type { ExplorerElement, Scope } from "@/lib/kg/explorer";

/**
 * What the explorer's canvas does to Cytoscape, apart from React: the layouts of each scope, fitting the view, making
 * the canvas hold the elements a scope draws (in place, so nodes keep their places), and lighting what the reader
 * looks at.
 */
export type ExplorerLayout = "force" | "radial" | "tree";

/** The layouts each scope offers: a cluster's parts only make sense to the force layout. */
export const SCOPE_LAYOUTS: Record<Scope["kind"], readonly ExplorerLayout[]> = {
  overview: ["force", "radial"],
  cluster: ["force"],
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
  if (layout === "radial") {
    const depth = depths(cy, root);
    const largest = Math.max(1, ...cy.nodes().map((node) => Number(node.data("size") ?? 0)));
    return {
      name: "concentric",
      ...common,
      concentric: (node: cytoscape.NodeSingular) =>
        kind === "overview" ? Number(node.data("size") ?? 0) : 100 - (depth.get(node.id()) ?? 99),
      levelWidth: () => (kind === "overview" ? largest / 4 : 1),
      minNodeSpacing: kind === "overview" ? 36 : 18,
      startAngle: (3 / 2) * Math.PI,
    } as cytoscape.LayoutOptions;
  }
  if (layout === "tree") {
    return { name: "breadthfirst", ...common, roots: root ? [root] : undefined, directed: false, spacingFactor: 1.15 };
  }
  return {
    name: "fcose",
    ...common,
    quality: kind === "overview" ? "proof" : "default",
    randomize: !incremental,
    nodeDimensionsIncludeLabels: kind === "overview",
    packComponents: true,
    nodeRepulsion: () => (kind === "overview" ? 14000 : 4500),
    idealEdgeLength: () => (kind === "overview" ? 150 : kind === "cluster" ? 42 : 85),
    edgeElasticity: () => 0.45,
    nestingFactor: 0.1,
    gravity: kind === "cluster" ? 0.4 : 0.25,
    gravityCompound: 1.2,
    gravityRangeCompound: 1.5,
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
    if (node.hasClass("part")) {
      const inside = node.union(node.descendants());
      cy.nodes().not(inside).addClass("dim");
      cy.edges().filter((edge) => !inside.contains(edge.source()) || !inside.contains(edge.target())).addClass("dim");
      return;
    }
    const near = node.closedNeighborhood();
    near.nodes().addClass("labelled");
    node.connectedEdges().addClass("hl reveal");
    cy.elements().not(near).not(near.nodes().ancestors()).addClass("dim");
  });
}

export function inView(cy: cytoscape.Core, id: string): boolean {
  const node = cy.getElementById(id);
  if (node.empty()) return true;
  const { x, y } = node.position();
  const box = cy.extent();
  return x >= box.x1 && x <= box.x2 && y >= box.y1 && y <= box.y2;
}
