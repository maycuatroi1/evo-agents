import type { KgBuildCoverage, KgHealthView, KgMeasure } from "./insight-queries";
import type { TrendRow } from "./trend-model";

export { type SyncSource, syncOf, type SyncState } from "./graph-sync";

/**
 * How the Graph health page reads the knowledge graph's health (docs/curator.md, kg_health; evo_agents.hub.kg_coverage):
 * each git source of a measure against its repo's default branch, a build's coverage, the retrieval evaluation, what
 * agents got from the graph in their traces, and the worker's sync. Every part is a JSON object the hub wrote, so each
 * is read defensively: a value missing or of another type is null, never a zero.
 */

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function text(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

export type SourceHealth = {
  source: string;
  repo: string | null;
  commit: string | null;
  branch: string | null;
  head: string | null;
  behind: number | null;
  lagHours: number | null;
  missing: number | null;
  stale: number | null;
  deleted: number | null;
  samples: { missing: string[]; stale: string[]; deleted: string[] };
  evidence: string[];
  /** Why part of the source could not be read; null when all of it was. */
  why: string | null;
};

export function sourcesOf(measure: Pick<KgMeasure, "sources"> | null | undefined): SourceHealth[] {
  return (measure?.sources ?? []).map((raw) => {
    const entry = record(raw);
    const samples = record(entry.samples);
    return {
      source: text(entry.source) ?? "-",
      repo: text(entry.repo),
      commit: text(entry.commit),
      branch: text(entry.default_branch),
      head: text(entry.head),
      behind: num(entry.behind),
      lagHours: num(entry.lag_hours),
      missing: num(entry.missing),
      stale: num(entry.stale),
      deleted: num(entry.deleted),
      samples: { missing: strings(samples.missing), stale: strings(samples.stale), deleted: strings(samples.deleted) },
      evidence: strings(entry.evidence),
      why: text(entry.why),
    };
  });
}

/** A sum over the sources that say it; null when none does. */
function sum(values: (number | null)[]): number | null {
  const held = values.filter((value): value is number => value !== null);
  return held.length > 0 ? held.reduce((total, value) => total + value, 0) : null;
}

/** The commits the graph lacks, the files it lacks, holds stale or deleted, and the longest wait, over its sources. */
export function measureTotals(sources: readonly SourceHealth[]): { behind: number | null; off: number | null; lagHours: number | null } {
  const lags = sources.map((item) => item.lagHours).filter((value): value is number => value !== null);
  return {
    behind: sum(sources.map((item) => item.behind)),
    off: sum(sources.flatMap((item) => [item.missing, item.stale, item.deleted])),
    lagHours: lags.length > 0 ? Math.max(...lags) : null,
  };
}

export type BuildCoverage = {
  mentions: number | null;
  resolved: number | null;
  ambiguous: number | null;
  dangling: number | null;
  danglingEdges: number | null;
  /** Python functions and methods outside tests that no call edge reaches, of them all. */
  uncalledFunctions: number | null;
  uncalledMethods: number | null;
};

export function coverageOf(coverage: unknown): BuildCoverage | null {
  if (typeof coverage !== "object" || coverage === null) return null;
  const found = record(coverage);
  const python = record(found.python);
  return {
    mentions: num(found.mentions),
    resolved: num(found.resolved_ratio),
    ambiguous: num(found.ambiguous_ratio),
    dangling: num(found.dangling_ratio),
    danglingEdges: num(found.dangling_edges),
    uncalledFunctions: num(python.uncalled_function_ratio),
    uncalledMethods: num(python.uncalled_method_ratio),
  };
}

export type EvalScore = { questions: number | null; recall1: number | null; recall5: number | null; mrr: number | null; why: string | null };

/** The retrieval evaluation of a measure's graph: kg_search's scores over the plan-step questions, or why there is none. */
export function evalOf(value: unknown): EvalScore | null {
  if (typeof value !== "object" || value === null) return null;
  const found = record(value);
  const method = record(record(found.methods).kg_search);
  return {
    questions: num(found.questions),
    recall1: num(method["recall@1"]),
    recall5: num(method["recall@5"]),
    mrr: num(method.mrr),
    why: text(found.why),
  };
}

export type Retrieval = {
  used: number;
  lookups: number;
  empty: number;
  fallbacks: number;
  edited: number;
  inTop: number;
  emptyRate: number | null;
  fallbackRate: number | null;
  inTopRate: number | null;
  misses: number;
  evidence: string[];
};

/** What agents got from the graph in the night's sessions and runs (the figures' `kg_health.retrieval`). */
export function retrievalOf(kgHealth: unknown): Retrieval | null {
  const found = record(record(kgHealth).retrieval);
  if (Object.keys(found).length === 0) return null;
  const count = (key: string) => Math.max(0, Math.round(num(found[key]) ?? 0));
  return {
    used: count("used"),
    lookups: count("lookups"),
    empty: count("empty"),
    fallbacks: count("fallbacks"),
    edited: count("edited"),
    inTop: count("in_top"),
    emptyRate: count("lookups") > 0 ? num(found.empty_rate) : null,
    fallbackRate: count("empty") > 0 ? num(found.fallback_rate) : null,
    inTopRate: count("edited") > 0 ? num(found.in_top_rate) : null,
    misses: count("misses"),
    evidence: strings(found.evidence),
  };
}

/** The measures as a trend, oldest first: commits behind, files off, and the evaluation's recall at 5 and MRR. */
export function measureRows(view: Pick<KgHealthView, "measures">, label: (at: string) => { tick: string; title: string }): TrendRow[] {
  return [...view.measures]
    .reverse()
    .filter((measure) => measure.error === null)
    .map((measure) => {
      const totals = measureTotals(sourcesOf(measure));
      const score = evalOf(measure.eval);
      return {
        at: measure.measured_at,
        ...label(measure.measured_at),
        values: { behind: totals.behind, off: totals.off, recall5: score?.recall5 ?? null, mrr: score?.mrr ?? null },
      };
    });
}

/** The successful builds as a trend, oldest first: the shares of mentions resolved, ambiguous and dangling. */
export function buildRows(builds: readonly KgBuildCoverage[], label: (at: string) => { tick: string; title: string }): TrendRow[] {
  return [...builds].reverse().map((build) => {
    const coverage = coverageOf(build.coverage);
    return {
      at: build.finished_at,
      ...label(build.finished_at),
      values: { resolved: coverage?.resolved ?? null, ambiguous: coverage?.ambiguous ?? null, dangling: coverage?.dangling ?? null },
    };
  });
}

/** A commit as its first 12 characters. */
export function shortCommit(sha: string | null): string | null {
  return sha ? sha.slice(0, 12) : null;
}
