import { describe, expect, it } from "vitest";

import { parsePlan } from "@/lib/plans";

import { layoutPlanGraph } from "./layout";
import { arrowDirection, type NavigableNode, neighbour } from "./navigate";

const box = (key: string, row: number, x: number): NavigableNode => ({ key, row, x, width: 100 });

describe("neighbour", () => {
  // Row 0: a b c; row 1: d (under b), e (under c); row 2: f.
  const nodes = [box("a", 0, 0), box("b", 0, 120), box("c", 0, 240), box("d", 1, 110), box("e", 1, 250), box("f", 2, 0)];

  it("goes along a row with Left and Right, and stays at its ends", () => {
    expect(neighbour(nodes, "b", "right")).toBe("c");
    expect(neighbour(nodes, "b", "left")).toBe("a");
    expect(neighbour(nodes, "c", "right")).toBeNull();
    expect(neighbour(nodes, "a", "left")).toBeNull();
  });

  it("goes to the nearest step across in the row above or below", () => {
    expect(neighbour(nodes, "a", "down")).toBe("d");
    expect(neighbour(nodes, "c", "down")).toBe("e");
    expect(neighbour(nodes, "d", "up")).toBe("b");
    expect(neighbour(nodes, "e", "up")).toBe("c");
    expect(neighbour(nodes, "e", "down")).toBe("f");
    expect(neighbour(nodes, "a", "up")).toBeNull();
    expect(neighbour(nodes, "f", "down")).toBeNull();
  });

  it("breaks a tie towards the left, whatever the order of the steps", () => {
    const tie = [box("r", 1, 100), box("l", 1, -100), box("top", 0, 0)];
    expect(neighbour(tie, "top", "down")).toBe("l");
    expect(neighbour([...tie].reverse(), "top", "down")).toBe("l");
  });

  it("knows no move from a key that is not a step", () => {
    expect(neighbour(nodes, "zz", "down")).toBeNull();
  });

  it("walks the graph of a context-economy shaped plan", () => {
    const depends: Record<number, number[]> = { 3: [2], 5: [1], 6: [1, 2, 3, 4, 5], 7: [6], 8: [7], 9: [8], 10: [7, 9], 11: [10], 12: [11] };
    const steps = Array.from({ length: 12 }, (_, i) => ({ id: i + 1, depends_on: depends[i + 1] ?? [] }));
    const graph = layoutPlanGraph(parsePlan({ id: "p", steps }).steps);
    expect(neighbour(graph.nodes, "1", "right")).toBe("2");
    expect(neighbour(graph.nodes, "2", "right")).toBe("4");
    expect(neighbour(graph.nodes, "11", "down")).toBe("12");
    expect(neighbour(graph.nodes, "12", "up")).toBe("11");
    expect(neighbour(graph.nodes, "6", "down")).toBe("7");
  });
});

describe("arrowDirection", () => {
  it("names the four arrows and nothing else", () => {
    expect(["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Enter"].map(arrowDirection)).toEqual(["up", "down", "left", "right", null]);
  });
});
