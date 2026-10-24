import { describe, expect, it } from "vitest";

import { decisionsAbout, decisionSteps, decisionsToReview, decisionWhat, nodeDeprecation } from "./decisions";
import type { KgDecision } from "./types";

const decision = (number: number, extra: Partial<KgDecision> = {}): KgDecision => ({
  number,
  kind: "deprecate",
  state: "active",
  src: "app:file:a.py",
  rel: null,
  dst: null,
  history: [{ rev: number, action: "decide", by: "wendy", at: "2026-10-25T08:00:00Z", reason: "why", build_id: 7 }],
  ...extra,
});

describe("kg decisions", () => {
  it("decision lines read as the CLI prints them", () => {
    expect(decisionWhat(decision(1))).toBe("app:file:a.py");
    expect(decisionWhat(decision(2, { kind: "merge", dst: "app:file:b.py" }))).toBe("app:file:a.py = app:file:b.py");
    expect(decisionWhat(decision(3, { kind: "link", rel: "verifies", dst: "app:file:b.py" }))).toBe("app:file:a.py -verifies-> app:file:b.py");
    expect(decisionWhat(decision(4, { rel: "calls", dst: "app:file:b.py" }))).toBe("app:file:a.py -calls-> app:file:b.py");
  });

  it("decisions about a node name it as their subject or their other node", () => {
    const all = [decision(1), decision(2, { kind: "merge", src: "x", dst: "app:file:a.py" }), decision(3, { src: "y" })];
    expect(decisionsAbout(all, ["app:file:a.py"]).map((d) => d.number)).toEqual([1, 2]);
    expect(decisionsAbout(all, ["x", "y"]).map((d) => d.number)).toEqual([2, 3]);
    expect(decisionsAbout(all, [])).toEqual([]);
  });

  it("decision steps follow the hub's transitions", () => {
    expect(decisionSteps({ state: "proposed" })).toEqual(["accept", "reject"]);
    expect(decisionSteps({ state: "active" })).toEqual(["undo"]);
    expect(decisionSteps({ state: "orphan" })).toEqual(["undo"]);
    expect(decisionSteps({ state: "rejected" })).toEqual([]);
    expect(decisionSteps({ state: "undone" })).toEqual([]);
  });

  it("decisions to review are the proposals and the orphans", () => {
    const { proposed, orphans } = decisionsToReview([decision(1, { state: "proposed" }), decision(2), decision(3, { state: "orphan" })]);
    expect(proposed.map((d) => d.number)).toEqual([1]);
    expect(orphans.map((d) => d.number)).toEqual([3]);
  });

  it("reads a node's deprecation decision from its props, and nothing else", () => {
    expect(nodeDeprecation({ props: { deprecated: { reason: "moved", decision: 3 } } })).toEqual({ reason: "moved", decision: 3 });
    expect(nodeDeprecation({ props: {} })).toBeNull();
    expect(nodeDeprecation({ props: { deprecated: true } })).toBeNull();
    expect(nodeDeprecation({ props: { deprecated: { reason: 1, decision: "3" } } })).toBeNull();
  });
});
