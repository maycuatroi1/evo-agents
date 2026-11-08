import { describe, expect, it } from "vitest";

import { BEND_STEP, bendsOf, focusOf, type Line } from "./systems-canvas";

const lines: Line[] = [
  { id: "e0", source: "entry", target: "front" },
  { id: "e1", source: "boss", target: "front" },
  { id: "e2", source: "front", target: "boss" },
  { id: "e3", source: "front", target: "docs" },
];

describe("bends", () => {
  it("draws a line alone straight and bows two lines between the same nodes to opposite sides", () => {
    const bends = bendsOf(lines);
    expect(bends.get("e0")).toBe(0);
    expect(bends.get("e3")).toBe(0);
    // Opposite directions: the same signed bend lies on opposite sides, since each line's left is its own.
    expect(Math.abs(bends.get("e1")!)).toBe(BEND_STEP / 2);
    expect(bends.get("e1")).toBe(bends.get("e2"));
  });

  it("spreads lines that run the same way to both sides", () => {
    const bends = bendsOf([
      { id: "a", source: "x", target: "y" },
      { id: "b", source: "x", target: "y" },
    ]);
    expect(bends.get("a")).toBe(-bends.get("b")!);
    expect(bends.get("a")).not.toBe(0);
  });
});

describe("focus", () => {
  it("brings a node's lines and their other ends into focus", () => {
    const focus = focusOf(lines, { node: "boss" })!;
    expect([...focus.lines].sort()).toEqual(["e1", "e2"]);
    expect([...focus.nodes].sort()).toEqual(["boss", "front"]);
  });

  it("brings a line and its two ends into focus, and nothing for no pointer or a line not drawn", () => {
    expect(focusOf(lines, { line: "e3" })).toEqual({ nodes: new Set(["front", "docs"]), lines: new Set(["e3"]) });
    expect(focusOf(lines, null)).toBeNull();
    expect(focusOf(lines, { line: "gone" })).toBeNull();
  });
});
