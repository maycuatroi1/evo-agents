import Graph from "graphology";

import type { components } from "@/lib/api/schema";

/**
 * The model of the explorer's Entities view (`/p/{project}/kg?view=entities`), without the renderer: the graph of
 * entities and relations the hub reads for the reader (`/v1/kg/{project}/entity-graph`, `.../entity-graph/around`),
 * merged with the neighbourhoods the reader opens, filtered by type, and turned into a graphology graph that sigma.js
 * draws (components/kg/entity-canvas.tsx). Kept apart from sigma, which needs WebGL, so it runs under vitest's jsdom.
 * Every node and edge here is one the hub sent the reader: nothing filters for visibility.
 */
type Schemas = components["schemas"];

export type EntityGraphData = Schemas["EntityGraph"];
export type EntityNeighbourhood = Schemas["EntityNeighbourhood"];
export type EntityGraphNode = Schemas["EntityGraphNode"];
export type EntityEdge = Schemas["EntityEdge"];
export type EntityDetail = Schemas["EntityDetail"];
export type RelationDetail = Schemas["RelationDetail"];
export type Citation = Schemas["Citation"];

/** The entities the default view and a focus ask for, and the bounds the hub keeps (a3). */
export const DEFAULT_LIMIT = 150;
export const LIMITS = [50, 150, 300, 600, 1000] as const;
export const DEPTHS = [1, 2, 3] as const;
export type Depth = (typeof DEPTHS)[number];
/** The neighbours a double click opens around one entity. */
export const EXPAND_LIMIT = 40;

export function parseLimit(value: string | null | undefined): number {
  const found = Number(value);
  return LIMITS.includes(found as (typeof LIMITS)[number]) ? found : DEFAULT_LIMIT;
}

export function parseDepth(value: string | null | undefined): Depth {
  const found = Number(value);
  return found === 2 || found === 3 ? found : 1;
}

/** What the canvas draws: the nodes and edges of the base graph and of each neighbourhood opened, each once. */
export type WorkingSet = { nodes: EntityGraphNode[]; edges: EntityEdge[] };

export function mergeGraphs(parts: readonly (WorkingSet | null | undefined)[]): WorkingSet {
  const nodes = new Map<string, EntityGraphNode>();
  const edges = new Map<string, EntityEdge>();
  for (const part of parts) {
    if (!part) continue;
    for (const node of part.nodes) {
      const known = nodes.get(node.id);
      // The hop of a focus's own answer wins over a later neighbourhood's.
      nodes.set(node.id, known ? { ...node, hop: known.hop ?? node.hop } : node);
    }
    for (const edge of part.edges) edges.set(edge.id, edge);
  }
  return { nodes: [...nodes.values()], edges: [...edges.values()].filter((edge) => nodes.has(edge.src) && nodes.has(edge.dst)) };
}

/** The node kinds of a working set, most first, with their counts: the legend and its filters. */
export function typeCounts(nodes: readonly EntityGraphNode[]): { kind: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const node of nodes) counts.set(node.kind, (counts.get(node.kind) ?? 0) + 1);
  return [...counts.entries()].map(([kind, count]) => ({ kind, count })).sort((a, b) => b.count - a.count || a.kind.localeCompare(b.kind));
}

/**
 * The working set once the hidden kinds are out, split in two: the nodes with at least one relation left, which the
 * canvas draws, and the isolated ones, which the rail lists instead of scattering them over the canvas.
 */
export function splitShown(set: WorkingSet, hidden: ReadonlySet<string>): { linked: WorkingSet; isolated: EntityGraphNode[] } {
  const kept = set.nodes.filter((node) => !hidden.has(node.kind));
  const ids = new Set(kept.map((node) => node.id));
  const edges = set.edges.filter((edge) => ids.has(edge.src) && ids.has(edge.dst));
  const touched = new Set(edges.flatMap((edge) => [edge.src, edge.dst]));
  return {
    linked: { nodes: kept.filter((node) => touched.has(node.id)), edges },
    isolated: kept.filter((node) => !touched.has(node.id)).sort((a, b) => b.docs - a.docs || a.name.localeCompare(b.name)),
  };
}

/** A node's size on the canvas, by the documents that mention it, on a square-root scale against the most mentioned. */
export function nodeSize(docs: number, most: number): number {
  const min = 4;
  const max = 18;
  if (most <= 1) return min + 2;
  return Math.round((min + (max - min) * Math.sqrt(Math.max(docs - 1, 0) / (most - 1))) * 10) / 10;
}

/** An edge's width, by its weight against the heaviest, on a log scale. */
export function edgeSize(weight: number, heaviest: number): number {
  if (heaviest <= 1) return 1;
  return Math.round((0.6 + 3.4 * (Math.log(Math.max(weight, 1)) / Math.log(heaviest))) * 100) / 100;
}

/** What an edge says on the canvas: its keywords, else its relation's type in words. */
export function edgeLabel(edge: Pick<EntityEdge, "keywords" | "rel">): string {
  const keywords = edge.keywords?.trim();
  return keywords ? keywords : edge.rel.replaceAll("_", " ");
}

/** The fixed entity kinds of the lightrag style, then the closed ontology's, each with a tone of its own. */
export const KIND_TONES: Record<string, number> = {
  Actor: 0,
  Organization: 1,
  Place: 2,
  Event: 3,
  Concept: 4,
  Method: 5,
  Content: 6,
  Data: 7,
  Artifact: 8,
  Creature: 9,
  NaturalObject: 10,
  Other: 11,
  Work: 6,
};

/** Twelve categorical tones that hold 3:1 against the light and the dark surface; the dark one is lighter. */
export const ENTITY_PALETTE: { light: string; dark: string }[] = [
  { light: "#2f62d8", dark: "#6e9cff" },
  { light: "#c2410c", dark: "#fb8c4a" },
  { light: "#0f8072", dark: "#2dc2b0" },
  { light: "#b4237a", dark: "#f06bb5" },
  { light: "#7444e0", dark: "#a88bff" },
  { light: "#5b7a0c", dark: "#a3c43a" },
  { light: "#a5650a", dark: "#f2b84b" },
  { light: "#0e7490", dark: "#38bdf8" },
  { light: "#be123c", dark: "#fb7185" },
  { light: "#15803d", dark: "#4ade80" },
  { light: "#7a6f66", dark: "#b8b0a8" },
  { light: "#5b6577", dark: "#9aa6b8" },
];

/** The palette index of a kind: its own tone, or one picked from its name for a kind outside the known ones. */
export function kindTone(kind: string): number {
  const known = KIND_TONES[kind];
  if (known !== undefined) return known;
  let hash = 0;
  for (const char of kind) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return hash % ENTITY_PALETTE.length;
}

export function kindColour(kind: string, dark: boolean): string {
  const tone = ENTITY_PALETTE[kindTone(kind)];
  return dark ? tone.dark : tone.light;
}

/** A place in [-1, 1]² picked from the id, so the same graph starts the layout from the same places on every visit. */
export function seedPosition(id: string): { x: number; y: number } {
  let a = 2166136261;
  let b = 5381;
  for (const char of id) {
    a = Math.imul(a ^ char.charCodeAt(0), 16777619) >>> 0;
    b = ((b << 5) + b + char.charCodeAt(0)) >>> 0;
  }
  return { x: (a / 0xffffffff) * 2 - 1, y: (b / 0xffffffff) * 2 - 1 };
}

export type EntityNodeAttributes = {
  label: string;
  kind: string;
  x: number;
  y: number;
  size: number;
  color: string;
  docs: number;
  hop: number | null;
};

export type EntityEdgeAttributes = { label: string; rel: string; size: number; weight: number; color: string };

/**
 * The graphology graph sigma draws from a working set: one node per entity, coloured by kind and sized by documents,
 * one undirected edge per relation (LightRAG relations have no direction), labelled by its keywords. Places already
 * laid out in `previous` are kept, so an opened neighbourhood grows beside the node it came from.
 */
export function buildGraph(
  set: WorkingSet,
  options: { dark: boolean; edgeColour: string; previous?: ReadonlyMap<string, { x: number; y: number }> },
): Graph<EntityNodeAttributes, EntityEdgeAttributes> {
  const graph = new Graph<EntityNodeAttributes, EntityEdgeAttributes>({ type: "undirected", multi: true });
  const most = Math.max(1, ...set.nodes.map((node) => node.docs));
  const heaviest = Math.max(1, ...set.edges.map((edge) => edge.weight));
  const placed = new Map(options.previous ?? []);
  for (const node of set.nodes) {
    let place = placed.get(node.id);
    if (!place) {
      // A new node starts next to a neighbour already placed, else at its seeded place.
      const near = set.edges.find((edge) => (edge.src === node.id && placed.has(edge.dst)) || (edge.dst === node.id && placed.has(edge.src)));
      const anchor = near ? placed.get(near.src === node.id ? near.dst : near.src) : undefined;
      const seed = seedPosition(node.id);
      place = anchor ? { x: anchor.x + seed.x * 0.05, y: anchor.y + seed.y * 0.05 } : seed;
    }
    graph.addNode(node.id, {
      label: node.name,
      kind: node.kind,
      x: place.x,
      y: place.y,
      size: nodeSize(node.docs, most),
      color: kindColour(node.kind, options.dark),
      docs: node.docs,
      hop: node.hop ?? null,
    });
  }
  for (const edge of set.edges) {
    if (!graph.hasNode(edge.src) || !graph.hasNode(edge.dst) || graph.hasEdge(edge.id)) continue;
    graph.addEdgeWithKey(edge.id, edge.src, edge.dst, {
      label: edgeLabel(edge),
      rel: edge.rel,
      size: edgeSize(edge.weight, heaviest),
      weight: edge.weight,
      color: options.edgeColour,
    });
  }
  return graph;
}

/** The ids within one relation of `id` in the graph drawn, `id` included: what lights up when it is pointed at. */
export function neighbourIds(set: WorkingSet, id: string): Set<string> {
  const out = new Set([id]);
  for (const edge of set.edges) {
    if (edge.src === id) out.add(edge.dst);
    if (edge.dst === id) out.add(edge.src);
  }
  return out;
}

/** The drawn entities whose name holds every word of `query`, case and accents aside, best match first. */
export function matchEntities(nodes: readonly EntityGraphNode[], query: string, max = 8): EntityGraphNode[] {
  const fold = (text: string) => text.normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase();
  const words = fold(query).split(/\s+/).filter(Boolean);
  if (words.length === 0) return [];
  const rank = (name: string) => (name === words.join(" ") ? 0 : name.startsWith(words[0]) ? 1 : 2);
  return nodes
    .map((node) => ({ node, name: fold(node.name) }))
    .filter(({ name }) => words.every((word) => name.includes(word)))
    .sort((a, b) => rank(a.name) - rank(b.name) || b.node.docs - a.node.docs || a.name.localeCompare(b.name))
    .slice(0, max)
    .map(({ node }) => node);
}

/**
 * What turning the entities step on would cost for `documents` documents, in USD, at the entity-graph-api strategy's
 * model and price (anthropic/claude-haiku-5.5 on OpenRouter, 0.10 per million tokens in and 0.50 out, 2026-11-05) and
 * its measured tokens a passage (1,274 out with reasoning off, docs/kg-strategies.md), with about 2,000 tokens in a
 * call (prompt and text) and one call a document; merges and summaries add a few percent. Long documents cost more.
 */
export const COST_MODEL = { model: "anthropic/claude-haiku-5.5", tokensIn: 2_000, tokensOut: 1_274, priceIn: 0.1, priceOut: 0.5, ceiling: 5 };

export function estimateCost(documents: number): { perDocument: number; total: number } {
  const perDocument = (COST_MODEL.tokensIn * COST_MODEL.priceIn + COST_MODEL.tokensOut * COST_MODEL.priceOut) / 1e6;
  return { perDocument, total: Math.max(documents, 0) * perDocument };
}

/** The command that turns the entity graph on for a hub project; the next build reads the entities. */
export function enableCommand(project: string): string {
  return `evo-agents hub kg settings --project ${project} --strategy entity-graph-api --reason "entity graph view"`;
}
