import { displayName, kindStyle, shortLabel } from "./graph";
import { type ExplorerElement, kindsOfMembers, majorIds, nodeDiameter } from "./explorer";
import type { GraphEdge, GraphNode, Neighbourhood, NeighbourGroup } from "./types";

/**
 * The explorer at a node (lib/kg/explorer.ts names the scopes): the node's neighbourhood and those of the nodes the
 * reader expands, merged into one working set, and how the canvas draws it.
 */

/** Direct neighbours of each group an expansion shows before the reader opens the group. */
export const EXPAND_GROUP = 6;
/** Past this many nodes the explorer asks the reader to hide or collapse some before expanding more. */
export const WORKING_CAP = 400;

/** A node the reader expanded, and the groups of its neighbours they opened in full. */
export type Expansion = { id: string; open: readonly string[] };

/** The prefix of the id of the node that stands for what a group holds beyond those drawn. */
export const MORE = "more\u0000";

export function moreId(expansion: string, groupKey: string): string {
  return `${MORE}${expansion}\u0000${groupKey}`;
}

/** The expansion and the group a "+N more" node stands for; null for any other id. */
export function parseMoreId(id: string): { expansion: string; group: string } | null {
  if (!id.startsWith(MORE)) return null;
  const rest = id.slice(MORE.length);
  const cut = rest.lastIndexOf("\u0000");
  return cut < 0 ? null : { expansion: rest.slice(0, cut), group: rest.slice(cut + 1) };
}

export type WorkingNode = GraphNode & {
  /** The expansion that brought the node in first. */
  origin: string;
  expanded: boolean;
};

export type WorkingSet = {
  seed: string;
  nodes: Map<string, WorkingNode>;
  edges: Map<string, GraphEdge>;
  /** The groups of each expanded node that hold more than it draws. */
  more: { expansion: string; group: NeighbourGroup; hidden: number }[];
};

/**
 * The neighbourhoods of `expansions`, in order (the seed first), merged into one working set: each node once, from
 * the first neighbourhood that holds it, each edge once, and without the nodes the reader hid (or their edges). A
 * neighbourhood not read yet adds nothing.
 */
export function mergeExpansions(
  seed: string,
  expansions: readonly Expansion[],
  views: readonly (Neighbourhood | undefined)[],
  hiddenNodes: ReadonlySet<string> = new Set(),
): WorkingSet {
  const nodes = new Map<string, WorkingNode>();
  const edges = new Map<string, GraphEdge>();
  const more: WorkingSet["more"] = [];
  // An expansion stands for the node its neighbourhood is about: an alias asked for reads as the node's own id.
  const expandedIds = new Set(expansions.flatMap((expansion, i) => [expansion.id, views[i]?.focus ?? expansion.id]));
  expansions.forEach((expansion, i) => {
    const view = views[i];
    if (!view || hiddenNodes.has(expansion.id) || hiddenNodes.has(view.focus)) return;
    for (const node of view.nodes) {
      if (hiddenNodes.has(node.id) || nodes.has(node.id)) continue;
      nodes.set(node.id, { ...node, origin: view.focus, expanded: expandedIds.has(node.id) });
    }
    for (const edge of view.edges) edges.set(edge.id, edge);
    for (const group of view.groups) {
      const hidden = group.total - group.shown.length;
      if (hidden > 0) more.push({ expansion: view.focus, group, hidden });
    }
  });
  for (const [id, edge] of edges) {
    if (!nodes.has(edge.src) || !nodes.has(edge.dst)) edges.delete(id);
  }
  return { seed: views[0]?.focus ?? seed, nodes, edges, more: more.filter((entry) => nodes.has(entry.expansion)) };
}

/** `id` expanded, keeping the expansions before it; nothing changes when it already is. */
export function expand(expansions: readonly Expansion[], id: string): Expansion[] {
  return expansions.some((expansion) => expansion.id === id) ? [...expansions] : [...expansions, { id, open: [] }];
}

/** `id` collapsed: its expansion goes, unless it is the seed's, which stays. */
export function collapse(expansions: readonly Expansion[], id: string, seed: string): Expansion[] {
  return id === seed ? [...expansions] : expansions.filter((expansion) => expansion.id !== id);
}

/** The group `key` of `id` opened in full, expanding `id` first when it is not. */
export function openGroup(expansions: readonly Expansion[], id: string, key: string): Expansion[] {
  const next = expand(expansions, id);
  return next.map((expansion) =>
    expansion.id === id && !expansion.open.includes(key) ? { ...expansion, open: [...expansion.open, key] } : expansion,
  );
}

export function workingElements(set: WorkingSet, hiddenKinds: ReadonlySet<string> = new Set()): ExplorerElement[] {
  const highest = Math.max(0, ...[...set.nodes.values()].map((node) => Math.log1p(node.degree)));
  const major = majorIds([...set.nodes.values()].map((node) => ({ id: node.id, degree: node.degree })));
  const nodes: ExplorerElement[] = [...set.nodes.values()].map((node) => {
    const { tone, shape } = kindStyle(node.kind);
    return {
      group: "nodes",
      data: {
        id: node.id,
        label: shortLabel(displayName(node)),
        name: displayName(node),
        kind: node.kind,
        tone,
        shape,
        degree: node.degree,
        diameter: nodeDiameter(Math.log1p(node.degree), highest),
        anchor: node.origin === node.id ? undefined : node.origin,
      },
      classes: [
        "member",
        ...(major.has(node.id) ? ["major"] : []),
        ...(node.id === set.seed ? ["seed"] : []),
        ...(node.expanded ? ["expanded"] : []),
        ...(node.hub ? ["hub"] : []),
        ...(hiddenKinds.has(node.kind) && node.id !== set.seed ? ["kind-hidden"] : []),
      ],
    };
  });
  const edges: ExplorerElement[] = [...set.edges.values()].map((edge) => ({
    group: "edges",
    data: { id: edge.id, source: edge.src, target: edge.dst, rel: edge.rel },
    classes: ["relation"],
  }));
  const more: ExplorerElement[] = set.more.flatMap(({ expansion, group, hidden }) => {
    const id = moreId(expansion, group.key);
    const ends = group.direction === "out" ? { source: expansion, target: id } : { source: id, target: expansion };
    return [
      {
        group: "nodes",
        data: { id, label: `+${hidden} ${group.kind}`, kind: group.kind, anchor: expansion, rel: group.rel },
        classes: ["more", ...(hiddenKinds.has(group.kind) ? ["kind-hidden"] : [])],
      },
      { group: "edges", data: { id: `${id}\u0000edge`, ...ends, rel: group.rel }, classes: ["more-edge"] },
    ];
  });
  return [...nodes, ...edges, ...more];
}

/** The kinds a working set draws, with how many of each, most first. */
export function kindsInView(nodes: Iterable<Pick<GraphNode, "kind">>): { kind: string; count: number }[] {
  return kindsOfMembers([...nodes]);
}

/** The group key a relation of node `id` falls in, as the API names it (`direction:rel:kind`). */
export function relationGroupKey(direction: "out" | "in", rel: string, kind: string): string {
  return `${direction}:${rel}:${kind}`;
}
