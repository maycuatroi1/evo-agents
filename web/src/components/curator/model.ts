import type { Route } from "next";

import type { ParamSource } from "@/components/admin/data";
import { parseRunId, runHref } from "@/components/runs/queries";
import { PLAN_ID } from "@/lib/plans";

import {
  type Charter,
  type CharterWrite,
  type CuratorState,
  type CuratorStatus,
  type Lens,
  type Proposal,
  PROPOSALS_PAGE,
  type ProposalAction,
  type ProposalQuery,
  type ProposalState,
  type Runtime,
} from "./queries";

/**
 * The Curator pages' pure parts: the hub's limits and vocabularies as the web needs them, where a project's night
 * shift stands in a word, the proposal list's filters in the URL, what an answer may carry, where each piece of
 * evidence leads, and the charter as a form and back. Every limit here repeats one of the API's (evo_agents.hub.curator,
 * evo_agents.hub.review), so a form refuses what the hub would refuse before anything is sent; the hub decides again.
 */

/** `review.LENSES`, in the order the nights take them. */
export const LENSES = [
  "tool_errors",
  "environment",
  "corrections",
  "failed_runs",
  "tech_debt",
  "code_health",
  "docs_drift",
  "skills_memory",
  "cost",
  "security",
  "product_goals",
] as const satisfies readonly Lens[];

/** `review.PROPOSAL_STATES`, in the order the list's filter shows them: what waits first. */
export const PROPOSAL_STATES = ["open", "deferred", "accepted", "rejected", "dropped"] as const satisfies readonly ProposalState[];

/** `curator.TIERS`: from what the hub may merge by itself (0) to what the owner alone merges (3). */
export const TIERS = [0, 1, 2, 3] as const;
export type Tier = (typeof TIERS)[number];

/** `curator.AUTO_MERGE_TIERS`: the tiers a charter may let the hub merge; tier 1 waits for a plan of its own. */
export const AUTO_MERGE_TIERS = [0] as const;

/** `runs.RUNTIMES`: the runtimes a role of the Curator runs on. */
export const RUNTIMES = ["claude-code", "opencode", "codex"] as const satisfies readonly Runtime[];

/** `review.DEFER_DAYS` and `review.DEFAULT_DEFER_DAYS`. */
export const DEFER_DAYS = { min: 1, max: 90, default: 7 } as const;
/** `review.MAX_NOTE_CHARS`: the owner's note with an answer, one line. */
export const MAX_NOTE_CHARS = 2000;

export const LIMITS = {
  goals: 20,
  goalChars: 2000,
  nightPlans: 50,
  protectedPaths: 200,
  pathChars: 500,
  hiddenChecks: 50,
  checkChars: 2000,
  budgetUsd: 1000,
  turns: { min: 1, max: 10_000 },
  runMinutes: { min: 10, max: 720 },
  runsPerNight: { min: 1, max: 100 },
  decisionsPerDay: { min: 0, max: 100 },
  circuitBreaker: { min: 1, max: 10 },
  /** `ledger.OUTCOME_DAYS`: the days after a merge over which the hub counts a change's figures again. */
  outcomeDays: { min: 1, max: 90 },
  lenses: { min: 1, max: LENSES.length },
  reviewDays: { min: 1, max: 30 },
  timeZoneChars: 64,
  modelChars: 200,
} as const;

const TIME_OF_DAY = /^([01][0-9]|2[0-3]):[0-5][0-9]$/;
const TIME_ZONE = /^[A-Za-z][A-Za-z0-9_+-]*(\/[A-Za-z0-9_+-]+)*$/;
const GOAL_ID = /^[a-z0-9][a-z0-9-]{0,63}$/;
const WORKER_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;
/** One line without control characters: `runs.LINE`. */
const LINE = /^[^\u0000-\u001f\u007f]+$/;

export function isLens(value: string | null | undefined): value is Lens {
  return (LENSES as readonly string[]).includes(value ?? "");
}

function isProposalState(value: string | null | undefined): value is ProposalState {
  return (PROPOSAL_STATES as readonly string[]).includes(value ?? "");
}

// Where the night shift stands

/** A project's night shift in a word, as the pill beside the Curator's title says it: `off` without a charter. */
export type CuratorLook = CuratorState | "off";

export function curatorLook(status: Pick<CuratorStatus, "charter" | "state">): CuratorLook {
  return status.charter === null || !status.state ? "off" : status.state;
}

/**
 * What the visitor may do on the Curator's pages: an admin of the project writes the charter and answers proposals;
 * an admin, or the owner of one of its schedules, pauses and resumes the night shift once it has a schedule.
 */
export type CuratorRights = { charter: boolean; answer: boolean; pause: boolean };

export function curatorRights(
  status: Pick<CuratorStatus, "schedules"> | null,
  viewer: { login: string; role: string | null } | null,
): CuratorRights {
  const admin = viewer?.role === "admin";
  const owner = viewer !== null && (status?.schedules ?? []).some((schedule) => schedule.owner === viewer.login);
  return { charter: admin, answer: admin, pause: (status?.schedules.length ?? 0) > 0 && (admin || owner) };
}

// The proposals' filters

/** The most pages the URL may name: 50 a page up to the API's largest offset. */
const MAX_PAGE = 2000;

export type ProposalFilters = {
  state: ProposalState | null;
  tier: Tier | null;
  lens: Lens | null;
  /** The review run whose proposals the list shows, from a night's row. */
  run: number | null;
  page: number;
};

export const NO_PROPOSAL_FILTERS: ProposalFilters = { state: null, tier: null, lens: null, run: null, page: 1 };

export function readProposalFilters(source: ParamSource): ProposalFilters {
  const state = source.get("state");
  const tier = Number(source.get("tier") ?? "");
  const lens = source.get("lens");
  const page = Number(source.get("page") ?? "1");
  const tierText = source.get("tier");
  return {
    state: isProposalState(state) ? state : null,
    tier: tierText !== null && tierText !== "" && (TIERS as readonly number[]).includes(tier) ? (tier as Tier) : null,
    lens: isLens(lens) ? lens : null,
    run: parseRunId(source.get("run")?.trim() ?? ""),
    page: Number.isInteger(page) && page >= 1 && page <= MAX_PAGE ? page : 1,
  };
}

/** The URL's query for a set of filters, in a stable order; empty when nothing is set. */
export function proposalSearch(filters: ProposalFilters): string {
  const params = new URLSearchParams();
  if (filters.state) params.set("state", filters.state);
  if (filters.tier !== null) params.set("tier", String(filters.tier));
  if (filters.lens) params.set("lens", filters.lens);
  if (filters.run !== null) params.set("run", String(filters.run));
  if (filters.page > 1) params.set("page", String(filters.page));
  const text = params.toString();
  return text ? `?${text}` : "";
}

export function isFiltered(filters: ProposalFilters): boolean {
  return filters.state !== null || filters.tier !== null || filters.lens !== null || filters.run !== null;
}

export function proposalListQuery(filters: ProposalFilters): ProposalQuery {
  return {
    state: filters.state,
    tier: filters.tier,
    lens: filters.lens,
    run: filters.run,
    limit: PROPOSALS_PAGE,
    offset: (filters.page - 1) * PROPOSALS_PAGE,
  };
}

// Answers

/** An open or deferred proposal is answered; an accepted, rejected or dropped one is not. */
export function isAnswerable(proposal: Pick<Proposal, "state">): boolean {
  return proposal.state === "open" || proposal.state === "deferred";
}

export type AnswerProblem = "noteLine" | "noteLong" | "days" | null;

/** Why an answer cannot be sent as it stands: a note of more than one line or over 2,000 characters, or days out of 1 to 90. */
export function answerProblem(action: ProposalAction, note: string, days: string): AnswerProblem {
  const words = note.trim();
  if (words && !LINE.test(words)) return "noteLine";
  if ([...words].length > MAX_NOTE_CHARS) return "noteLong";
  if (action === "defer") {
    const value = Number(days);
    if (!Number.isInteger(value) || value < DEFER_DAYS.min || value > DEFER_DAYS.max) return "days";
  }
  return null;
}

/** The body of an answer: the action, the note when there is one, and the days of a deferral. */
export function answerBody(action: ProposalAction, note: string, days: string) {
  const words = note.trim();
  return {
    action,
    ...(words ? { note: words } : {}),
    ...(action === "defer" ? { defer_days: Number(days) } : {}),
  };
}

// Evidence

/** A repo of the project as GET /v1/projects/{p} lists it. */
export type RepoInfo = { name: string; origin?: string | null; default_branch?: string | null };

export type EvidenceView =
  | { kind: "run"; runId: number; seq: number; href: Route }
  | { kind: "session"; session: string; field: string | null; index: number | null }
  | { kind: "code"; repo: string; path: string; line: number | null; commit: string | null; url: string | null }
  | { kind: "other"; text: string };

function str(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function int(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

/**
 * The web address of a file of a repo at `ref`, on the line given: GitHub's blob URL, or GitLab's for a host whose
 * name says gitlab. Null for an origin the web cannot read as https or as git's scp form (`git@host:group/repo.git`).
 */
export function codeUrl(origin: string | null | undefined, ref: string | null, path: string, line: number | null): string | null {
  if (!origin || !ref) return null;
  let base: string | null = null;
  const scp = /^[\w.-]+@([\w.-]+):(.+?)(?:\.git)?\/?$/.exec(origin);
  if (scp) base = `https://${scp[1]}/${scp[2]}`;
  else {
    try {
      const url = new URL(origin);
      if (url.protocol === "https:" || url.protocol === "http:") {
        base = `https://${url.host}${url.pathname.replace(/\.git\/?$/, "").replace(/\/+$/, "")}`;
      }
    } catch {
      base = null;
    }
  }
  if (!base) return null;
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  const blob = /gitlab/i.test(new URL(base).host) ? "-/blob" : "blob";
  return `${base}/${blob}/${encodeURIComponent(ref)}/${encoded}${line !== null ? `#L${line}` : ""}`;
}

/** Where a piece of evidence leads, as the hub resolved it: a run's event, a session's digest, a line of code. */
export function evidenceView(raw: Record<string, unknown>, project: string, repos: readonly RepoInfo[]): EvidenceView {
  const kind = raw.kind;
  if (kind === "run") {
    const runId = int(raw.run_id);
    const seq = int(raw.seq);
    if (runId !== null && seq !== null) return { kind: "run", runId, seq, href: `${runHref(project, runId)}?view=log` as Route };
  }
  if (kind === "session") {
    const session = str(raw.session_id);
    if (session !== null) return { kind: "session", session, field: str(raw.field), index: int(raw.index) };
  }
  if (kind === "code") {
    const repo = str(raw.repo);
    const path = str(raw.path);
    if (repo !== null && path !== null) {
      const info = repos.find((item) => item.name === repo);
      const commit = str(raw.commit);
      const line = int(raw.line);
      return { kind: "code", repo, path, line, commit, url: codeUrl(info?.origin, commit ?? info?.default_branch ?? null, path, line) };
    }
  }
  return { kind: "other", text: JSON.stringify(raw) };
}

/** The entry of a digest that a piece of evidence names, as text: a line the person typed, a command, an error. */
export function digestEntry(digest: Record<string, unknown>, field: string | null, index: number | null): string | null {
  if (field === null || index === null) return null;
  const list = digest[field];
  if (!Array.isArray(list) || index < 0 || index >= list.length) return null;
  const entry: unknown = list[index];
  if (typeof entry === "string") return entry;
  return JSON.stringify(entry, null, 2);
}

// The charter as a form

export type RoleForm = { runtime: Runtime; model: string };

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
  protectedPaths: string;
  maxFailedInARow: string;
  outcomeDays: string;
  reviewLenses: string;
  reviewDays: string;
  reviewBudget: string;
  reviewer: RoleForm;
  builder: RoleForm;
  judge: RoleForm;
  /** The Judge's checks, which the hub shows to admins only; null keeps those of the newest revision. */
  hiddenChecks: string | null;
};

export type CharterField = Exclude<keyof CharterForm, "goals" | "reviewer" | "builder" | "judge" | "autoMerge"> | "goals" | `${"reviewer" | "builder" | "judge"}Model`;

export type CharterProblem =
  | { code: "required" }
  | { code: "time" }
  | { code: "timezone" }
  | { code: "sameTime" }
  | { code: "worker" }
  | { code: "money"; max: number }
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

function lines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

const money = (value: number | null | undefined) => (value === null || value === undefined ? "" : String(value));

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
      protectedPaths: "",
      maxFailedInARow: "2",
      outcomeDays: "7",
      reviewLenses: "3",
      reviewDays: "7",
      reviewBudget: "",
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
    worker: charter.worker,
    nightBudget: money(charter.night_budget_usd),
    runBudget: money(charter.run_budget_usd),
    runMaxTurns: String(charter.run_max_turns ?? 300),
    runMinutes: String(charter.run_minutes ?? 120),
    maxRunsPerNight: String(charter.max_runs_per_night ?? 6),
    nightPlans: (charter.night_plans ?? []).join("\n"),
    maxDecisionsPerDay: String(charter.max_decisions_per_day ?? 5),
    briefAt: charter.brief_at ?? "07:00",
    autoMerge: (charter.auto_merge ?? []).includes(0),
    protectedPaths: (charter.protected_paths ?? []).join("\n"),
    maxFailedInARow: String(charter.circuit_breaker?.max_failed_in_a_row ?? 2),
    outcomeDays: String(charter.outcome_days ?? 7),
    reviewLenses: String(charter.review?.lenses ?? 3),
    reviewDays: String(charter.review?.days ?? 7),
    reviewBudget: money(charter.review?.budget_usd),
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

function dollars(text: string, optional: boolean): number | null | CharterProblem {
  const trimmed = text.trim();
  if (trimmed === "") return optional ? null : { code: "required" };
  const value = Number(trimmed);
  if (!Number.isFinite(value) || value <= 0 || value > LIMITS.budgetUsd) return { code: "money", max: LIMITS.budgetUsd };
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

function roleBody(role: RoleForm, field: CharterField, errors: CharterErrors) {
  const model = role.model.trim();
  if (model && ([...model].length > LIMITS.modelChars || !LINE.test(model))) errors[field] = { code: "model", max: LIMITS.modelChars };
  return { runtime: role.runtime, model: model || null };
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
  const worker = form.worker.trim();
  if (!WORKER_NAME.test(worker)) errors.worker = worker === "" ? { code: "required" } : { code: "worker" };

  const nightBudget = note<number | null>("nightBudget", dollars(form.nightBudget, false));
  const runBudget = note<number | null>("runBudget", dollars(form.runBudget, true));
  const reviewBudget = note<number | null>("reviewBudget", dollars(form.reviewBudget, true));
  if (nightBudget !== null && runBudget !== null && runBudget > nightBudget) errors.runBudget = { code: "overNight" };
  if (nightBudget !== null && reviewBudget !== null && reviewBudget > nightBudget) errors.reviewBudget = { code: "overNight" };
  const runMaxTurns = note<number>("runMaxTurns", wholeNumber(form.runMaxTurns, LIMITS.turns));
  const runMinutes = note<number>("runMinutes", wholeNumber(form.runMinutes, LIMITS.runMinutes));
  const maxRunsPerNight = note<number>("maxRunsPerNight", wholeNumber(form.maxRunsPerNight, LIMITS.runsPerNight));
  const maxDecisionsPerDay = note<number>("maxDecisionsPerDay", wholeNumber(form.maxDecisionsPerDay, LIMITS.decisionsPerDay));
  const maxFailedInARow = note<number>("maxFailedInARow", wholeNumber(form.maxFailedInARow, LIMITS.circuitBreaker));
  const outcomeDays = note<number>("outcomeDays", wholeNumber(form.outcomeDays, LIMITS.outcomeDays));
  const reviewLenses = note<number>("reviewLenses", wholeNumber(form.reviewLenses, LIMITS.lenses));
  const reviewDays = note<number>("reviewDays", wholeNumber(form.reviewDays, LIMITS.reviewDays));
  const nightPlans = note<string[]>("nightPlans", listOf(form.nightPlans, LIMITS.nightPlans, 100, (line) => PLAN_ID.test(line), "planId"));
  const protectedPaths = note<string[]>(
    "protectedPaths",
    listOf(form.protectedPaths, LIMITS.protectedPaths, LIMITS.pathChars, (line) => LINE.test(line), "control"),
  );
  const hiddenChecks =
    form.hiddenChecks === null
      ? null
      : note<string[]>("hiddenChecks", listOf(form.hiddenChecks, LIMITS.hiddenChecks, LIMITS.checkChars, (line) => !line.includes("\u0000"), "control"));
  const reviewer = roleBody(form.reviewer, "reviewerModel", errors);
  const builder = roleBody(form.builder, "builderModel", errors);
  const judge = roleBody(form.judge, "judgeModel", errors);

  if (Object.keys(errors).length > 0) return { body: null, errors };
  return {
    body: {
      goals: form.goals.map((goal) => ({ id: goal.id.trim(), what: goal.what.trim() })),
      window: { start: form.windowStart.trim(), end: form.windowEnd.trim(), timezone },
      worker,
      night_budget_usd: nightBudget as number,
      run_budget_usd: runBudget,
      run_max_turns: runMaxTurns as number,
      run_minutes: runMinutes as number,
      max_runs_per_night: maxRunsPerNight as number,
      night_plans: nightPlans ?? [],
      max_decisions_per_day: maxDecisionsPerDay as number,
      brief_at: form.briefAt.trim(),
      auto_merge: form.autoMerge ? [...AUTO_MERGE_TIERS] : [],
      protected_paths: protectedPaths ?? [],
      circuit_breaker: { max_failed_in_a_row: maxFailedInARow as number },
      outcome_days: outcomeDays as number,
      review: { lenses: reviewLenses as number, days: reviewDays as number, budget_usd: reviewBudget },
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

// The charter's page

/** `?revision=N` names an older revision to read; `?edit=1` the form, for an admin (never over an older revision). */
export function readCharterView(source: ParamSource): { revision: number | null; edit: boolean } {
  const text = source.get("revision") ?? "";
  const revision = /^[1-9][0-9]{0,8}$/.test(text) ? Number(text) : null;
  return { revision, edit: source.get("edit") === "1" && revision === null };
}

// Nights

/** A night's outcome, as its row says it: work in flight, every run done, a run that failed, or nothing queued. */
export type NightOutcome = "active" | "failed" | "done" | "empty";

export function nightOutcome(night: { runs: number; done: number; failed: number; active: number }): NightOutcome {
  if (night.active > 0) return "active";
  if (night.failed > 0) return "failed";
  if (night.runs > 0) return "done";
  return "empty";
}

/** "12.3%" of what a night may cost, for its meter; 0 without a budget. */
export function budgetShare(cost: number, budget: number | null): number {
  if (budget === null || budget <= 0) return 0;
  return Math.min(1, Math.max(0, cost / budget));
}
