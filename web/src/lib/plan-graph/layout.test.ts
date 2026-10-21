import { describe, expect, it } from "vitest";

import { parsePlan } from "@/lib/plans";

import { GRAPH_SIZES, layoutPlanGraph, type PlanGraph, type Point } from "./layout";

type RawStep = { id: string | number; status?: string; depends_on?: (string | number)[] };

const graphOf = (steps: RawStep[]) => layoutPlanGraph(parsePlan({ id: "p", steps }).steps);

const node = (graph: PlanGraph, key: string) => {
  const found = graph.nodes.find((n) => n.key === key);
  if (!found) throw new Error(`no step ${key}`);
  return found;
};

/** The keys of each row, left to right. */
const rows = (graph: PlanGraph) =>
  Array.from({ length: graph.rows }, (_, row) =>
    graph.nodes
      .filter((n) => n.row === row)
      .sort((a, b) => a.x - b.x)
      .map((n) => n.key),
  );

const edge = (graph: PlanGraph, from: string, to: string) => graph.edges.find((e) => e.from === from && e.to === to);

/** Whether the segment a-b enters the inside of the box (touching its border is fine). */
function crosses(a: Point, b: Point, box: { x: number; y: number; width: number; height: number }): boolean {
  const [x1, x2] = [Math.min(a.x, b.x), Math.max(a.x, b.x)];
  const [y1, y2] = [Math.min(a.y, b.y), Math.max(a.y, b.y)];
  return x1 < box.x + box.width && x2 > box.x && y1 < box.y + box.height && y2 > box.y;
}

/** The routing rules every drawn line keeps; returns what breaks them, so a failure names the line. */
function routeProblems(graph: PlanGraph): string[] {
  const { nodeHeight, rowGap } = GRAPH_SIZES;
  const gaps = new Set(Array.from({ length: graph.rows }, (_, row) => row * (nodeHeight + rowGap) + nodeHeight + rowGap / 2));
  const problems: string[] = [];
  for (const e of graph.edges.filter((d) => d.kind === "drawn")) {
    const name = `${e.from}>${e.to}`;
    const { points } = e;
    const from = node(graph, e.from);
    const to = node(graph, e.to);
    if (points.length < 2) problems.push(`${name}: no route`);
    const first = points[0];
    const last = points[points.length - 1];
    if (first.y !== from.y + from.height || first.x <= from.x || first.x >= from.x + from.width) {
      problems.push(`${name}: does not leave the bottom of ${e.from}`);
    }
    if (last.y !== to.y || last.x <= to.x || last.x >= to.x + to.width) problems.push(`${name}: does not enter ${e.to}`);
    for (let i = 1; i < points.length; i += 1) {
      const [a, b] = [points[i - 1], points[i]];
      if (a.x !== b.x && a.y !== b.y) problems.push(`${name}: segment ${i} is not orthogonal`);
      if (a.y === b.y && !gaps.has(a.y)) problems.push(`${name}: turns at y ${a.y}, outside a gap between rows`);
      if (a.y === b.y && b.y < first.y) problems.push(`${name}: goes up`);
      for (const box of graph.nodes) {
        if (crosses(a, b, box)) problems.push(`${name}: crosses the box of ${box.key}`);
      }
    }
  }
  return problems;
}

/** Every dependency that is not part of a cycle leaves its step above the one that waits. */
function orderProblems(graph: PlanGraph): string[] {
  return graph.edges
    .filter((e) => e.kind === "drawn" || e.kind === "implied")
    .filter((e) => node(graph, e.from).row >= node(graph, e.to).row)
    .map((e) => `${e.from} is not above ${e.to}`);
}

// context-economy at revision 19: steps 1 to 10 done, 11 and 12 pending.
const contextEconomy: RawStep[] = [
  { id: 1, status: "done", depends_on: [] },
  { id: 2, status: "done", depends_on: [] },
  { id: 3, status: "done", depends_on: [2] },
  { id: 4, status: "done", depends_on: [] },
  { id: 5, status: "done", depends_on: [1] },
  { id: 6, status: "done", depends_on: [1, 2, 3, 4, 5] },
  { id: 7, status: "done", depends_on: [6] },
  { id: 8, status: "done", depends_on: [7] },
  { id: 9, status: "done", depends_on: [8] },
  { id: 10, status: "done", depends_on: [7, 9] },
  { id: 11, status: "pending", depends_on: [10] },
  { id: 12, status: "pending", depends_on: [11] },
];

describe("layoutPlanGraph", () => {
  it("lays out context-economy revision 19", () => {
    const graph = graphOf(contextEconomy);
    expect(graph.nodes).toHaveLength(12);
    expect(rows(graph)[0]).toEqual(["1", "2", "4"]);
    const into6 = graph.edges.filter((e) => e.to === "6");
    expect(into6.filter((e) => e.kind === "drawn").map((e) => e.from)).toEqual(["3", "4", "5"]);
    expect(into6.map((e) => e.from)).toEqual(["1", "2", "3", "4", "5"]);
    for (const [from, to] of [
      ["1", "6"],
      ["2", "6"],
      ["7", "10"],
    ]) {
      expect(edge(graph, from, to)).toMatchObject({ kind: "implied", points: [] });
    }
    expect(graph.edges.filter((e) => e.kind !== "drawn")).toHaveLength(3);
    expect(graph.rows).toBe(9);
    expect(node(graph, "11")).toMatchObject({ ready: true, waitingOn: [] });
    expect(node(graph, "12")).toMatchObject({ ready: false, waitingOn: ["11"] });
    expect(node(graph, "6").ancestors).toEqual(["1", "2", "3", "4", "5"]);
    expect(node(graph, "6").descendants).toEqual(["7", "8", "9", "10", "11", "12"]);
    expect(node(graph, "3").ancestors).toEqual(["2"]);
    expect(edge(graph, "11", "12")?.fromDone).toBe(false);
    expect(edge(graph, "10", "11")?.fromDone).toBe(true);
    expect(routeProblems(graph)).toEqual([]);
    expect(orderProblems(graph)).toEqual([]);
  });

  it("puts a chain one step per row", () => {
    const graph = graphOf(
      Array.from({ length: 6 }, (_, i) => ({ id: i + 1, depends_on: i ? [i] : [], status: i < 2 ? "done" : "pending" })),
    );
    expect(rows(graph)).toEqual([["1"], ["2"], ["3"], ["4"], ["5"], ["6"]]);
    expect(new Set(graph.nodes.map((n) => n.x)).size).toBe(1);
    expect(graph.nodes.filter((n) => n.ready).map((n) => n.key)).toEqual(["3"]);
    expect(node(graph, "4").waitingOn).toEqual(["3"]);
    expect(graph.edges.every((e) => e.kind === "drawn")).toBe(true);
    expect(graph.height).toBe(6 * GRAPH_SIZES.nodeHeight + 5 * GRAPH_SIZES.rowGap);
    expect(routeProblems(graph)).toEqual([]);
  });

  it("puts eight parallel steps in one row in plan order", () => {
    const graph = graphOf(Array.from({ length: 8 }, (_, i) => ({ id: i + 1 })));
    expect(rows(graph)).toEqual([["1", "2", "3", "4", "5", "6", "7", "8"]]);
    expect(graph.edges).toEqual([]);
    expect(graph.nodes.every((n) => n.ready && n.ancestors.length === 0)).toBe(true);
    const xs = graph.nodes.map((n) => n.x);
    expect(xs[0]).toBe(0);
    expect(graph.width).toBe(8 * GRAPH_SIZES.nodeWidth + 7 * GRAPH_SIZES.columnGap);
  });

  it("keeps a fan-out and fan-in in plan order", () => {
    const graph = graphOf([
      { id: "setup" },
      { id: "api", depends_on: ["setup"] },
      { id: "web", depends_on: ["setup"] },
      { id: "docs", depends_on: ["setup"] },
      { id: "release", depends_on: ["api", "web", "docs", "setup"] },
    ]);
    expect(rows(graph)).toEqual([["setup"], ["api", "web", "docs"], ["release"]]);
    expect(edge(graph, "setup", "release")?.kind).toBe("implied");
    expect(node(graph, "setup").descendants).toEqual(["api", "web", "docs", "release"]);
    expect(routeProblems(graph)).toEqual([]);
  });

  it("routes a long line through a free lane, never across a box", () => {
    const graph = graphOf([
      { id: "a" },
      { id: "b" },
      { id: "c", depends_on: ["b"] },
      { id: "d", depends_on: ["c"] },
      { id: "e", depends_on: ["d", "a"] },
    ]);
    expect(rows(graph)[0]).toEqual(["a", "b"]);
    expect(node(graph, "e").row).toBe(3);
    expect(edge(graph, "a", "e")?.kind).toBe("drawn");
    expect(routeProblems(graph)).toEqual([]);
  });

  it("draws nothing for a key no step has but still returns it", () => {
    const graph = graphOf([{ id: 1 }, { id: 2, depends_on: [1, 99] }, { id: 3, depends_on: ["ghost"] }]);
    expect(edge(graph, "99", "2")).toMatchObject({ kind: "unknown", fromIndex: null, points: [] });
    expect(edge(graph, "ghost", "3")).toMatchObject({ kind: "unknown", points: [] });
    expect(rows(graph)[0]).toEqual(["1", "3"]);
    expect(node(graph, "3")).toMatchObject({ ready: true, waitingOn: [], ancestors: [] });
    expect(node(graph, "2").waitingOn).toEqual(["1"]);
  });

  it("survives a cycle and marks the line that closes it", () => {
    const graph = graphOf([
      { id: 1, depends_on: [3] },
      { id: 2, depends_on: [1] },
      { id: 3, depends_on: [2] },
      { id: 4, depends_on: [4] },
      { id: 5, depends_on: [3] },
    ]);
    expect(graph.nodes).toHaveLength(5);
    const cycles = graph.edges.filter((e) => e.kind === "cycle").map((e) => `${e.from}>${e.to}`);
    expect(cycles).toEqual(["1>2", "4>4"]);
    expect(orderProblems(graph)).toEqual([]);
    expect(routeProblems(graph)).toEqual([]);
    expect(node(graph, "1").ancestors).toEqual(["2", "3"]);
    expect(node(graph, "4")).toMatchObject({ ready: false, waitingOn: ["4"], ancestors: [], descendants: [] });
  });

  it("gives an empty layout for a plan without steps", () => {
    expect(layoutPlanGraph([])).toEqual({ nodes: [], edges: [], width: 0, height: 0, rows: 0, mirrored: false });
  });

  it("lays out 80 steps within 500 ms", () => {
    // A deterministic tangle: each step waits for one to three earlier steps, some far back.
    let seed = 7;
    const random = () => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed / 2147483648;
    };
    const steps: RawStep[] = Array.from({ length: 80 }, (_, i) => {
      const count = i < 4 ? 0 : 1 + Math.floor(random() * 3);
      const deps = new Set<number>();
      for (let n = 0; n < count; n += 1) deps.add(1 + Math.floor(random() * i));
      return { id: i + 1, status: i < 30 ? "done" : "pending", depends_on: [...deps] };
    });
    const started = performance.now();
    const graph = graphOf(steps);
    expect(performance.now() - started).toBeLessThan(500);
    expect(graph.nodes).toHaveLength(80);
    const sources = graph.nodes.filter((n) => !graph.edges.some((e) => e.to === n.key && e.kind !== "unknown"));
    expect(sources.every((n) => n.row === 0)).toBe(true);
    expect(orderProblems(graph)).toEqual([]);
    expect(routeProblems(graph)).toEqual([]);
  });
});
