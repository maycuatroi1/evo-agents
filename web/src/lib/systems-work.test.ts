import { describe, expect, it } from "vitest";

import type { Spec, TraceSpan, TraceSummary } from "./systems";
import { edgeKey } from "./systems-observe";
import {
  agentName,
  barsOf,
  budgetShare,
  budgetTone,
  durationParts,
  filterCounts,
  filterOf,
  handoffs,
  lanesOf,
  matches,
  nameOfNode,
  nodeNames,
  pathOf,
  percentAt,
  scaleOf,
  ticksOf,
  type TraceTimeline,
  workOf,
  workSeconds,
} from "./systems-work";

const NOW = Date.parse("2026-11-07T10:00:00Z");
const at = (ms: number) => new Date(NOW + ms).toISOString();

const SPEC: Spec = {
  schema: "evo-agent-system/1",
  name: "Northwind desk",
  purpose: "",
  nodes: [
    { id: "entry", kind: "entry", position: { x: 0, y: 0 } },
    { id: "triage", kind: "agent", profile: "triage", version: 1, position: { x: 300, y: 0 } },
    { id: "billing", kind: "agent", profile: "billing", version: 1, position: { x: 600, y: 0 } },
    { id: "legal", kind: "agent", profile: "legal", version: 1, position: { x: 600, y: 200 } },
  ],
  edges: [
    { from: "entry", to: "triage", kind: "interacts", description: "" },
    { from: "triage", to: "billing", kind: "interacts", description: "" },
  ],
};

function trace(patch: Partial<TraceSummary>): TraceSummary {
  return {
    id: 1,
    system: "desk",
    revision: 1,
    chat_id: 1,
    message_id: 1,
    opened_by: "mai",
    started_at: at(-60_000),
    last_at: at(-10_000),
    runs: 1,
    off_design: 0,
    errors: 0,
    usd: 0,
    status: "running",
    ...patch,
  };
}

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

describe("filters", () => {
  const asked = (login: string) => trace({ id: 2, status: "needs_person", waiting: { kind: "decision", login, decision_id: 9, question: "Refund?" } });
  const traces = [trace({ id: 1 }), asked("mai"), asked("binh"), trace({ id: 4, status: "failed" }), trace({ id: 5, status: "done" })];

  it("counts each filter, needs you only for the decisions put to the viewer", () => {
    expect(filterCounts(traces, "mai")).toEqual({ all: 5, needs_you: 1, running: 1, failed: 1, done: 1 });
    expect(filterCounts(traces, null).needs_you).toBe(0);
    expect(matches(asked("mai"), "needs_you", "mai")).toBe(true);
    expect(matches(asked("mai"), "running", "mai")).toBe(false);
  });

  it("reads the filter and the work of the address, else all and none", () => {
    expect(filterOf("failed")).toBe("failed");
    expect(filterOf("nope")).toBe("all");
    expect(filterOf(null)).toBe("all");
    expect(workOf("12")).toBe(12);
    expect(workOf("-1")).toBeNull();
    expect(workOf("x")).toBeNull();
  });

  it("measures the budget share and the time of a work", () => {
    expect(budgetShare(0.5, 2)).toBe(0.25);
    expect(budgetShare(0.5, 0)).toBeNull();
    expect(workSeconds(trace({ status: "done" }), NOW)).toBe(50);
    expect(workSeconds(trace({ status: "running" }), NOW)).toBe(60);
  });
});

describe("handoffs and path", () => {
  it("lists the turns handed over and the asks refused, not a resumed turn", () => {
    const spans = [
      span({ node: "triage", from_node: null }),
      span({ node: "billing", from_node: "triage", agent: "billing" }),
      span({ node: "billing", from_node: "triage", agent: "billing", attributes: { "evo.resume_of_run_id": 4 } }),
      span({ name: "execute_tool", tool: "agent_ask", node: "triage", to_node: "legal", status: "refused", off_design: true, reason: "no edge" }),
    ];
    const found = handoffs(spans);
    expect(found.map((item) => [item.from, item.to, item.refused, item.offDesign])).toEqual([
      ["triage", "billing", false, false],
      ["triage", "legal", true, true],
    ]);
    expect(found[1].reason).toBe("no edge");
    const path = pathOf(SPEC, spans);
    expect([...path.nodes].sort()).toEqual(["billing", "entry", "legal", "triage"]);
    expect(path.edges.has(edgeKey("entry", "triage"))).toBe(true);
    expect(path.edges.has(edgeKey("triage", "legal"))).toBe(true);
  });
});

describe("timeline", () => {
  const timeline = {
    runs: [
      {
        span_id: 1,
        run_id: 10,
        agent: "triage",
        node: "triage",
        state: "done",
        segments: [
          { kind: "queued", started_at: at(-100_000), ended_at: at(-90_000) },
          { kind: "running", started_at: at(-90_000), ended_at: at(-50_000) },
        ],
      },
      {
        span_id: 2,
        run_id: 11,
        agent: "billing",
        node: "billing",
        state: "failed",
        segments: [{ kind: "running", started_at: at(-50_000), ended_at: at(-40_000) }],
      },
      {
        span_id: 3,
        run_id: 12,
        agent: "triage",
        node: "triage",
        state: "parked",
        segments: [
          { kind: "running", started_at: at(-40_000), ended_at: at(-20_000) },
          { kind: "parked", started_at: at(-20_000), ended_at: null },
        ],
      },
    ],
    marks: [
      { decision_id: 5, run_id: 12, agent: "triage", login: "mai", question: "Refund?", state: "open", asked_at: at(-30_000), answered_at: null },
    ],
  } satisfies Required<TraceTimeline>;

  it("covers the first segment to now and places the bars", () => {
    const scale = scaleOf(timeline, NOW);
    expect(scale).toEqual({ start: NOW - 100_000, end: NOW });
    if (!scale) return;
    expect(percentAt(scale, NOW - 50_000)).toBe(50);
    const [queued, running] = barsOf(timeline.runs[0], scale, NOW);
    expect([queued.kind, queued.left, queued.width]).toEqual(["queued", 0, 10]);
    expect([running.kind, running.left, running.width]).toEqual(["done", 10, 40]);
    expect(barsOf(timeline.runs[1], scale, NOW)[0].kind).toBe("failed");
    const parked = barsOf(timeline.runs[2], scale, NOW);
    expect(parked.map((bar) => [bar.kind, bar.open])).toEqual([
      ["running", false],
      ["parked", true],
    ]);
    expect(parked[1].left + parked[1].width).toBe(100);
    expect(ticksOf(scale, 4)).toHaveLength(5);
  });

  it("gives a lane per agent, and no scale for an empty timeline", () => {
    expect(lanesOf(timeline).map((lane) => [lane.agent, lane.runs.length])).toEqual([
      ["triage", 2],
      ["billing", 1],
    ]);
    expect(scaleOf({ runs: [], marks: [] }, NOW)).toBeNull();
  });
});

describe("names and amounts", () => {
  const names = new Map([["lumen-triage", "Lumen Triage"]]);
  const spec = {
    nodes: [
      { id: "entry", kind: "entry", position: { x: 0, y: 0 } },
      { id: "triage", kind: "agent", position: { x: 1, y: 0 }, profile: "lumen-triage" },
      { id: "billing", kind: "agent", position: { x: 2, y: 0 }, profile: "lumen-billing", label: "Billing desk" },
      { id: "legal", kind: "agent", position: { x: 3, y: 0 }, profile: "lumen-legal" },
    ],
    edges: [],
  } as unknown as Spec;

  it("names agents and nodes by what a person knows them by", () => {
    expect(agentName("lumen-triage", names)).toBe("Lumen Triage");
    expect(agentName("@lumen-triage", names)).toBe("Lumen Triage");
    expect(agentName("lumen-other", names)).toBe("@lumen-other");
    expect(agentName(null, names)).toBe("");
    const nodes = nodeNames(spec, names);
    expect([...nodes.values()]).toEqual(["entry", "Lumen Triage", "Billing desk", "@lumen-legal"]);
    expect(nameOfNode("triage", nodes, names)).toBe("Lumen Triage");
    expect(nameOfNode("@lumen-triage", nodes, names)).toBe("Lumen Triage");
  });

  it("splits a duration and tones a budget", () => {
    expect(durationParts(3725.4)).toEqual({ hours: 1, minutes: 2, seconds: 5 });
    expect(durationParts(-3)).toEqual({ hours: 0, minutes: 0, seconds: 0 });
    expect([budgetTone(0.5), budgetTone(0.8), budgetTone(1), budgetTone(1.4)]).toEqual(["normal", "warning", "danger", "danger"]);
  });
});
