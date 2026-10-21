import type { TrendRow } from "./trend-model";

/**
 * How the Runtime page reads a night's figures (docs/curator.md; evo_agents.hub.runtime_metrics): the `runtime` of the
 * sessions and runs of the night's span (tokens, the cache's share, cost, context, hooks and tools timed) and the
 * `harness` of the Claude Code sessions whose transcripts the hub read (what each source of context put in, the
 * always-loaded files by repo, the tools whose output was largest). Each night counts the days of the charter's review
 * before it, so night to night is a rolling window. Read defensively: a value missing or of another type is null.
 */

/** The nights the trends span; the URL names one (`?nights=7`), none is 14. */
export const RUNTIME_RANGES = [7, 14, 30] as const;
export type RuntimeRange = (typeof RUNTIME_RANGES)[number];
export const DEFAULT_RANGE: RuntimeRange = 14;

export function readRuntimeRange(text: string | null): RuntimeRange {
  const value = Number(text);
  return (RUNTIME_RANGES as readonly number[]).includes(value) ? (value as RuntimeRange) : DEFAULT_RANGE;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function text(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function list(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null) : [];
}

/** The sources of context the chart stacks, bottom first: the fixed ones, then the output of tools (MCP tools in it). */
export const CONTEXT_SERIES = ["always_loaded", "mcp_instructions", "skills", "hooks", "tool_output"] as const;
export type ContextSeries = (typeof CONTEXT_SERIES)[number];

export type Timing = { name: string; command: string | null; timed: number; p50: number | null; p95: number | null; max: number | null; failed: number; timedOut: number };
export type LoadedRepo = { name: string; dir: boolean; tokens: number; files: { path: string; tokens: number; sessions: number }[] };
export type Heavy = { name: string; evidence: string | null; tokens: number | null; cacheRead: number | null; peak: number | null; cost: number | null };

export type RuntimeNight = {
  night: string;
  /** Sessions of Claude Code whose context sources the hub read. */
  harnessSessions: number;
  /** Tokens each source put in, per such session; null without one. */
  perSession: Record<ContextSeries, number | null>;
  fixedPerSession: number | null;
  charsPerToken: number | null;
  sessions: { counted: number; cacheRead: number | null; cacheWrite: number | null; costPer: number | null; peakP50: number | null; peakP95: number | null; compacted: number };
  runs: { counted: number; cacheRead: number | null; costPer: number | null; byKind: { kind: string; runs: number; tokens: number; cost: number | null }[] };
  merged: { prs: number; costPer: number | null };
  hookMs: number | null;
  hooks: Timing[];
  tools: Timing[];
  loaded: { files: number; tokens: number | null; repos: LoadedRepo[] };
  toolOutput: { tool: string; mcp: boolean; tokens: number; sessions: number }[];
  heaviest: Heavy[];
};

function timings(value: unknown, key: "hook" | "tool"): Timing[] {
  return list(value).map((entry) => ({
    name: text(entry[key]) ?? "-",
    command: text(entry.command),
    timed: num(entry.timed) ?? 0,
    p50: num(entry.p50_ms),
    p95: num(entry.p95_ms),
    max: num(entry.max_ms),
    failed: num(entry.failed) ?? 0,
    timedOut: num(entry.timed_out) ?? 0,
  }));
}

/** The figures of one night as the Runtime page shows them; null when they hold no runtime nor harness yet. */
export function runtimeNight(night: string, figures: unknown): RuntimeNight | null {
  const all = record(figures);
  if (!("runtime" in all) && !("harness" in all)) return null;
  const runtime = record(all.runtime);
  const harness = record(all.harness);
  const sessions = record(runtime.sessions);
  const runs = record(runtime.runs);
  const known = num(harness.sessions) ?? 0;
  const tokensOf = Object.fromEntries(list(harness.sources).map((entry) => [text(entry.source) ?? "", num(entry.tokens) ?? 0]));
  const per = (tokens: number) => (known > 0 ? Math.round(tokens / known) : null);
  const perSession = {
    always_loaded: per(tokensOf.always_loaded ?? 0),
    mcp_instructions: per(tokensOf.mcp_instructions ?? 0),
    skills: per(tokensOf.skills ?? 0),
    hooks: per(tokensOf.hooks ?? 0),
    tool_output: per((tokensOf.tool_output ?? 0) + (tokensOf.mcp_output ?? 0)),
  };
  const context = record(sessions.context);
  const loaded = record(harness.always_loaded);
  const merged = record(runtime.merged_prs);
  return {
    night,
    harnessSessions: known,
    perSession,
    fixedPerSession: num(harness.fixed_tokens_per_session),
    charsPerToken: num(harness.chars_per_token),
    sessions: {
      counted: num(sessions.counted) ?? 0,
      cacheRead: (num(sessions.counted) ?? 0) > 0 ? num(sessions.cache_read_share) : null,
      cacheWrite: (num(sessions.counted) ?? 0) > 0 ? num(sessions.cache_write_share) : null,
      costPer: num(sessions.cost_per_session),
      peakP50: num(context.peak_p50),
      peakP95: num(context.peak_p95),
      compacted: num(record(sessions.compactions).compacted) ?? 0,
    },
    runs: {
      counted: num(runs.counted) ?? 0,
      cacheRead: (num(runs.counted) ?? 0) > 0 ? num(runs.cache_read_share) : null,
      costPer: num(runs.cost_per_run),
      byKind: list(runs.by_kind).map((entry) => ({ kind: text(entry.kind) ?? "-", runs: num(entry.runs) ?? 0, tokens: num(entry.tokens) ?? 0, cost: num(entry.cost_usd) })),
    },
    merged: { prs: num(merged.prs) ?? 0, costPer: num(merged.cost_per_pr) },
    hookMs: num(runtime.hook_ms),
    hooks: timings(runtime.hooks, "hook"),
    tools: timings(runtime.tools, "tool"),
    loaded: {
      files: num(loaded.files) ?? 0,
      tokens: num(loaded.tokens),
      repos: list(loaded.repos).map((entry) => ({
        name: text(entry.repo) ?? text(entry.dir) ?? "-",
        dir: text(entry.repo) === null,
        tokens: num(entry.tokens) ?? 0,
        files: list(entry.files).map((file) => ({ path: text(file.path) ?? "-", tokens: num(file.tokens) ?? 0, sessions: num(file.sessions) ?? 0 })),
      })),
    },
    toolOutput: list(harness.tool_output).map((entry) => ({ tool: text(entry.tool) ?? "-", mcp: entry.mcp === true, tokens: num(entry.tokens) ?? 0, sessions: num(entry.sessions) ?? 0 })),
    heaviest: list(sessions.heaviest).map((entry) => ({
      name: text(entry.session) ?? "-",
      evidence: text(entry.evidence),
      tokens: num(entry.tokens),
      cacheRead: num(entry.cache_read_tokens),
      peak: num(entry.context_peak),
      cost: num(entry.cost_usd),
    })),
  };
}

/** The slowest hook of a night by its 95th percentile: the first the figures list. */
export function slowestHook(night: Pick<RuntimeNight, "hooks">): Timing | null {
  return night.hooks.find((hook) => hook.p95 !== null) ?? null;
}

type Label = (night: string) => { tick: string; title: string };

function rows(nights: readonly RuntimeNight[], label: Label, values: (night: RuntimeNight) => Record<string, number | null>): TrendRow[] {
  return [...nights].sort((a, b) => a.night.localeCompare(b.night)).map((night) => ({ at: night.night, ...label(night.night), values: values(night) }));
}

/** The trends of the page, one row a night, oldest first. */
export function runtimeTrends(nights: readonly RuntimeNight[], label: Label) {
  return {
    context: rows(nights, label, (night) => ({ ...night.perSession })),
    cache: rows(nights, label, (night) => ({ sessions: night.sessions.cacheRead, runs: night.runs.cacheRead })),
    cost: rows(nights, label, (night) => ({ session: night.sessions.costPer, run: night.runs.costPer, pr: night.merged.costPer })),
    hooks: rows(nights, label, (night) => {
      const hook = slowestHook(night);
      return { p95: hook?.p95 ?? null, p50: hook?.p50 ?? null };
    }),
    peaks: rows(nights, label, (night) => ({ p95: night.sessions.peakP95, p50: night.sessions.peakP50 })),
  };
}

/** The nights of the list whose figures were counted, newest first, at most `limit`. */
export function countedNights(nights: readonly { night: string; figures: boolean }[], limit: number): string[] {
  return nights
    .filter((night) => night.figures)
    .map((night) => night.night)
    .sort((a, b) => b.localeCompare(a))
    .slice(0, limit);
}
