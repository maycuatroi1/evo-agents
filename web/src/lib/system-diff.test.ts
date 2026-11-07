import { describe, expect, it } from "vitest";

import { specDiff } from "./system-diff";
import type { Spec } from "./systems";

const saved: Spec = {
  schema: "evo-agent-system/1",
  name: "Northwind support",
  purpose: "",
  nodes: [
    { id: "in", kind: "entry", position: { x: 0, y: 0 } },
    { id: "triage", kind: "agent", profile: "triage", version: 1, position: { x: 240, y: 0 } },
    { id: "review", kind: "agent", profile: "reviewer", version: 1, position: { x: 480, y: 0 } },
  ],
  edges: [
    { from: "in", to: "triage", kind: "interacts", description: "" },
    { from: "triage", to: "review", kind: "interacts", description: "a second look" },
  ],
};

describe("specDiff", () => {
  it("marks what a proposal adds, drops and changes, and keeps a move alone unmarked", () => {
    const proposed: Spec = {
      ...saved,
      nodes: [
        { id: "in", kind: "entry", position: { x: 10, y: 10 } },
        { id: "triage", kind: "agent", profile: "triage", version: 2, position: { x: 240, y: 0 } },
        { id: "docs", kind: "kg", graph: "lumen-docs", position: { x: 240, y: 160 } },
      ],
      edges: [
        { from: "in", to: "triage", kind: "interacts", description: "" },
        { from: "triage", to: "docs", kind: "uses_kg", description: "" },
      ],
    };
    const diff = specDiff(saved, proposed);
    expect(diff.nodes.map(({ node, ghost }) => [node.id, ghost])).toEqual([
      ["in", null],
      ["triage", "changed"],
      ["docs", "added"],
      ["review", "removed"],
    ]);
    expect(diff.nodes[0]?.node.position).toEqual({ x: 10, y: 10 });
    expect(diff.edges.map(({ edge, ghost }) => [`${edge.from}>${edge.to}`, ghost])).toEqual([
      ["in>triage", null],
      ["triage>docs", "added"],
      ["triage>review", "removed"],
    ]);
    expect(diff.counts).toEqual({ added: 1, removed: 1, changed: 1, addedEdges: 1, removedEdges: 1 });
  });

  it("finds nothing between a spec and itself", () => {
    expect(specDiff(saved, saved).counts).toEqual({ added: 0, removed: 0, changed: 0, addedEdges: 0, removedEdges: 0 });
  });
});
