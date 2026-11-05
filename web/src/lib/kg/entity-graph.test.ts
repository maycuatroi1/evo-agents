import { describe, expect, it } from "vitest";

import {
  buildGraph,
  DEFAULT_LIMIT,
  edgeLabel,
  edgeSize,
  ENTITY_PALETTE,
  type EntityEdge,
  type EntityGraphNode,
  estimateCost,
  kindColour,
  matchEntities,
  mergeGraphs,
  neighbourIds,
  nodeSize,
  parseDepth,
  parseLimit,
  splitShown,
  typeCounts,
} from "./entity-graph";

const label = { level: "internal", location: "any", integrity: "U" };
const node = (id: string, kind: string, name: string, docs = 1, hop?: number): EntityGraphNode => ({
  id,
  kind,
  name,
  type: kind.toLowerCase(),
  description: null,
  docs,
  degree: 1,
  status: "inferred",
  conf: 0.9,
  model: "anthropic/claude-haiku-5.5",
  label,
  ...(hop === undefined ? {} : { hop }),
});
const edge = (id: string, src: string, dst: string, weight = 1, keywords: string | null = null): EntityEdge => ({
  id,
  src,
  rel: "works_for",
  dst,
  keywords,
  description: null,
  weight,
  conf: 0.8,
  status: "inferred",
  model: "anthropic/claude-haiku-5.5",
  sources: 1,
  label,
});

const ADA = node("entity:ada", "Person", "Ada Lovelace", 9);
const BABBAGE = node("entity:babbage", "Person", "Charles Babbage", 4);
const ENGINE = node("entity:engine", "Artifact", "Analytical Engine", 6);
const LONDON = node("entity:london", "Location", "London", 2);
const SET = { nodes: [ADA, BABBAGE, ENGINE, LONDON], edges: [edge("r:1", ADA.id, ENGINE.id, 3, "designed, notes"), edge("r:2", BABBAGE.id, ENGINE.id, 5)] };

describe("the URL's limit and depth", () => {
  it("keep the offered values and fall back on the defaults", () => {
    expect(parseLimit("300")).toBe(300);
    expect(parseLimit("42")).toBe(DEFAULT_LIMIT);
    expect(parseLimit(null)).toBe(DEFAULT_LIMIT);
    expect(parseDepth("3")).toBe(3);
    expect(parseDepth("9")).toBe(1);
    expect(parseDepth(undefined)).toBe(1);
  });
});

describe("mergeGraphs", () => {
  it("keeps each node and edge once, the first hop, and drops edges whose ends are missing", () => {
    const merged = mergeGraphs([
      { nodes: [{ ...ADA, hop: 0 }, ENGINE], edges: [edge("r:1", ADA.id, ENGINE.id)] },
      null,
      { nodes: [{ ...ADA, hop: 1 }, BABBAGE], edges: [edge("r:1", ADA.id, ENGINE.id), edge("r:2", BABBAGE.id, ENGINE.id), edge("r:9", ADA.id, "entity:gone")] },
    ]);
    expect(merged.nodes.map((n) => n.id)).toEqual([ADA.id, ENGINE.id, BABBAGE.id]);
    expect(merged.nodes[0]?.hop).toBe(0);
    expect(merged.edges.map((e) => e.id)).toEqual(["r:1", "r:2"]);
  });
});

describe("the type filter", () => {
  it("counts the kinds, most first, then by name", () => {
    expect(typeCounts(SET.nodes)).toEqual([
      { kind: "Person", count: 2 },
      { kind: "Artifact", count: 1 },
      { kind: "Location", count: 1 },
    ]);
  });

  it("lists the isolated entities beside the canvas, most documents first", () => {
    const { linked, isolated } = splitShown(SET, new Set());
    expect(linked.nodes.map((n) => n.id)).toEqual([ADA.id, BABBAGE.id, ENGINE.id]);
    expect(isolated.map((n) => n.id)).toEqual([LONDON.id]);
  });

  it("hides a kind with its relations, so what it linked can become isolated", () => {
    const { linked, isolated } = splitShown(SET, new Set(["Artifact"]));
    expect(linked).toEqual({ nodes: [], edges: [] });
    expect(isolated.map((n) => n.id)).toEqual([ADA.id, BABBAGE.id, LONDON.id]);
  });
});

describe("sizes and labels", () => {
  it("sizes a node by its documents on a square-root scale between 4 and 18", () => {
    expect(nodeSize(1, 9)).toBe(4);
    expect(nodeSize(9, 9)).toBe(18);
    expect(nodeSize(3, 9)).toBe(11);
    expect(nodeSize(1, 1)).toBe(6);
  });

  it("widens an edge with its weight, on a log scale", () => {
    expect(edgeSize(1, 10)).toBe(0.6);
    expect(edgeSize(10, 10)).toBe(4);
    expect(edgeSize(3, 1)).toBe(1);
  });

  it("names a relation by its keywords, else by its type in words", () => {
    expect(edgeLabel({ keywords: " designed, notes ", rel: "works_for" })).toBe("designed, notes");
    expect(edgeLabel({ keywords: null, rel: "works_for" })).toBe("works for");
  });

  it("colours a kind with its tone in each theme, an unknown kind too", () => {
    const tones = ENTITY_PALETTE.flatMap((tone) => [tone.light, tone.dark]);
    expect(tones).toContain(kindColour("Person", false));
    expect(tones).toContain(kindColour("Not a known kind", true));
    expect(kindColour("Person", false)).not.toBe(kindColour("Person", true));
  });
});

describe("buildGraph", () => {
  it("draws one node per entity and one undirected edge per relation, sized and labelled", () => {
    const graph = buildGraph(SET, { dark: false, edgeColour: "#ccc" });
    expect(graph.order).toBe(4);
    expect(graph.size).toBe(2);
    expect(graph.type).toBe("undirected");
    expect(graph.getNodeAttribute(ADA.id, "size")).toBe(18);
    expect(graph.getNodeAttribute(ADA.id, "label")).toBe("Ada Lovelace");
    expect(graph.getEdgeAttribute("r:1", "label")).toBe("designed, notes");
    expect(graph.getEdgeAttribute("r:2", "size")).toBe(4);
  });

  it("keeps the places laid out before and starts a new node beside a placed neighbour", () => {
    const previous = new Map([
      [ENGINE.id, { x: 100, y: 100 }],
      [ADA.id, { x: 90, y: 110 }],
    ]);
    const graph = buildGraph(SET, { dark: true, edgeColour: "#333", previous });
    expect(graph.getNodeAttributes(ENGINE.id)).toMatchObject({ x: 100, y: 100 });
    const { x, y } = graph.getNodeAttributes(BABBAGE.id);
    expect(Math.abs(x - 100)).toBeLessThan(1);
    expect(Math.abs(y - 100)).toBeLessThan(1);
  });

  it("places the same graph the same way twice", () => {
    const one = buildGraph(SET, { dark: false, edgeColour: "#ccc" });
    const two = buildGraph(SET, { dark: false, edgeColour: "#ccc" });
    expect(one.getNodeAttributes(LONDON.id)).toEqual(two.getNodeAttributes(LONDON.id));
  });
});

describe("search and neighbours", () => {
  it("finds drawn entities by every word, case and accents aside, the exact name first", () => {
    const nodes = [...SET.nodes, node("entity:lovelace", "Person", "Lovelace", 1), node("entity:hue", "Location", "Huế", 3)];
    expect(matchEntities(nodes, "LOVELACE").map((n) => n.id)).toEqual(["entity:lovelace", ADA.id]);
    expect(matchEntities(nodes, "ada love").map((n) => n.id)).toEqual([ADA.id]);
    expect(matchEntities(nodes, "hue").map((n) => n.id)).toEqual(["entity:hue"]);
    expect(matchEntities(nodes, "  ")).toEqual([]);
  });

  it("lights a node and those one relation away", () => {
    expect([...neighbourIds(SET, ENGINE.id)].sort()).toEqual([ADA.id, BABBAGE.id, ENGINE.id].sort());
  });
});

describe("the empty state's estimate", () => {
  it("prices one model call a document at the strategy's measured tokens", () => {
    const { perDocument, total } = estimateCost(1000);
    expect(perDocument).toBeCloseTo((2000 * 0.1 + 1274 * 0.5) / 1e6, 10);
    expect(total).toBeCloseTo(perDocument * 1000, 10);
    expect(estimateCost(-3).total).toBe(0);
  });
});
