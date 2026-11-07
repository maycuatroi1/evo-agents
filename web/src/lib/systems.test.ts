import { describe, expect, it } from "vitest";

import {
  canAdd,
  freeId,
  historyOf,
  joins,
  newSpec,
  outdated,
  placeProblems,
  push,
  redo,
  sameSpec,
  slugOf,
  slugProblem,
  type Spec,
  specBody,
  specOf,
  undo,
  withNode,
  withoutNode,
  worst,
} from "./systems";
import { laidOut } from "./systems-layout";

const spec = (): Spec => ({
  schema: "evo-agent-system/1",
  name: "Support",
  purpose: "",
  nodes: [
    { id: "entry", kind: "entry", position: { x: 0, y: 0 } },
    { id: "triage", kind: "agent", profile: "triage", version: 2, position: { x: 300, y: 0 } },
    { id: "billing", kind: "agent", profile: "billing", version: 1, position: { x: 600, y: 0 } },
    { id: "docs", kind: "kg", graph: "default", position: { x: 600, y: 200 } },
    { id: "boss", kind: "orchestrator", profile: "boss", version: 1, position: { x: 300, y: 300 } },
    { id: "curator", kind: "curator", position: { x: 0, y: 300 } },
  ],
  edges: [{ from: "entry", to: "triage", kind: "interacts", description: "" }],
});

describe("joins", () => {
  it("offers only the edge kinds the two node kinds take, in their direction", () => {
    expect(joins(spec(), "triage", "billing")).toEqual([
      { kind: "interacts", from: "triage", to: "billing" },
      { kind: "interacts", from: "billing", to: "triage" },
    ]);
    expect(joins(spec(), "triage", "docs")).toEqual([{ kind: "uses_kg", from: "triage", to: "docs" }]);
    // A drag from the graph to the agent is the agent using the graph.
    expect(joins(spec(), "docs", "billing")).toEqual([{ kind: "uses_kg", from: "billing", to: "docs" }]);
    expect(joins(spec(), "boss", "billing")).toEqual([
      { kind: "orchestrates", from: "boss", to: "billing" },
      { kind: "escalates", from: "billing", to: "boss" },
    ]);
    expect(joins(spec(), "curator", "docs")).toEqual([{ kind: "optimizes", from: "curator", to: "docs" }]);
  });

  it("refuses pairs no edge kind joins, a node to itself, a drawn edge and a second agent for the entry", () => {
    expect(joins(spec(), "entry", "docs")).toEqual([]);
    expect(joins(spec(), "curator", "boss")).toEqual([]);
    expect(joins(spec(), "triage", "triage")).toEqual([]);
    expect(joins(spec(), "entry", "billing")).toEqual([]);
    expect(joins(spec(), "entry", "triage")).toEqual([]);
  });
});

describe("spec", () => {
  it("reads the API's mapping and writes only the keys of the schema", () => {
    const read = specOf({ ...spec(), slug: "support", nodes: [...spec().nodes, "junk"] });
    expect(read.nodes).toHaveLength(6);
    const body = specBody(read) as { nodes: Record<string, unknown>[] };
    expect(body).not.toHaveProperty("slug");
    expect(body.nodes[0]).toEqual({ id: "entry", kind: "entry", position: { x: 0, y: 0 } });
    expect(body.nodes[1]).toEqual({ id: "triage", kind: "agent", position: { x: 300, y: 0 }, profile: "triage", version: 2 });
    expect(body.nodes[3]).toEqual({ id: "docs", kind: "kg", position: { x: 600, y: 200 }, graph: "default" });
  });

  it("names, ids and single kinds", () => {
    expect(slugOf("Hỗ trợ khách hàng!")).toBe("ho-tro-khach-hang");
    expect(slugProblem("validate")).toBe("reserved");
    expect(slugProblem("Bad Slug")).toBe("pattern");
    expect(slugProblem("support")).toBeNull();
    expect(freeId(spec(), "triage")).toBe("triage-2");
    expect(canAdd(spec(), "orchestrator")).toBe(false);
    expect(canAdd(spec(), "agent")).toBe(true);
    expect(newSpec(" Sales ").nodes.map((node) => node.kind)).toEqual(["entry"]);
  });

  it("drops a node with its edges", () => {
    const left = withoutNode(spec(), "triage");
    expect(left.nodes.map((node) => node.id)).not.toContain("triage");
    expect(left.edges).toEqual([]);
  });
});

describe("problems and pins", () => {
  it("places each problem on its edge, its node or the system", () => {
    const placed = placeProblems([
      { level: "error", path: "edges[0]", message: "x", edge: 0, node: null },
      { level: "warning", path: "nodes[1]", message: "y", node: "triage" },
      { level: "warning", path: "name", message: "z" },
    ]);
    expect(placed.edges.get(0)).toHaveLength(1);
    expect(worst(placed.nodes.get("triage"))).toBe("warning");
    expect(placed.system).toHaveLength(1);
    expect(worst([])).toBeNull();
  });

  it("finds a pin older than the profile's latest version", () => {
    const latest = new Map([["triage", 3]]);
    expect(outdated(spec().nodes[1], latest)).toBe(3);
    expect(outdated(spec().nodes[2], latest)).toBeNull();
    expect(outdated(spec().nodes[3], latest)).toBeNull();
  });
});

describe("history", () => {
  it("undoes and redoes, and keeps no step that changes nothing", () => {
    const start = historyOf(spec());
    expect(push(start, spec())).toBe(start);
    const moved = push(start, withNode(spec(), "triage", { label: "Triage" }));
    expect(moved.past).toHaveLength(1);
    const back = undo(moved);
    expect(sameSpec(back.present, spec())).toBe(true);
    expect(redo(back).present.nodes[1].label).toBe("Triage");
    expect(undo(start)).toBe(start);
  });
});

describe("layout", () => {
  it("puts the entry before its agent and the agent before its graph", () => {
    const withGraph = { ...spec(), edges: [...spec().edges, { from: "triage", to: "docs", kind: "uses_kg" as const, description: "" }] };
    const at = new Map(laidOut(withGraph).nodes.map((node) => [node.id, node.position]));
    expect(at.get("entry")!.x).toBeLessThan(at.get("triage")!.x);
    expect(at.get("triage")!.x).toBeLessThan(at.get("docs")!.x);
  });
});
