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
import { boundsOf, drawnAt, laidOut, NODE_SIZE, SLOT_HEIGHT, SLOT_WIDTH, slotOf } from "./systems-layout";

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
  const full = () => ({
    ...spec(),
    edges: [
      ...spec().edges,
      { from: "triage", to: "billing", kind: "interacts" as const, description: "" },
      { from: "triage", to: "docs", kind: "uses_kg" as const, description: "" },
      { from: "boss", to: "billing", kind: "orchestrates" as const, description: "" },
      { from: "billing", to: "boss", kind: "escalates" as const, description: "" },
      { from: "curator", to: "triage", kind: "optimizes" as const, description: "" },
    ],
  });

  it("runs the flow left to right, the orchestrator and the Curator above it and the graphs below", () => {
    const at = new Map(laidOut(full()).nodes.map((node) => [node.id, node.position]));
    expect(at.get("entry")!.x).toBeLessThan(at.get("triage")!.x);
    expect(at.get("triage")!.x).toBeLessThan(at.get("billing")!.x);
    expect(at.get("entry")!.y).toBe(at.get("triage")!.y);
    expect(at.get("boss")!.y).toBeLessThan(at.get("triage")!.y);
    expect(at.get("curator")!.y).toBe(at.get("boss")!.y);
    expect(at.get("docs")!.y).toBeGreaterThan(at.get("triage")!.y);
  });

  it("puts the orchestrator over the agents it directs and a graph under its readers, never two on one place", () => {
    const at = new Map(laidOut(full()).nodes.map((node) => [node.id, node.position]));
    expect(at.get("boss")!.x).toBe(at.get("billing")!.x);
    expect(at.get("docs")!.x).toBe(at.get("triage")!.x);
    expect(Math.abs(at.get("curator")!.x - at.get("boss")!.x)).toBeGreaterThanOrEqual(SLOT_WIDTH);
  });

  it("bounds every slot of the laid-out spec, with room for the words around them", () => {
    const placed = laidOut(full());
    const box = boundsOf(placed);
    for (const node of placed.nodes) {
      expect(node.position.x).toBeGreaterThanOrEqual(box.x);
      expect(node.position.y).toBeGreaterThan(box.y);
      expect(node.position.x + SLOT_WIDTH).toBeLessThanOrEqual(box.x + box.width);
      expect(node.position.y + SLOT_HEIGHT).toBeLessThan(box.y + box.height);
    }
  });

  it("keeps a node's shape in the middle of its slot", () => {
    for (const kind of ["entry", "agent", "kg", "curator", "orchestrator"] as const) {
      const slot = { x: 40, y: -12 };
      expect(slotOf(kind, drawnAt(kind, slot))).toEqual(slot);
    }
    expect(drawnAt("agent", { x: 0, y: 0 })).toEqual({ x: (SLOT_WIDTH - NODE_SIZE.agent.width) / 2, y: (SLOT_HEIGHT - NODE_SIZE.agent.height) / 2 });
  });
});
