import { describe, expect, it } from "vitest";

import { parsePlan } from "@/lib/plans";

import { layoutPlanGraph } from "./layout";
import { OPENING_MIN_ZOOM, openingViewport } from "./viewport";

const chain = (length: number) =>
  layoutPlanGraph(parsePlan({ id: "p", steps: Array.from({ length }, (_, i) => ({ id: i + 1, depends_on: i ? [i] : [] })) }).steps);
const wide = (length: number) =>
  layoutPlanGraph(parsePlan({ id: "p", steps: Array.from({ length }, (_, i) => ({ id: i + 1 })) }).steps);

describe("openingViewport", () => {
  it("fits a small plan whole, centred, never past zoom 1", () => {
    const graph = chain(2);
    const view = openingViewport(graph, { width: 1000, height: 640 });
    expect(view.zoom).toBe(1);
    expect(view.x).toBeCloseTo((1000 - graph.width) / 2);
    expect(view.y).toBeCloseTo((640 - graph.height) / 2);
  });

  it("fits a plan that still reads at the floor", () => {
    const graph = chain(9);
    const view = openingViewport(graph, { width: 1000, height: 640 });
    expect(view.zoom).toBeGreaterThanOrEqual(OPENING_MIN_ZOOM);
    expect(view.zoom).toBeLessThan(1);
    expect(graph.height * view.zoom).toBeLessThanOrEqual(640);
  });

  it("opens a tall plan at the floor with its first row at the top", () => {
    const graph = chain(30);
    const view = openingViewport(graph, { width: 1000, height: 640 });
    expect(view.zoom).toBe(OPENING_MIN_ZOOM);
    expect(view.y).toBeCloseTo(640 * 0.06);
    expect(view.x).toBeCloseTo((1000 - graph.width * OPENING_MIN_ZOOM) / 2);
  });

  it("keeps a wide plan's drawing on the canvas, starting from its first step", () => {
    const graph = wide(12);
    const frame = { width: 375, height: 512 };
    const view = openingViewport(graph, frame);
    expect(view.zoom).toBe(OPENING_MIN_ZOOM);
    expect(view.x).toBeLessThanOrEqual(frame.width * 0.06);
    expect(view.x + graph.width * view.zoom).toBeGreaterThanOrEqual(frame.width * 0.94 - 0.001);
    // Step 1 is on the canvas.
    const first = graph.nodes[0];
    expect(view.x + first.x * view.zoom).toBeGreaterThanOrEqual(0);
    expect(view.x + (first.x + first.width) * view.zoom).toBeLessThanOrEqual(frame.width);
  });

  it("an empty plan opens at zoom 1", () => {
    expect(openingViewport(wide(0), { width: 800, height: 600 }).zoom).toBe(1);
  });
});
