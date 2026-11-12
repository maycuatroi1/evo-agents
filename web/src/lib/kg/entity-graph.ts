import Graph from "graphology";

import type { components } from "@/lib/api/schema";

import { type CanvasEdge, type CanvasNode, restEdgeColour, seedPosition } from "./canvas-graph";
import type { Theme } from "./canvas-theme";
import { kindTone } from "./graph";

/**
 * The model of the explorer's Entities view (`/p/{project}/kg?view=entities`), without the renderer: the graph of
 * entities and relations the hub reads for the reader (`/v1/kg/{project}/entity-graph`, `.../entity-graph/around`),
 * merged with the neighbourhoods the reader opens, filtered by type, and turned into the graphology graph every canvas
 * of the graph draws (components/kg/sigma-canvas.tsx). Kept apart from sigma, which needs WebGL, so it runs under
 * vitest's jsdom.
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
export type CodeNode = Schemas["CodeNode"];
export type CodeEdge = Schemas["CodeEdge"];
export type CodeLink = Schemas["CodeLink"];
export type EntitySetup = Schemas["SetupOut"];

export { DEFAULT_LIMIT, DEPTHS, type Depth, EXPAND_LIMIT, LIMITS, parseDepth, parseLimit } from "./entity-params";

/**
 * What the canvas draws: the nodes and edges of the base graph and of each neighbourhood opened, each once, and with
 * the code switch on, the Directories, Files and Symbols those entities point to (`code=true` of the reads).
 */
export type WorkingSet = { nodes: EntityGraphNode[]; edges: EntityEdge[]; codeNodes?: CodeNode[]; codeEdges?: CodeEdge[] };

/** A graph as the hub answers it, its code nodes included when they were asked for. */
type GraphPart = WorkingSet & { code_nodes?: CodeNode[]; code_edges?: CodeEdge[] };

/** The key of the canvas edge from an entity to a code node: one per pair, whatever relations join them. */
export function codeEdgeKey(edge: Pick<CodeEdge, "src" | "dst">): string {
  return `code:${edge.src}->${edge.dst}`;
}

export function mergeGraphs(parts: readonly (GraphPart | null | undefined)[]): WorkingSet {
  const nodes = new Map<string, EntityGraphNode>();
  const edges = new Map<string, EntityEdge>();
  const codeNodes = new Map<string, CodeNode>();
  const codeEdges = new Map<string, CodeEdge>();
  for (const part of parts) {
    if (!part) continue;
    for (const node of part.nodes) {
      const known = nodes.get(node.id);
      // The hop of a focus's own answer wins over a later neighbourhood's.
      nodes.set(node.id, known ? { ...node, hop: known.hop ?? node.hop } : node);
    }
    for (const edge of part.edges) edges.set(edge.id, edge);
    for (const node of part.code_nodes ?? part.codeNodes ?? []) codeNodes.set(node.id, node);
    for (const edge of part.code_edges ?? part.codeEdges ?? []) codeEdges.set(codeEdgeKey(edge), edge);
  }
  return {
    nodes: [...nodes.values()],
    edges: [...edges.values()].filter((edge) => nodes.has(edge.src) && nodes.has(edge.dst)),
    codeNodes: [...codeNodes.values()],
    codeEdges: [...codeEdges.values()].filter((edge) => nodes.has(edge.src) && codeNodes.has(edge.dst)),
  };
}

/** The node kinds of a working set, most first, with their counts: the legend and its filters. */
export function typeCounts(nodes: readonly EntityGraphNode[]): { kind: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const node of nodes) counts.set(node.kind, (counts.get(node.kind) ?? 0) + 1);
  return [...counts.entries()].map(([kind, count]) => ({ kind, count })).sort((a, b) => b.count - a.count || a.kind.localeCompare(b.kind));
}

/**
 * The working set once the hidden kinds are out, split in two: the nodes with at least one relation left, which the
 * canvas draws, and the isolated ones, which the rail lists instead of scattering them over the canvas. With code
 * drawn, a link to code counts as a relation, and a code node stays while one entity kept points to it.
 */
export function splitShown(set: WorkingSet, hidden: ReadonlySet<string>): { linked: WorkingSet; isolated: EntityGraphNode[] } {
  const kept = set.nodes.filter((node) => !hidden.has(node.kind));
  const ids = new Set(kept.map((node) => node.id));
  const edges = set.edges.filter((edge) => ids.has(edge.src) && ids.has(edge.dst));
  const codeEdges = (set.codeEdges ?? []).filter((edge) => ids.has(edge.src));
  const pointed = new Set(codeEdges.map((edge) => edge.dst));
  const touched = new Set([...edges.flatMap((edge) => [edge.src, edge.dst]), ...codeEdges.map((edge) => edge.src)]);
  return {
    linked: {
      nodes: kept.filter((node) => touched.has(node.id)),
      edges,
      codeNodes: (set.codeNodes ?? []).filter((node) => pointed.has(node.id)),
      codeEdges,
    },
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

/** The palette index of an entity type: its own tone, or one picked from its name for a type outside the known ones. */
export function entityTone(kind: string): number {
  const known = KIND_TONES[kind];
  if (known !== undefined) return known;
  let hash = 0;
  for (const char of kind) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return hash % ENTITY_PALETTE.length;
}

export function kindColour(kind: string, dark: boolean): string {
  const tone = ENTITY_PALETTE[entityTone(kind)];
  return dark ? tone.dark : tone.light;
}

export type EntityNodeAttributes = CanvasNode & {
  docs: number;
  hop: number | null;
  /** An entity's visible code links (Directories, Files, Symbols); 0 for a code node. */
  code: number;
  /** A Directory, File or Symbol an entity points to, drawn in its kind's tone as on every canvas of the graph. */
  isCode: boolean;
};

export type EntityEdgeAttributes = CanvasEdge & { rel: string; weight: number; code: boolean };

/**
 * The graphology graph the canvas draws from a working set: one node per entity, coloured by type and sized by
 * documents, one undirected edge per relation (LightRAG relations have no direction), labelled by its keywords; with
 * code, one node per code node in its kind's tone (the explorer's colour for it), sized by the entities that point to
 * it, and one thin edge from each entity to it. Every node starts at a place seeded by its id; the canvas keeps the
 * places of the graph it drew before (lib/kg/canvas-graph.ts, placeNodes).
 */
export function buildGraph(set: WorkingSet, theme: Theme): Graph<EntityNodeAttributes, EntityEdgeAttributes> {
  const graph = new Graph<EntityNodeAttributes, EntityEdgeAttributes>({ type: "undirected", multi: true });
  const most = Math.max(1, ...set.nodes.map((node) => node.docs));
  const heaviest = Math.max(1, ...set.edges.map((edge) => edge.weight));
  const rest = restEdgeColour(theme);
  for (const node of set.nodes) {
    const seed = seedPosition(node.id);
    const code = node.code ?? 0;
    graph.addNode(node.id, {
      label: node.name,
      kind: node.kind,
      x: seed.x * 100,
      y: seed.y * 100,
      size: nodeSize(node.docs, most),
      color: kindColour(node.kind, theme.dark),
      ringColor: theme.surface,
      ring: 0,
      weight: node.docs,
      badge: code ? `</> ${code}` : undefined,
      docs: node.docs,
      hop: node.hop ?? null,
      code,
      isCode: false,
    });
  }
  for (const edge of set.edges) {
    if (!graph.hasNode(edge.src) || !graph.hasNode(edge.dst) || graph.hasEdge(edge.id)) continue;
    graph.addEdgeWithKey(edge.id, edge.src, edge.dst, {
      label: edgeLabel(edge),
      rel: edge.rel,
      size: edgeSize(edge.weight, heaviest),
      weight: edge.weight,
      color: rest,
      type: "line",
      code: false,
    });
  }
  const mostPointed = Math.max(1, ...(set.codeNodes ?? []).map((node) => node.entities));
  for (const node of set.codeNodes ?? []) {
    if (graph.hasNode(node.id)) continue;
    const near = (set.codeEdges ?? []).find((edge) => edge.dst === node.id && graph.hasNode(edge.src));
    const seed = seedPosition(node.id);
    graph.addNode(node.id, {
      label: node.name,
      kind: node.kind,
      x: seed.x * 100,
      y: seed.y * 100,
      size: Math.max(3, nodeSize(node.entities, mostPointed) * 0.75),
      color: theme.tones[kindTone(node.kind)],
      ringColor: theme.surface,
      ring: 0,
      weight: node.entities,
      anchor: near?.src,
      docs: 0,
      hop: null,
      code: 0,
      isCode: true,
    });
  }
  for (const edge of set.codeEdges ?? []) {
    const key = codeEdgeKey(edge);
    if (!graph.hasNode(edge.src) || !graph.hasNode(edge.dst) || graph.hasEdge(key)) continue;
    graph.addEdgeWithKey(key, edge.src, edge.dst, {
      label: edge.rel.replaceAll("_", " "),
      rel: edge.rel,
      size: 0.5,
      weight: edge.conf,
      color: theme.subtle,
      type: "line",
      quiet: true,
      code: true,
    });
  }
  return graph;
}

/** Whether a canvas node is code, not an entity: the canvas selects it by opening its node page instead. */
export function isCodeNode(attributes: Pick<EntityNodeAttributes, "isCode">): boolean {
  return attributes.isCode;
}

/** A code link's group in the inspector: its repo and package, the repo's root named `/`. */
export function codePackage(link: Pick<CodeLink, "repo" | "package">): string {
  const where = link.package || "/";
  return link.repo ? `${link.repo}:${where}` : where;
}

/** The code links of an entity grouped by repo and package, in the hub's order (repo, package, kind, path). */
export function groupCode(links: readonly CodeLink[]): { key: string; links: CodeLink[] }[] {
  const groups = new Map<string, CodeLink[]>();
  for (const link of links) {
    const key = codePackage(link);
    groups.set(key, [...(groups.get(key) ?? []), link]);
  }
  return [...groups.entries()].map(([key, members]) => ({ key, links: members }));
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
