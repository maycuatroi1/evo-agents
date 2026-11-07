import { describe, expect, it } from "vitest";

import type { Spec, TraceSpan, TraceSummary } from "./systems";
import { edgeActivity, ghostNodes, graphQueries, LIT_MS, metricsOf, nodeStates, treeOrder, waterfall, withTrace } from "./systems-observe";

const NOW = Date.parse("2026-11-07T10:00:00Z");
const at = (ms: number) => new Date(NOW + ms).toISOString();

const SPEC: Spec = {
  schema: "evo-agent-system/1",
  name: "Support desk",
  purpose: "",
  nodes: [
    { id: "entry", kind: "entry", position: { x: 0, y: 0 } },
    { id: "triage", kind: "agent", profile: "triage", version: 1, position: { x: 300, y: 0 } },
    { id: "billing", kind: "agent", profile: "billing", version: 1, position: { x: 600, y: 0 } },
    { id: "docs", kind: "kg", graph: "docs", position: { x: 600, y: 200 } },
  ],
  edges: [
    { from: "entry", to: "triage", kind: "interacts", description: "" },
    { from: "triage", to: "billing", kind: "interacts", description: "" },
    { from: "triage", to: "docs", kind: "uses_kg", description: "" },
  ],
};

let nextId = 1;
function span(patch: Partial<TraceSpan>): TraceSpan {
  return {
    id: nextId++,
    parent_id: null,
    name: "invoke_agent",
    tool: null,
    agent: "triage",
    node: "triage",
    from_node: null,
    to_node: null,
    run_id: null,
    project: null,
    runtime: null,
    status: "ok",
    off_design: false,
    reason: null,
    message_id: null,
    attributes: {},
    started_at: at(-60_000),
    ended_at: at(-50_000),
    seconds: 10,
    input_tokens: 0,
    output_tokens: 0,
    usd: 0,
    model: null,
    run_state: null,
    content_withheld: false,
    ...patch,
  };
}

describe("nodeStates", () => {
  it("says working, waiting, failed and idle from the newest runs of each node", () => {
    const spans = [
      span({ node: "triage", from_node: "entry", status: "running", run_state: "running", ended_at: null, started_at: at(-2_000) }),
      span({ node: "billing", from_node: "triage", status: "running", run_state: "parked", ended_at: null }),
    ];
    const states = nodeStates(SPEC, spans, NOW);
    expect(states.get("triage")).toBe("working");
    expect(states.get("billing")).toBe("waiting");
    expect(states.get("entry")).toBe("working");
    expect(states.get("docs")).toBe("idle");
  });

  it("says failed only while the node's latest run failed", () => {
    const failed = span({ node: "billing", status: "error", started_at: at(-30_000), ended_at: at(-29_000) });
    const earlier = span({ node: "billing", status: "ok", started_at: at(-90_000), ended_at: at(-80_000) });
    expect(nodeStates(SPEC, [earlier, failed], NOW).get("billing")).toBe("failed");
    const later = span({ node: "billing", status: "ok", started_at: at(-10_000), ended_at: at(-9_000) });
    expect(nodeStates(SPEC, [earlier, failed, later], NOW).get("billing")).toBe("idle");
    expect(nodeStates(SPEC, [failed], NOW).get("entry")).toBe("idle");
  });
});

describe("edgeActivity", () => {
  it("counts edges between agents from the metrics, kg edges from the queries, and lights recent crossings", () => {
    const metrics = metricsOf({ nodes: [], edges: [{ from_node: "triage", to_node: "billing", count: 7, off_design: 0 }] });
    const spans = [
      span({ node: "billing", from_node: "triage", status: "running", ended_at: null, started_at: at(-1_000) }),
      span({ name: "execute_tool", tool: "kg_search", node: "triage", attributes: { "gen_ai.data_source.id": "docs" }, started_at: at(-LIT_MS * 3), ended_at: at(-LIT_MS * 3) }),
      span({ name: "execute_tool", tool: "kg_search", node: "triage", attributes: { "gen_ai.data_source.id": "docs" }, started_at: at(-1_000), ended_at: at(-900) }),
    ];
    const { edges, extra } = edgeActivity(SPEC, metrics, spans, NOW);
    expect(edges.get(1)).toEqual({ count: 7, lit: true, offDesign: 0 });
    expect(edges.get(2)).toEqual({ count: 2, lit: true, offDesign: 0 });
    expect(edges.get(0)).toEqual({ count: 0, lit: false, offDesign: 0 });
    expect(extra).toEqual([]);
    expect(graphQueries(SPEC, spans).get("docs")).toBe(2);
    expect(nodeStates(SPEC, spans, NOW).get("docs")).toBe("working");
  });

  it("draws a crossing the spec has no edge for as an off-design edge, to a ghost node when it reaches outside", () => {
    const metrics = metricsOf({ nodes: [], edges: [{ from_node: "billing", to_node: "@legal", count: 2, off_design: 2 }] });
    const spans = [span({ name: "execute_tool", tool: "agent_ask", node: "billing", to_node: "@outside", status: "refused", off_design: true, started_at: at(-60_000), ended_at: at(-60_000) })];
    const { extra } = edgeActivity(SPEC, metrics, spans, NOW);
    expect(extra).toEqual([
      { from: "billing", to: "@outside", count: 1, lit: false, offDesign: 1 },
      { from: "billing", to: "@legal", count: 2, lit: false, offDesign: 2 },
    ]);
    expect(ghostNodes(SPEC, extra).map((ghost) => ghost.id)).toEqual(["@outside", "@legal"]);
  });

  it("crosses no edge for a turn that goes on in a session", () => {
    const spans = [span({ node: "billing", from_node: "triage", attributes: { "evo.resume_of_run_id": 3 }, started_at: at(-1_000) })];
    expect(edgeActivity(SPEC, metricsOf(null), spans, NOW).edges.get(1)).toEqual({ count: 0, lit: false, offDesign: 0 });
  });
});

describe("the trace's waterfall", () => {
  it("orders spans by their tree and places each bar on the trace's time", () => {
    const root = span({ started_at: at(0), ended_at: at(10_000) });
    const child = span({ parent_id: root.id, name: "execute_tool", tool: "agent_ask", started_at: at(2_000), ended_at: at(2_000) });
    const grandchild = span({ parent_id: child.id, node: "billing", started_at: at(5_000), ended_at: null, status: "running" });
    const order = treeOrder([grandchild, root, child]);
    expect(order.map(({ span: one, depth }) => [one.id, depth])).toEqual([
      [root.id, 0],
      [child.id, 1],
      [grandchild.id, 2],
    ]);
    const bars = waterfall([root, child, grandchild], NOW + 20_000);
    expect(bars.get(root.id)).toEqual({ left: 0, width: 50 });
    expect(bars.get(child.id)?.left).toBe(10);
    expect(bars.get(grandchild.id)).toEqual({ left: 25, width: 75 });
  });
});

describe("withTrace", () => {
  const summary = (id: number, runs: number): TraceSummary => ({ id, system: "desk", revision: 1, chat_id: null, message_id: null, opened_by: null, started_at: at(0), last_at: at(0), runs, off_design: 0, errors: 0, usd: 0 });
  it("puts a moved trace in its place, a new one first, and keeps the list's length", () => {
    expect(withTrace([summary(2, 1), summary(1, 1)], summary(2, 3), 20).map((trace) => [trace.id, trace.runs])).toEqual([
      [2, 3],
      [1, 1],
    ]);
    expect(withTrace([summary(2, 1), summary(1, 1)], summary(3, 1), 2).map((trace) => trace.id)).toEqual([3, 2]);
    expect(withTrace(undefined, summary(1, 1), 20)).toHaveLength(1);
  });
});
