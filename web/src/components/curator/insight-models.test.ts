import { describe, expect, it } from "vitest";

import { buildRows, coverageOf as buildCoverage, evalOf, measureRows, measureTotals, retrievalOf, sourcesOf, syncOf } from "./graph-health-model";
import { coverageOf, evidenceRef, facetNumbers, historySearch, markedParts, NO_HISTORY_FILTERS, readHistoryFilters, searchQueryOf, sessionsQueryOf } from "./history-model";
import type { KgHealthView } from "./insight-queries";
import { armPassed, armTotals, lowered, verdictOf } from "./replay-model";
import { countedNights, readRuntimeRange, runtimeNight, runtimeTrends, slowestHook } from "./runtime-model";
import { hasValues, latest, sparkValues, stackEnds, stackTotal } from "./trend-model";

const label = (at: string) => ({ tick: at.slice(5, 10), title: at });

describe("the History page's filters", () => {
  it("reads the URL's search and filters, keeping only what the API takes", () => {
    const params = new URLSearchParams("q=flaky test&source=run&source=transcript&source=mail&hits=3&kind=run&outcome=failed&cluster=fix/FM-3.2&page=2");
    expect(readHistoryFilters(params)).toEqual({
      q: "flaky test",
      sources: ["transcript", "run"],
      hits: 3,
      kind: "run",
      outcome: "failed",
      cluster: "fix/FM-3.2",
      page: 2,
    });
    expect(readHistoryFilters(new URLSearchParams("kind=chat&outcome=won&cluster=Fix/x&page=0&hits=999"))).toEqual(NO_HISTORY_FILTERS);
  });

  it("writes them back in a stable order and asks the API for the page they name", () => {
    expect(historySearch(NO_HISTORY_FILTERS)).toBe("");
    const filters = { ...NO_HISTORY_FILTERS, q: "  timeout ", sources: ["chat" as const], hits: 2, cluster: "debug/none", page: 3 };
    expect(historySearch(filters)).toBe("?q=timeout&source=chat&hits=2&cluster=debug%2Fnone&page=3");
    expect(searchQueryOf(filters)).toEqual({ q: "timeout", sources: ["chat"], offset: 20 });
    expect(sessionsQueryOf(filters)).toEqual({ kind: null, outcome: null, cluster: "debug/none", offset: 50 });
  });

  it("marks the words the query matched", () => {
    expect(markedParts("ran «pytest» then «tail» again")).toEqual([
      { text: "ran ", marked: false },
      { text: "pytest", marked: true },
      { text: " then ", marked: false },
      { text: "tail", marked: true },
      { text: " again", marked: false },
    ]);
    expect(markedParts("no mark «open")).toEqual([{ text: "no mark «open", marked: false }]);
    expect(markedParts("")).toEqual([]);
  });

  it("reads what a piece of evidence names", () => {
    expect(evidenceRef("session:0199a3c1-0000")).toEqual({ kind: "session", session: "0199a3c1-0000" });
    expect(evidenceRef("transcript:abc:12")).toEqual({ kind: "transcript", session: "abc", file: "main", line: 12 });
    expect(evidenceRef("transcript:abc:subagents/agent-1.jsonl:7")).toEqual({ kind: "transcript", session: "abc", file: "subagents/agent-1.jsonl", line: 7 });
    expect(evidenceRef("run:42:3")).toEqual({ kind: "run", run: 42, seq: 3 });
    expect(evidenceRef("run:42")).toBeNull();
    expect(evidenceRef("transcript:abc:0")).toBeNull();
    expect(evidenceRef("code:api:README.md")).toBeNull();
    expect(evidenceRef(null)).toBeNull();
  });

  it("says how far the hub holds what the machines reported", () => {
    expect(coverageOf({ sessions: 30, analyzed: 28, waiting: 2, from_digests: 10, runs: 5, runs_analyzed: 5, failed: 1 })).toEqual({
      held: 30,
      reported: 40,
      waiting: 10,
      share: 0.75,
      analyzed: 33,
      analyzable: 35,
      failed: 1,
    });
    expect(coverageOf({})).toMatchObject({ reported: 0, share: null });
    expect(coverageOf(null)).toBeNull();
    expect(facetNumbers({ messages: 12, tool_errors: 2, cost_usd: 0.42, corrections: "x" })).toEqual({ messages: 12, toolErrors: 2, corrections: 0, cost: 0.42 });
  });
});

const HEALTH: KgHealthView = {
  project: "demo",
  measures: [
    {
      measured_at: "2026-10-19T06:00:00Z",
      build_id: 9,
      error: "GitHub did not answer",
      build: null,
      sources: [],
      eval: null,
    },
    {
      measured_at: "2026-10-19T00:00:00Z",
      build_id: 9,
      error: null,
      build: { id: 9 },
      sources: [
        {
          source: "api",
          repo: "api",
          commit: "a".repeat(40),
          default_branch: "main",
          head: "b".repeat(40),
          behind: 3,
          lag_hours: 5.5,
          missing: 2,
          stale: 1,
          deleted: 0,
          samples: { missing: ["src/new.py", "src/other.py"], stale: ["README.md"], deleted: [] },
          evidence: ["code:api:src/new.py"],
          why: null,
        },
        { source: "docs", repo: null, commit: null, behind: null, lag_hours: null, missing: null, why: "a path source" },
      ],
      eval: { questions: 40, methods: { kg_search: { "recall@1": 0.4, "recall@5": 0.65, mrr: 0.5 } } },
    },
    {
      measured_at: "2026-10-18T18:00:00Z",
      build_id: 8,
      error: null,
      build: { id: 8 },
      sources: [{ source: "api", behind: 7, missing: 4, stale: 0, deleted: 1 }],
      eval: { why: "the project has no graph on the hub yet" },
    },
  ],
  builds: [
    { id: 9, finished_at: "2026-10-18T23:00:00Z", changed: true, coverage: { mentions: 100, resolved_ratio: 0.8, ambiguous_ratio: 0.15, dangling_ratio: 0.05, python: { uncalled_method_ratio: 0.3 } } },
    { id: 8, finished_at: "2026-10-18T11:00:00Z", changed: false, coverage: null },
  ],
};

describe("the knowledge graph's health", () => {
  it("reads each source of a measure and what they add up to", () => {
    const sources = sourcesOf(HEALTH.measures[1]);
    expect(sources[0]).toMatchObject({ source: "api", behind: 3, lagHours: 5.5, missing: 2, stale: 1, deleted: 0, why: null });
    expect(sources[0].samples.missing).toEqual(["src/new.py", "src/other.py"]);
    expect(sources[1]).toMatchObject({ source: "docs", behind: null, missing: null, why: "a path source" });
    expect(measureTotals(sources)).toEqual({ behind: 3, off: 3, lagHours: 5.5 });
    expect(measureTotals([])).toEqual({ behind: null, off: null, lagHours: null });
    expect(sourcesOf(null)).toEqual([]);
  });

  it("reads a build's coverage and the retrieval evaluation", () => {
    expect(buildCoverage(HEALTH.builds[0].coverage)).toMatchObject({ resolved: 0.8, ambiguous: 0.15, dangling: 0.05, uncalledMethods: 0.3, uncalledFunctions: null });
    expect(buildCoverage(null)).toBeNull();
    expect(evalOf(HEALTH.measures[1].eval)).toEqual({ questions: 40, recall1: 0.4, recall5: 0.65, mrr: 0.5, why: null });
    expect(evalOf(HEALTH.measures[2].eval)).toMatchObject({ recall5: null, why: "the project has no graph on the hub yet" });
  });

  it("charts the measures that read the graph and the builds, oldest first", () => {
    const rows = measureRows(HEALTH, label);
    expect(rows.map((row) => row.at)).toEqual(["2026-10-18T18:00:00Z", "2026-10-19T00:00:00Z"]);
    expect(rows.map((row) => row.values)).toEqual([
      { behind: 7, off: 5, recall5: null, mrr: null },
      { behind: 3, off: 3, recall5: 0.65, mrr: 0.5 },
    ]);
    expect(buildRows(HEALTH.builds, label).map((row) => row.values)).toEqual([
      { resolved: null, ambiguous: null, dangling: null },
      { resolved: 0.8, ambiguous: 0.15, dangling: 0.05 },
    ]);
  });

  it("reads what agents got from the graph and the worker's sync", () => {
    expect(retrievalOf({ retrieval: { used: 4, lookups: 10, empty: 3, fallbacks: 2, edited: 0, in_top: 0, empty_rate: 0.3, fallback_rate: 0.6667, in_top_rate: 0, misses: 0, evidence: ["session:a"] } })).toEqual({
      used: 4,
      lookups: 10,
      empty: 3,
      fallbacks: 2,
      edited: 0,
      inTop: 0,
      emptyRate: 0.3,
      fallbackRate: 0.6667,
      inTopRate: null,
      misses: 0,
      evidence: ["session:a"],
    });
    expect(retrievalOf(null)).toBeNull();
    const sync = syncOf({ worker: "night-mac", checked_at: "2026-10-19T01:00:00Z", error: null, failed: 1, sources: [{ source: "api", status: "failed", error: "fetch failed" }, { source: "docs" }] });
    expect(sync).toMatchObject({ worker: "night-mac", failed: 1 });
    expect(sync?.sources).toEqual([
      { source: "api", status: "failed", error: "fetch failed", commit: null, behind: null, checkedAt: null },
      { source: "docs", status: "ok", error: null, commit: null, behind: null, checkedAt: null },
    ]);
    expect(syncOf(null)).toBeNull();
  });
});

const FIGURES = {
  runtime: {
    sessions: {
      counted: 4,
      cache_read_share: 0.82,
      cost_per_session: 0.5,
      context: { peak_p50: 60000, peak_p95: 140000 },
      compactions: { compacted: 1 },
      heaviest: [{ session: "s1", evidence: "session:s1", tokens: 900, cache_read_tokens: 700, context_peak: 120000, cost_usd: 1.2 }],
    },
    runs: { counted: 0, cache_read_share: 0, cost_per_run: null, by_kind: [{ kind: "plan", runs: 2, tokens: 10, cost_usd: 0.3 }] },
    merged_prs: { prs: 1, cost_per_pr: 2.5 },
    hook_ms: 4000,
    hooks: [
      { hook: "Stop", command: "evo-ak hub stop", timed: 4, p50_ms: 300, p95_ms: 1000, max_ms: 900, failed: 0, timed_out: 0 },
      { hook: "SessionStart", command: "x", timed: 4, p50_ms: 100, p95_ms: 300 },
    ],
    tools: [{ tool: "Bash", timed: 20, p50_ms: 1000, p95_ms: 30000 }],
  },
  harness: {
    sessions: 2,
    chars_per_token: 4,
    sources: [
      { source: "always_loaded", tokens: 8000 },
      { source: "mcp_instructions", tokens: 2000 },
      { source: "skills", tokens: 1000 },
      { source: "hooks", tokens: 0 },
      { source: "mcp_output", tokens: 3000 },
      { source: "tool_output", tokens: 5000 },
    ],
    fixed_tokens_per_session: 5500,
    always_loaded: { files: 2, tokens: 8000, repos: [{ repo: "api", dir: null, tokens: 8000, files: [{ path: "~/api/CLAUDE.md", tokens: 8000, sessions: 2 }] }] },
    tool_output: [{ tool: "mcp__kg__kg_search", mcp: true, tokens: 3000, sessions: 2 }],
  },
};

describe("the runtime and the harness", () => {
  it("reads a night's figures as the page shows them", () => {
    const night = runtimeNight("2026-10-19", FIGURES);
    expect(night).not.toBeNull();
    expect(night?.perSession).toEqual({ always_loaded: 4000, mcp_instructions: 1000, skills: 500, hooks: 0, tool_output: 4000 });
    expect(night?.sessions).toMatchObject({ counted: 4, cacheRead: 0.82, costPer: 0.5, peakP50: 60000, peakP95: 140000, compacted: 1 });
    expect(night?.runs).toMatchObject({ counted: 0, cacheRead: null, costPer: null });
    expect(night?.merged).toEqual({ prs: 1, costPer: 2.5 });
    expect(night?.loaded.repos[0]).toMatchObject({ name: "api", dir: false, tokens: 8000 });
    expect(night?.heaviest[0]).toEqual({ name: "s1", evidence: "session:s1", tokens: 900, cacheRead: 700, peak: 120000, cost: 1.2 });
    expect(slowestHook(night!)).toMatchObject({ name: "Stop", p95: 1000, p50: 300 });
    expect(runtimeNight("2026-10-01", { sessions: {} })).toBeNull();
    expect(runtimeNight("2026-10-02", { runtime: {}, harness: { sessions: 0 } })?.perSession.always_loaded).toBeNull();
  });

  it("charts the nights oldest first and picks the counted nights of the range", () => {
    const one = runtimeNight("2026-10-19", FIGURES)!;
    const two = runtimeNight("2026-10-18", { runtime: { sessions: { counted: 2, cache_read_share: 0.5 } }, harness: { sessions: 0 } })!;
    const trends = runtimeTrends([one, two], (night) => ({ tick: night, title: night }));
    expect(trends.cache.map((row) => [row.at, row.values.sessions])).toEqual([
      ["2026-10-18", 0.5],
      ["2026-10-19", 0.82],
    ]);
    expect(trends.hooks[1].values).toEqual({ p95: 1000, p50: 300 });
    expect(trends.context[0].values.always_loaded).toBeNull();
    expect(
      countedNights(
        [
          { night: "2026-10-17", figures: true },
          { night: "2026-10-19", figures: true },
          { night: "2026-10-18", figures: false },
        ],
        5,
      ),
    ).toEqual(["2026-10-19", "2026-10-17"]);
    expect(readRuntimeRange("7")).toBe(7);
    expect(readRuntimeRange("8")).toBe(14);
    expect(readRuntimeRange(null)).toBe(14);
  });
});

describe("a change's replays", () => {
  it("reads the verdict, the arms and each replay", () => {
    const verdict = verdictOf({
      won: true,
      reason: "arm b passed as many",
      tasks: 6,
      a: { passed: 4, cost_usd: 1.2, turns: 30 },
      b: { passed: 4, cost_usd: 0.9, turns: 31 },
      saving: { cost_usd: 0.25, turns: -0.03 },
      paths: ["skills/x/SKILL.md", 3],
    });
    expect(verdict).toEqual({
      won: true,
      reason: "arm b passed as many",
      tasks: 6,
      needed: 6,
      a: { passed: 4, cost: 1.2, turns: 30 },
      b: { passed: 4, cost: 0.9, turns: 31 },
      saving: { cost: 0.25, turns: -0.03 },
      paths: ["skills/x/SKILL.md"],
    });
    expect(verdictOf(null)).toBeNull();
    expect(armTotals({ a: { replays: 2, passed: 1, tool_errors: 3, duration_ms: null }, b: {} }, "a")).toEqual({ replays: 2, passed: 1, toolErrors: 3, durationMs: null, peak: null });
    expect(armPassed({ a: { passed: true }, b: null }, "a")).toBe(true);
    expect(armPassed({ a: { passed: false }, b: null }, "b")).toBeNull();
    expect(lowered(0, 2, verdict)).toBe(true);
    expect(lowered(2, 2, verdict)).toBe(false);
    expect(lowered(0, 2, { ...verdict!, won: false })).toBe(false);
  });
});

describe("a trend's rows", () => {
  const rows = [
    { at: "a", tick: "a", title: "a", values: { x: 1, y: null } },
    { at: "b", tick: "b", title: "b", values: { x: null, y: null } },
    { at: "c", tick: "c", title: "c", values: { x: 3, y: 2 } },
  ];

  it("finds the ends and the total of a stack, and whether anything is there to draw", () => {
    expect(stackEnds({ x: 1, y: 0, z: 2 }, ["x", "y", "z"])).toEqual({ bottom: "x", top: "z" });
    expect(stackEnds({ x: null }, ["x"])).toEqual({ bottom: null, top: null });
    expect(stackTotal({ x: 1, y: null, z: 2 }, ["x", "y", "z"])).toBe(3);
    expect(stackTotal({ x: null }, ["x"])).toBeNull();
    expect(hasValues(rows, ["y"])).toBe(true);
    expect(hasValues(rows.slice(0, 2), ["y"])).toBe(false);
  });

  it("gives the latest value and the values of a sparkline", () => {
    expect(latest(rows, "x")).toBe(3);
    expect(latest(rows.slice(0, 2), "y")).toBeNull();
    expect(sparkValues(rows, "x")).toEqual([1, 0, 3]);
    expect(sparkValues(rows, "y")).toBeNull();
  });
});
