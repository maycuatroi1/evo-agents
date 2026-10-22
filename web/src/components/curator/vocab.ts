import type { Lens, ProposalState, Runtime } from "./queries";

/**
 * The hub's vocabularies and limits as the Curator pages need them. Every limit repeats one of the API's
 * (evo_agents.hub.curator, evo_agents.hub.review, evo_agents.hub.replay), so a form refuses what the hub would refuse
 * before anything is sent; the hub decides again.
 */

/** `review.LENSES`, in the order the nights take them. */
export const LENSES = [
  "tool_errors",
  "environment",
  "corrections",
  "failed_runs",
  "runtime",
  "tech_debt",
  "code_health",
  "docs_drift",
  "harness",
  "skills_memory",
  "cost",
  "security",
  "product_goals",
  "product",
  "kg_health",
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
  /** `replay.MAX_TASKS`: the holdout tasks each change of a skill or an AGENTS.md is replayed on. */
  replayTasks: { min: 0, max: 20 },
  timeZoneChars: 64,
  modelChars: 200,
} as const;

/** `replay.MIN_HOLDOUT`: the holdout tasks a change's replays need, both arms at its head, before they can win. */
export const MIN_HOLDOUT = 6;
/** `replay.MIN_SAVING`: with as many tasks passed, arm b's mean cost or turns this share lower than arm a's wins. */
export const MIN_SAVING = 0.1;

export const TIME_OF_DAY = /^([01][0-9]|2[0-3]):[0-5][0-9]$/;
export const TIME_ZONE = /^[A-Za-z][A-Za-z0-9_+-]*(\/[A-Za-z0-9_+-]+)*$/;
export const GOAL_ID = /^[a-z0-9][a-z0-9-]{0,63}$/;
export const WORKER_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;
/** One line without control characters: `runs.LINE`. */
export const LINE = /^[^\u0000-\u001f\u007f]+$/;

export function isLens(value: string | null | undefined): value is Lens {
  return (LENSES as readonly string[]).includes(value ?? "");
}
