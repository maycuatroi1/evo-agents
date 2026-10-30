import { PLAN_ID } from "@/lib/plans";

import type { Charter, CharterWrite, Runtime } from "./queries";
import { AUTO_MERGE_TIERS, GOAL_ID, LIMITS, LINE, PLANNED_TIERS, POOL_DUTY, POOL_NAME, type Tier, TIME_OF_DAY, TIME_ZONE, WORKER_NAME } from "./vocab";

/**
 * The charter as a form and back (curator.CharterBody): every number as typed, every list one entry a line, checked
 * with the hub's own limits so a form the web takes the hub takes too. The form writes the whole body, so what it
 * shows is what it keeps: the night's replay of the Curator's changes, its history run and its weekly probes included.
 */

export type RoleForm = { runtime: Runtime; model: string };
export type ProbeModel = { runtime: Runtime; model?: string | null };

/** The charter as the form edits it: every number as typed, every list one entry a line. */
export type CharterForm = {
  goals: { id: string; what: string }[];
  windowStart: string;
  windowEnd: string;
  timezone: string;
  worker: string;
  nightBudget: string;
  runBudget: string;
  runMaxTurns: string;
  runMinutes: string;
  maxRunsPerNight: string;
  nightPlans: string;
  maxDecisionsPerDay: string;
  briefAt: string;
  autoMerge: boolean;
  /** Kept as the charter wrote them: `auto_merge` beyond tier 0 (the form switches tier 0 alone), `planned_tiers`, `merge_without_ci`, `answer_by`. */
  moreAutoMerge: Tier[];
  plannedTiers: Tier[];
  mergeWithoutCi: boolean;
  answerBy: "owner" | "agent";
  protectedPaths: string;
  maxFailedInARow: string;
  outcomeDays: string;
  reviewLenses: string;
  reviewDays: string;
  reviewBudget: string;
  /** `replay.tasks`: the holdout tasks each change of a skill or an AGENTS.md is replayed on. */
  replayTasks: string;
  /** `replay.budget_usd`: what a night's replays may cost; empty, none is run. */
  replayBudget: string;
  /** `history.budget_usd`: what the night's history run may cost; empty, the hub's default; 0, none. */
  historyBudget: string;
  /** `history.model`: empty, the cheapest the worker on duty reports. */
  historyModel: string;
  /** `probes.k`: the sessions of each arm of a probe, on each model. */
  probeK: string;
  /** `probes.budget_usd`: what the probe runs of a week may cost; empty, none is run. */
  probeBudget: string;
  /** `probes.models` as the charter names them, kept as they are; empty, the builder's, whichever it is when written. */
  probeModels: ProbeModel[];
  reviewer: RoleForm;
  builder: RoleForm;
  judge: RoleForm;
  /** The Judge's checks, which the hub shows to admins only; null keeps those of the newest revision. */
  hiddenChecks: string | null;
};

export type CharterField =
  | Exclude<keyof CharterForm, "goals" | "reviewer" | "builder" | "judge" | "autoMerge" | "moreAutoMerge" | "plannedTiers" | "mergeWithoutCi" | "answerBy" | "probeModels">
  | "goals"
  | `${"reviewer" | "builder" | "judge"}Model`;

export type CharterProblem =
  | { code: "required" }
  | { code: "time" }
  | { code: "timezone" }
  | { code: "sameTime" }
  | { code: "worker" }
  | { code: "money"; max: number }
  | { code: "moneyOrZero"; max: number }
  | { code: "overNight" }
  | { code: "whole"; min: number; max: number }
  | { code: "goalId"; line: number }
  | { code: "goalWhat"; line: number; max: number }
  | { code: "goalRepeated"; id: string }
  | { code: "tooMany"; max: number }
  | { code: "planId"; line: number }
  | { code: "lineTooLong"; line: number; max: number }
  | { code: "control"; line: number }
  | { code: "model"; max: number };

export type CharterErrors = Partial<Record<CharterField, CharterProblem>>;

/** `replay.DEFAULT_TASKS` and `probes.DEFAULT_K`. */
const REPLAY_TASKS = 3;
const PROBE_K = 3;

const lines = (text: string): string[] => text.split("\n").map((line) => line.trim()).filter((line) => line !== "");

const money = (value: number | null | undefined) => (value === null || value === undefined ? "" : String(value));

/**
 * The models a charter's probes run on, as the form keeps them: none when they are the charter's builder alone, which
 * is what the hub fills in for none, so that a new builder takes the probes along with it.
 */
function probeModels(charter: Charter): ProbeModel[] {
  const models = charter.probes?.models ?? [];
  const builder = { runtime: charter.builder?.runtime ?? "claude-code", model: charter.builder?.model ?? null };
  const only = models.length === 1 && models[0]?.runtime === builder.runtime && (models[0]?.model ?? null) === builder.model;
  return only ? [] : models.map((item) => ({ runtime: item.runtime, model: item.model ?? null }));
}

function roleForm(role: { runtime: Runtime; model?: string | null } | undefined): RoleForm {
  return { runtime: role?.runtime ?? "claude-code", model: role?.model ?? "" };
}

/**
 * The form for `charter`, or for a first charter: the hub's defaults, the visitor's first worker that may take the
 * night shift's runs and their own time zone.
 */
export function charterForm(charter: Charter | null, first: { worker: string; timezone: string }): CharterForm {
  if (charter === null) {
    return {
      goals: [],
      windowStart: "22:00",
      windowEnd: "06:00",
      timezone: first.timezone,
      worker: first.worker,
      nightBudget: "5",
      runBudget: "",
      runMaxTurns: "300",
      runMinutes: "120",
      maxRunsPerNight: "6",
      nightPlans: "",
      maxDecisionsPerDay: "5",
      briefAt: "07:00",
      autoMerge: false,
      moreAutoMerge: [],
      plannedTiers: [...PLANNED_TIERS],
      mergeWithoutCi: false,
      answerBy: "owner",
      protectedPaths: "",
      maxFailedInARow: "2",
      outcomeDays: "7",
      reviewLenses: "3",
      reviewDays: "7",
      reviewBudget: "",
      replayTasks: String(REPLAY_TASKS),
      replayBudget: "",
      historyBudget: "",
      historyModel: "",
      probeK: String(PROBE_K),
      probeBudget: "",
      probeModels: [],
      reviewer: roleForm(undefined),
      builder: roleForm(undefined),
      judge: roleForm(undefined),
      hiddenChecks: "",
    };
  }
  return {
    goals: (charter.goals ?? []).map((goal) => ({ id: goal.id, what: goal.what })),
    windowStart: charter.window.start,
    windowEnd: charter.window.end,
    timezone: charter.window.timezone,
    worker: charter.pool ? `${POOL_DUTY}${charter.pool}` : (charter.worker ?? ""),
    nightBudget: money(charter.night_budget_usd),
    runBudget: money(charter.run_budget_usd),
    runMaxTurns: String(charter.run_max_turns ?? 300),
    runMinutes: String(charter.run_minutes ?? 120),
    maxRunsPerNight: String(charter.max_runs_per_night ?? 6),
    nightPlans: (charter.night_plans ?? []).join("\n"),
    maxDecisionsPerDay: String(charter.max_decisions_per_day ?? 5),
    briefAt: charter.brief_at ?? "07:00",
    autoMerge: (charter.auto_merge ?? []).includes(0),
    moreAutoMerge: (charter.auto_merge ?? []).filter((tier) => tier !== 0),
    plannedTiers: [...(charter.planned_tiers ?? PLANNED_TIERS)],
    mergeWithoutCi: charter.merge_without_ci ?? false,
    answerBy: charter.answer_by ?? "owner",
    protectedPaths: (charter.protected_paths ?? []).join("\n"),
    maxFailedInARow: String(charter.circuit_breaker?.max_failed_in_a_row ?? 2),
    outcomeDays: String(charter.outcome_days ?? 7),
    reviewLenses: String(charter.review?.lenses ?? 3),
    reviewDays: String(charter.review?.days ?? 7),
    reviewBudget: money(charter.review?.budget_usd),
    replayTasks: String(charter.replay?.tasks ?? REPLAY_TASKS),
    replayBudget: money(charter.replay?.budget_usd),
    historyBudget: money(charter.history?.budget_usd),
    historyModel: charter.history?.model ?? "",
    probeK: String(charter.probes?.k ?? PROBE_K),
    probeBudget: money(charter.probes?.budget_usd),
    probeModels: probeModels(charter),
    reviewer: roleForm(charter.reviewer),
    builder: roleForm(charter.builder),
    judge: roleForm(charter.judge),
    hiddenChecks: charter.judge?.hidden_checks === null || charter.judge?.hidden_checks === undefined ? null : charter.judge.hidden_checks.join("\n"),
  };
}

function wholeNumber(text: string, range: { min: number; max: number }): number | CharterProblem {
  const value = Number(text.trim());
  if (text.trim() === "") return { code: "required" };
  if (!Number.isInteger(value) || value < range.min || value > range.max) return { code: "whole", ...range };
  return value;
}

/**
 * Dollars as typed: empty is null when the field is optional; `zero` takes 0 too (the history run's "none"); `max` is
 * a night's most, or a week's for the probes.
 */
function dollars(text: string, optional: boolean, zero = false, max: number = LIMITS.budgetUsd): number | null | CharterProblem {
  const trimmed = text.trim();
  if (trimmed === "") return optional ? null : { code: "required" };
  const value = Number(trimmed);
  const low = zero ? value < 0 : value <= 0;
  if (!Number.isFinite(value) || low || value > max) return { code: zero ? "moneyOrZero" : "money", max };
  return value;
}

function problem(value: unknown): value is CharterProblem {
  return typeof value === "object" && value !== null && "code" in value;
}

function listOf(text: string, max: number, maxChars: number, check: (line: string) => boolean, bad: "planId" | "control") {
  const found = lines(text);
  if (found.length > max) return { code: "tooMany", max } as CharterProblem;
  for (const [position, line] of found.entries()) {
    if ([...line].length > maxChars) return { code: "lineTooLong", line: position + 1, max: maxChars } as CharterProblem;
    if (!check(line)) return { code: bad, line: position + 1 } as CharterProblem;
  }
  return [...new Set(found)];
}

/** A model's name as typed: null when empty, a problem when it is too long or not one line. */
function modelOf(text: string, field: CharterField, errors: CharterErrors): string | null {
  const model = text.trim();
  if (model && ([...model].length > LIMITS.modelChars || !LINE.test(model))) errors[field] = { code: "model", max: LIMITS.modelChars };
  return model || null;
}

function roleBody(role: RoleForm, field: CharterField, errors: CharterErrors) {
  return { runtime: role.runtime, model: modelOf(role.model, field, errors) };
}

/**
 * The charter a form writes, or the first problem of each field. The checks are the hub's (curator.CharterBody), so a
 * form the web takes the hub takes too, but for what only the hub knows: whether the time zone is one Postgres knows
 * and whether the worker is one of the writer's own that serves the project.
 */
export function charterBody(form: CharterForm): { body: CharterWrite; errors: null } | { body: null; errors: CharterErrors } {
  const errors: CharterErrors = {};
  const note = <T,>(field: CharterField, value: T | CharterProblem): T | null => {
    if (problem(value)) {
      errors[field] = value;
      return null;
    }
    return value as T;
  };

  if (form.goals.length > LIMITS.goals) errors.goals = { code: "tooMany", max: LIMITS.goals };
  const seen = new Set<string>();
  form.goals.forEach((goal, position) => {
    const id = goal.id.trim();
    const what = goal.what.trim();
    if (errors.goals) return;
    if (!GOAL_ID.test(id)) errors.goals = { code: "goalId", line: position + 1 };
    else if (what === "" || [...what].length > LIMITS.goalChars) errors.goals = { code: "goalWhat", line: position + 1, max: LIMITS.goalChars };
    else if (seen.has(id)) errors.goals = { code: "goalRepeated", id };
    seen.add(id);
  });

  for (const field of ["windowStart", "windowEnd", "briefAt"] as const) {
    if (!TIME_OF_DAY.test(form[field].trim())) errors[field] = { code: "time" };
  }
  if (!errors.windowStart && !errors.windowEnd && form.windowStart.trim() === form.windowEnd.trim()) errors.windowEnd = { code: "sameTime" };
  const timezone = form.timezone.trim();
  if (!TIME_ZONE.test(timezone) || timezone.length > LIMITS.timeZoneChars) errors.timezone = { code: "timezone" };
  // The duty is a worker by its name, or a pool as pool:<name>; the body names the one it is.
  const duty = form.worker.trim();
  const pool = duty.startsWith(POOL_DUTY) ? duty.slice(POOL_DUTY.length) : null;
  if (duty === "") errors.worker = { code: "required" };
  else if (pool !== null ? !POOL_NAME.test(pool) : !WORKER_NAME.test(duty)) errors.worker = { code: "worker" };

  const nightBudget = note<number | null>("nightBudget", dollars(form.nightBudget, false));
  const caps = {
    runBudget: note<number | null>("runBudget", dollars(form.runBudget, true)),
    reviewBudget: note<number | null>("reviewBudget", dollars(form.reviewBudget, true)),
    replayBudget: note<number | null>("replayBudget", dollars(form.replayBudget, true)),
    historyBudget: note<number | null>("historyBudget", dollars(form.historyBudget, true, true)),
  };
  for (const [field, cap] of Object.entries(caps) as [keyof typeof caps, number | null][]) {
    if (nightBudget !== null && cap !== null && cap > nightBudget) errors[field] = { code: "overNight" };
  }
  const runMaxTurns = note<number>("runMaxTurns", wholeNumber(form.runMaxTurns, LIMITS.turns));
  const runMinutes = note<number>("runMinutes", wholeNumber(form.runMinutes, LIMITS.runMinutes));
  const maxRunsPerNight = note<number>("maxRunsPerNight", wholeNumber(form.maxRunsPerNight, LIMITS.runsPerNight));
  const maxDecisionsPerDay = note<number>("maxDecisionsPerDay", wholeNumber(form.maxDecisionsPerDay, LIMITS.decisionsPerDay));
  const maxFailedInARow = note<number>("maxFailedInARow", wholeNumber(form.maxFailedInARow, LIMITS.circuitBreaker));
  const outcomeDays = note<number>("outcomeDays", wholeNumber(form.outcomeDays, LIMITS.outcomeDays));
  const reviewLenses = note<number>("reviewLenses", wholeNumber(form.reviewLenses, LIMITS.lenses));
  const reviewDays = note<number>("reviewDays", wholeNumber(form.reviewDays, LIMITS.reviewDays));
  const replayTasks = note<number>("replayTasks", wholeNumber(form.replayTasks, LIMITS.replayTasks));
  const probeK = note<number>("probeK", wholeNumber(form.probeK, LIMITS.probeK));
  // A week's probes are not a night's spending, so their budget is not held against the night's.
  const probeBudget = note<number | null>("probeBudget", dollars(form.probeBudget, true, false, LIMITS.probeBudgetUsd));
  const nightPlans = note<string[]>("nightPlans", listOf(form.nightPlans, LIMITS.nightPlans, 100, (line) => PLAN_ID.test(line), "planId"));
  const protectedPaths = note<string[]>(
    "protectedPaths",
    listOf(form.protectedPaths, LIMITS.protectedPaths, LIMITS.pathChars, (line) => LINE.test(line), "control"),
  );
  const hiddenChecks =
    form.hiddenChecks === null
      ? null
      : note<string[]>("hiddenChecks", listOf(form.hiddenChecks, LIMITS.hiddenChecks, LIMITS.checkChars, (line) => !line.includes("\u0000"), "control"));
  const historyModel = modelOf(form.historyModel, "historyModel", errors);
  const reviewer = roleBody(form.reviewer, "reviewerModel", errors);
  const builder = roleBody(form.builder, "builderModel", errors);
  const judge = roleBody(form.judge, "judgeModel", errors);

  if (Object.keys(errors).length > 0) return { body: null, errors };
  return {
    body: {
      goals: form.goals.map((goal) => ({ id: goal.id.trim(), what: goal.what.trim() })),
      window: { start: form.windowStart.trim(), end: form.windowEnd.trim(), timezone },
      ...(pool !== null ? { pool } : { worker: duty }),
      night_budget_usd: nightBudget as number,
      run_budget_usd: caps.runBudget,
      run_max_turns: runMaxTurns as number,
      run_minutes: runMinutes as number,
      max_runs_per_night: maxRunsPerNight as number,
      night_plans: nightPlans ?? [],
      max_decisions_per_day: maxDecisionsPerDay as number,
      brief_at: form.briefAt.trim(),
      planned_tiers: [...form.plannedTiers],
      auto_merge: [...(form.autoMerge ? AUTO_MERGE_TIERS : []), ...form.moreAutoMerge].sort((a, b) => a - b),
      merge_without_ci: form.mergeWithoutCi,
      answer_by: form.answerBy,
      protected_paths: protectedPaths ?? [],
      circuit_breaker: { max_failed_in_a_row: maxFailedInARow as number },
      outcome_days: outcomeDays as number,
      review: { lenses: reviewLenses as number, days: reviewDays as number, budget_usd: caps.reviewBudget },
      replay: { tasks: replayTasks as number, budget_usd: caps.replayBudget },
      history: { budget_usd: caps.historyBudget, model: historyModel },
      probes: { k: probeK as number, models: form.probeModels, budget_usd: probeBudget },
      reviewer,
      builder,
      judge: { ...judge, hidden_checks: hiddenChecks },
    },
    errors: null,
  };
}

/** The fields of a form in the order they show, so the first one with a problem takes focus. */
export const CHARTER_FIELDS: readonly CharterField[] = [
  "goals",
  "windowStart",
  "windowEnd",
  "timezone",
  "worker",
  "nightBudget",
  "runBudget",
  "maxRunsPerNight",
  "runMaxTurns",
  "runMinutes",
  "nightPlans",
  "maxDecisionsPerDay",
  "briefAt",
  "outcomeDays",
  "protectedPaths",
  "maxFailedInARow",
  "reviewLenses",
  "reviewDays",
  "reviewBudget",
  "replayTasks",
  "replayBudget",
  "historyBudget",
  "historyModel",
  "probeK",
  "probeBudget",
  "reviewerModel",
  "builderModel",
  "judgeModel",
  "hiddenChecks",
];

/** The time zones the browser knows, for the field's suggestions; the hub checks the one written against Postgres. */
export function timeZones(): string[] {
  try {
    return Intl.supportedValuesOf("timeZone");
  } catch {
    return [];
  }
}

/** The visitor's own time zone, for a first charter. */
export function ownTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}
