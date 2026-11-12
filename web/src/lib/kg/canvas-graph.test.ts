import { describe, expect, it } from "vitest";

import { type CanvasGraph, canvasGraph, layoutPositions, mix, placeNodes, radialLevels, treePositions } from "./canvas-graph";

function graphOf(ids: string[], edges: [string, string][], extra: Record<string, { weight?: number; anchor?: string }> = {}): CanvasGraph {
  const graph = canvasGraph();
  for (const id of ids) {
    graph.addNode(id, { label: id, kind: "File", x: 0, y: 0, size: 8, color: "#000000", ringColor: "#ffffff", ring: 0, weight: extra[id]?.weight ?? 1, anchor: extra[id]?.anchor });
  }
  edges.forEach(([a, b], i) => graph.addDirectedEdgeWithKey(`e${i}`, a, b, { label: "", size: 1, color: "#999999", type: "arrow" }));
  return graph;
}

describe("placeNodes", () => {
  it("keeps the places drawn before and starts a new node beside its anchor", () => {
    const graph = graphOf(["a", "b", "c"], [["a", "b"]], { c: { anchor: "a" } });
    const fresh = placeNodes(
      graph,
      new Map([
        ["a", { x: 500, y: 500 }],
        ["b", { x: 700, y: 300 }],
      ]),
    );
    expect(fresh).toEqual(["c"]);
    expect(graph.getNodeAttributes("b")).toMatchObject({ x: 700, y: 300 });
    const c = graph.getNodeAttributes("c");
    expect(Math.hypot(c.x - 500, c.y - 500)).toBeLessThan(20);
  });

  it("places a first drawing the same way twice, every node apart", () => {
    const one = graphOf(["a", "b", "c"], []);
    const two = graphOf(["a", "b", "c"], []);
    expect(placeNodes(one, new Map())).toEqual(["a", "b", "c"]);
    placeNodes(two, new Map());
    expect(one.getNodeAttributes("b")).toEqual(two.getNodeAttributes("b"));
    expect(one.getNodeAttribute("a", "x")).not.toBe(one.getNodeAttribute("b", "x"));
  });
});

describe("layouts", () => {
  it("rings a node's view by steps from its root, nodes out of reach outermost", () => {
    const graph = graphOf(["root", "near", "far", "alone"], [["root", "near"], ["far", "near"]]);
    expect(Object.fromEntries(radialLevels(graph, "root"))).toEqual({ root: 0, near: 1, far: 2, alone: 3 });
    const places = layoutPositions(graph, "radial", { root: "root", settled: false });
    const distance = (id: string) => Math.hypot(places.get(id)!.x, places.get(id)!.y);
    expect(distance("root")).toBe(0);
    expect(distance("near")).toBeLessThan(distance("far"));
  });

  it("puts the heaviest in the middle without a root", () => {
    const graph = graphOf(["big", "small", "tiny"], [], { big: { weight: 100 }, small: { weight: 10 }, tiny: { weight: 1 } });
    expect(radialLevels(graph, null).get("big")).toBe(0);
    expect(radialLevels(graph, null).get("tiny")).toBeGreaterThan(0);
  });

  it("rows a tree by steps from its root, no two nodes of a row on one place", () => {
    const graph = graphOf(["root", "a", "b", "c"], [["root", "a"], ["root", "b"], ["a", "c"]]);
    const places = treePositions(graph, "root");
    expect(places.get("a")!.y).toBe(places.get("b")!.y);
    expect(places.get("a")!.x).not.toBe(places.get("b")!.x);
    expect(places.get("c")!.y).toBeLessThan(places.get("a")!.y);
    expect(places.get("root")!.y).toBeGreaterThan(places.get("a")!.y);
  });

  it("lays every node out with the force layout", () => {
    const graph = graphOf(["a", "b", "c"], [["a", "b"], ["b", "c"]]);
    placeNodes(graph, new Map());
    const places = layoutPositions(graph, "force", { root: null, settled: false });
    expect([...places.keys()].sort()).toEqual(["a", "b", "c"]);
    for (const place of places.values()) expect(Number.isFinite(place.x) && Number.isFinite(place.y)).toBe(true);
  });
});

describe("mix", () => {
  it("fades a colour toward another and leaves what it cannot read", () => {
    expect(mix("#000000", "#ffffff", 0.5)).toBe("#808080");
    expect(mix("#2f4bd8", "#ffffff", 0)).toBe("#2f4bd8");
    expect(mix("oklch(0.5 0.1 200)", "#ffffff", 0.5)).toBe("oklch(0.5 0.1 200)");
  });
});
