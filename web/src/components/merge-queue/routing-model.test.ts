import { describe, expect, it } from "vitest";

import { modelProblem, modelSuggestions, readRouting, ROUTING_DEFAULTS, routingFormOf, routingProblems, routingSection, withRouting } from "./routing-model";

describe("the Token strategy", () => {
  it("reads each key at its value or its default, off by default", () => {
    expect(readRouting({})).toEqual(ROUTING_DEFAULTS);
    const routing = readRouting({ routing: { enabled: true, strategy: "cost-first", ladder: { easy: "sonnet" }, overflow: { easy: "zai/glm-4.6" }, hard_reserve_pct: "x" } });
    expect(routing).toMatchObject({ enabled: true, strategy: "cost-first", hard_reserve_pct: 10, ladder: { easy: "sonnet", medium: "sonnet", hard: "opus" } });
    expect(routing.overflow).toEqual({ easy: "zai/glm-4.6", medium: null, hard: null });
    expect(readRouting({ routing: { strategy: "newest" } }).strategy).toBe("surplus-first");
  });

  it("checks a model as the hub does: a runtime's name, or <prefix>/<model>, optional for the overflow", () => {
    expect(modelProblem("sonnet")).toBeNull();
    expect(modelProblem("zai/glm-4.6")).toBeNull();
    expect(modelProblem("")).toEqual({ code: "required" });
    expect(modelProblem("", true)).toBeNull();
    expect(modelProblem("Zai/glm")).toEqual({ code: "model", max: 200 });
    expect(modelProblem("a/b/c")).toBeNull();
    expect(modelProblem("zai//glm")).toEqual({ code: "model", max: 200 });
    expect(modelProblem("two words")).toEqual({ code: "model", max: 200 });
    expect(modelProblem("x".repeat(201))).toEqual({ code: "model", max: 200 });
  });

  it("finds the problems of the form by field", () => {
    const form = { ...routingFormOf(ROUTING_DEFAULTS), reservePct: "101", ceilingPct: "0", scorer: " ", ladder: { easy: "haiku", medium: "", hard: "opus" } };
    expect(routingProblems(form)).toEqual({
      reservePct: { code: "range", min: 0, max: 100 },
      ceilingPct: { code: "range", min: 1, max: 100 },
      scorer: { code: "required" },
      "ladder.medium": { code: "required" },
    });
    expect(routingProblems(routingFormOf(ROUTING_DEFAULTS))).toEqual({});
  });

  it("writes only what differs from the defaults, and keeps what the policy set and the form left", () => {
    const held = { routing: { strategy: "surplus-first", ladder: { hard: "opus" } } };
    const form = routingFormOf(readRouting(held));
    expect(routingSection(form, held)).toEqual(held.routing);
    const on = { ...form, enabled: true, strategy: "cost-first" as const, overflow: { easy: " zai/glm-4.6 ", medium: "", hard: "" } };
    expect(routingSection(on, held)).toEqual({ enabled: true, strategy: "cost-first", ladder: { hard: "opus" }, overflow: { easy: "zai/glm-4.6" } });
    expect(routingSection(routingFormOf(ROUTING_DEFAULTS), {})).toBeNull();
  });

  it("puts the section in the whole body, keeping every other section, or takes it out", () => {
    const policy = { merge: { pipeline_minutes: 90 }, routing: { enabled: true } };
    expect(withRouting(policy, { strategy: "quota-burn" })).toEqual({ merge: { pipeline_minutes: 90 }, routing: { strategy: "quota-burn" } });
    expect(withRouting(policy, null)).toEqual({ merge: { pipeline_minutes: 90 } });
    expect(policy.routing).toEqual({ enabled: true });
  });

  it("suggests the runtime's own models, then the project's gateway models, once each", () => {
    expect(modelSuggestions([{ model: "zai/glm-4.6" }, { model: "sonnet" }])).toEqual(["haiku", "sonnet", "opus", "zai/glm-4.6"]);
  });
});
