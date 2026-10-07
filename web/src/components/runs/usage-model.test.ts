import { describe, expect, it } from "vitest";

import claudeCode from "@/test/fixtures/trace/claude-code.json";
import codex from "@/test/fixtures/trace/codex.json";
import fakeClaudeCode from "@/test/fixtures/trace/fake-claude-code.json";
import opencode from "@/test/fixtures/trace/opencode.json";

import type { RunEvent } from "./queries";
import { readRunUsage, readTokens, runUsage, shares, totalTokens, usageFromEvents } from "./usage-model";

/** The fixtures hold each runtime's usage reports of a real run and the usage its worker reported with the run's end. */
type Fixture = { usage: Record<string, unknown> | null; events: RunEvent[] };

describe("usage from the runtimes' reports", () => {
  it.each([
    ["claude-code", claudeCode],
    ["opencode", opencode],
    ["codex", codex],
  ] as [string, Fixture][])("adds up %s's reports to what its worker reports for the whole run", (_name, fixture) => {
    expect(usageFromEvents(fixture.events)).toEqual(readRunUsage(fixture.usage));
  });

  it("adds up Claude Code's turns and takes its latest cost, the session's so far", () => {
    expect(usageFromEvents(claudeCode.events as RunEvent[])).toEqual({
      cacheRead: 70839 + 67925,
      input: 12,
      output: 361,
      reasoning: 0,
      cacheWrite: 23515 + 554,
      cost: { amount: 0.22757280000000002, currency: "USD" },
    });
  });

  it("adds up opencode's steps, cost included, with reasoning apart from output", () => {
    expect(usageFromEvents(opencode.events as RunEvent[])).toEqual({
      cacheRead: 283776,
      input: 94944,
      output: 103,
      reasoning: 109,
      cacheWrite: 0,
      cost: { amount: 0, currency: "USD" },
    });
  });

  it("takes Codex's latest total, its cached input out of input, and reports no cost", () => {
    const figures = usageFromEvents(codex.events as RunEvent[]);
    expect(figures).toEqual({ cacheRead: 102016, input: 125097 - 102016, output: 131, reasoning: 0, cacheWrite: 0, cost: null });
    expect(totalTokens(figures!)).toBe(125228); // Codex's own totalTokens: input and output
  });

  it("reads the fake adapter's Claude Code result, whose cost has no currency, as dollars", () => {
    expect(usageFromEvents(fakeClaudeCode.events as RunEvent[])).toMatchObject({ input: 4, output: 405, cost: { amount: 0.2041276, currency: "USD" } });
  });

  it("counts Claude Code's thinking and Codex's reasoning as parts of their output", () => {
    expect(readTokens({ input_tokens: 5, output_tokens: 300, output_tokens_details: { thinking_tokens: 120 } })).toMatchObject({ output: 180, reasoning: 120 });
    expect(readTokens({ inputTokens: 10, cachedInputTokens: 4, outputTokens: 50, reasoningOutputTokens: 20 })).toMatchObject({
      cacheRead: 4,
      input: 6,
      output: 30,
      reasoning: 20,
    });
  });

  it("reads nothing from a body of no known shape, and no report as none", () => {
    expect(readTokens({ speed: "standard" })).toBeNull();
    expect(readRunUsage(null)).toBeNull();
    expect(usageFromEvents([{ seq: 1, kind: "usage_update", body: { note: "x" } }])).toBeNull();
    expect(usageFromEvents([])).toBeNull();
  });
});

describe("runUsage", () => {
  it("prefers the run's own usage once reported, and its events while it runs", () => {
    const events = opencode.events as RunEvent[];
    expect(runUsage({ usage: null }, events)?.source).toBe("events");
    expect(runUsage({ usage: { input_tokens: 41200, output_tokens: 6800 } }, events)).toEqual({
      source: "run",
      figures: { cacheRead: 0, input: 41200, output: 6800, reasoning: 0, cacheWrite: 0, cost: null },
    });
    expect(runUsage({ usage: null }, [])).toBeNull();
  });

  it("says each part's share of the tokens, to one decimal", () => {
    const figures = { cacheRead: 93056, input: 341, output: 216, reasoning: 100, cacheWrite: 0, cost: null };
    expect(totalTokens(figures)).toBe(93713);
    expect(shares(figures)).toEqual({ cacheRead: 99.3, input: 0.4, output: 0.2, reasoning: 0.1 });
    expect(shares({ ...figures, cacheRead: 0, input: 0, output: 0, reasoning: 0 })).toEqual({ cacheRead: 0, input: 0, output: 0, reasoning: 0 });
  });
});
