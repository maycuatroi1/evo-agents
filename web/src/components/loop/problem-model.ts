import { PROBLEM_STATUSES, type ProblemAggregate, type ProblemMetric, type ProblemStatus, type ProjectProblem } from "@/lib/problem-queries";
import { DEFAULT_WINDOW_DAYS, LOGIN_NAME, PROBLEM_LIMITS, type ProblemWrite } from "@/lib/problem-writes";
import { MAX_SIGNALS, type Signal, type SignalsFilter } from "@/lib/signal-queries";

/**
 * The shapes of the Problems pages (plan problem-oriented-hub): the list's order
 * and its filter in the address, its metric summed up from its key's signals as the hub sums a goal's, and the fields
 * of the form a writer fills. Pure: no React, no query.
 */

export function isProblemStatus(value: string | null): value is ProblemStatus {
  return value !== null && (PROBLEM_STATUSES as readonly string[]).includes(value);
}

/** The list's filter from its address (`?status=open`); a status the hub does not know reads as every status. */
export function readStatusFilter(params: { get: (name: string) => string | null }): ProblemStatus | null {
  const status = params.get("status");
  return isProblemStatus(status) ? status : null;
}

/** The open problems first, then the solved and the dropped ones; by rank within each, then by code. */
export function byStatusThenRank<T extends Pick<ProjectProblem, "status" | "rank" | "number">>(problems: readonly T[]): T[] {
  const order = (status: ProblemStatus) => PROBLEM_STATUSES.indexOf(status);
  return [...problems].sort((a, b) => order(a.status) - order(b.status) || a.rank - b.rank || a.number - b.number);
}

export function statusCounts(problems: readonly Pick<ProjectProblem, "status">[]): Record<ProblemStatus, number> {
  const counts: Record<ProblemStatus, number> = { open: 0, solved: 0, dropped: 0 };
  for (const problem of problems) counts[problem.status] += 1;
  return counts;
}

// The metric

export function windowOf(metric: Pick<ProblemMetric, "window_days">): number {
  return metric.window_days ?? DEFAULT_WINDOW_DAYS;
}

/** The read of the signals a metric sums up: its key and tenant over its window back from `now`, to the minute, so a page reads it once. */
export function metricFilter(metric: ProblemMetric, now: number): SignalsFilter {
  const minute = 60_000;
  const since = new Date(Math.floor(now / minute) * minute - windowOf(metric) * 24 * 3600 * 1000).toISOString();
  return { key: metric.key, tenant: metric.tenant ?? null, since, limit: MAX_SIGNALS };
}

export type MetricPoint = { id: number; at: number; time: string; value: number };

/** The numbers of the window, oldest first, as the hub reads them: a value, measured after the window's start. */
export function metricPoints(signals: readonly Signal[], metric: ProblemMetric, now: number): MetricPoint[] {
  const start = now - windowOf(metric) * 24 * 3600 * 1000;
  return signals
    .filter((signal) => signal.value !== null && Date.parse(signal.measured_at) > start && (!metric.tenant || signal.tenant === metric.tenant))
    .map((signal) => ({ id: signal.id, at: Date.parse(signal.measured_at), time: signal.measured_at, value: signal.value as number }))
    .sort((a, b) => a.at - b.at || a.id - b.id);
}

/**
 * The metric's figure over its window, as the hub sums a goal's metric up (`loop.view.measure`): the last value, the
 * mean, the sum, the count, or a percentile by the nearest rank (`percentile_disc`); null without a value.
 */
export function aggregateOf(points: readonly MetricPoint[], aggregate: ProblemAggregate): number | null {
  if (aggregate === "count") return points.length === 0 ? null : points.length;
  if (points.length === 0) return null;
  const values = points.map((point) => point.value);
  switch (aggregate) {
    case "last":
      return points[points.length - 1].value;
    case "sum":
      return values.reduce((sum, value) => sum + value, 0);
    case "mean":
      return values.reduce((sum, value) => sum + value, 0) / values.length;
    case "p50":
    case "p90": {
      const sorted = [...values].sort((a, b) => a - b);
      const share = aggregate === "p50" ? 0.5 : 0.9;
      return sorted[Math.max(0, Math.ceil(share * sorted.length) - 1)];
    }
  }
}

// The form

/** A problem as its form holds it: every field as typed, numbers as text until they are sent. */
export type ProblemForm = {
  statement: string;
  who: string;
  when: string;
  workaround: string;
  metricKey: string;
  aggregate: ProblemAggregate;
  windowDays: string;
  tenant: string;
  rank: string;
  owner: string;
  note: string;
};

export type ProblemField = "statement" | "who" | "when" | "workaround" | "windowDays" | "tenant" | "rank" | "owner";

export const EMPTY_PROBLEM: ProblemForm = {
  statement: "",
  who: "",
  when: "",
  workaround: "",
  metricKey: "",
  aggregate: "mean",
  windowDays: "",
  tenant: "",
  rank: "",
  owner: "",
  note: "",
};

/** The form of an edit: the problem at the revision the page read, with no note yet. */
export function formOf(problem: ProjectProblem): ProblemForm {
  return {
    statement: problem.statement,
    who: problem.who,
    when: problem.when,
    workaround: problem.workaround,
    metricKey: problem.metric?.key ?? "",
    aggregate: problem.metric?.aggregate ?? "mean",
    windowDays: problem.metric?.window_days ? String(problem.metric.window_days) : "",
    tenant: problem.metric?.tenant ?? "",
    rank: String(problem.rank),
    owner: problem.owner,
    note: "",
  };
}

function wholeIn(text: string, low: number, high: number): boolean {
  return /^\d+$/.test(text) && Number(text) >= low && Number(text) <= high;
}

/** The fields the hub would refuse, in the form's order: a text left blank, a number out of its range, a login it cannot be. */
export function formProblems(form: ProblemForm): ProblemField[] {
  const wrong: ProblemField[] = [];
  for (const field of ["statement", "who", "when", "workaround"] as const) if (!form[field].trim()) wrong.push(field);
  if (form.metricKey && form.windowDays.trim() && !wholeIn(form.windowDays.trim(), 1, PROBLEM_LIMITS.windowDays)) wrong.push("windowDays");
  if (form.metricKey && form.tenant.trim().length > PROBLEM_LIMITS.tenant) wrong.push("tenant");
  if (form.rank.trim() && !wholeIn(form.rank.trim(), 1, PROBLEM_LIMITS.rank)) wrong.push("rank");
  if (form.owner.trim() && !LOGIN_NAME.test(form.owner.trim())) wrong.push("owner");
  return wrong;
}

/**
 * The write of `form`: a new problem (`held` null, `if_revision` 0, so it never overwrites one another writer made) or
 * the next revision of `held` at the revision the page read, keeping its status and the reason it was closed.
 */
export function writeOf(form: ProblemForm, held: ProjectProblem | null): ProblemWrite {
  const metric: ProblemMetric | null = form.metricKey
    ? {
        key: form.metricKey,
        aggregate: form.aggregate,
        window_days: form.windowDays.trim() ? Number(form.windowDays.trim()) : null,
        tenant: form.tenant.trim() || null,
      }
    : null;
  return {
    statement: form.statement.trim(),
    who: form.who.trim(),
    when: form.when.trim(),
    workaround: form.workaround.trim(),
    metric,
    rank: form.rank.trim() ? Number(form.rank.trim()) : null,
    owner: form.owner.trim() || null,
    status: held?.status ?? "open",
    close_reason: held?.close_reason ?? null,
    note: form.note.trim() || null,
    if_revision: held ? held.revision : 0,
  };
}

/** The fields a revision names as changed, in the hub's order (`problems.FIELDS`), for the words of its history. */
export const REVISION_FIELDS = ["statement", "who", "when", "workaround", "metric", "rank", "owner", "status", "close_reason"] as const;
export type RevisionField = (typeof REVISION_FIELDS)[number];

export function isRevisionField(value: string): value is RevisionField {
  return (REVISION_FIELDS as readonly string[]).includes(value);
}
