import type { ChangeReplay, Replay } from "./insight-queries";
import { MIN_HOLDOUT } from "./vocab";

/**
 * How a change's card reads its replays (docs/curator.md, Replays; evo_agents.hub.replay): the verdict at the head of
 * its pull request (whether arm b, the change's harness, beat arm a, the default branch's, on enough holdout tasks),
 * each arm's means over the tasks both arms ran, and each replay's own result. Read defensively: the arms and the
 * verdict are JSON objects the hub wrote.
 */

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

export type ArmMeans = { passed: number; cost: number | null; turns: number | null };

export type Verdict = {
  won: boolean;
  reason: string | null;
  /** Holdout tasks replayed with both arms at the head. */
  tasks: number;
  /** Of the MIN_HOLDOUT a win needs. */
  needed: number;
  a: ArmMeans;
  b: ArmMeans;
  /** b's saving over a: 1 - b / a, of the mean cost and of the mean turns. */
  saving: { cost: number | null; turns: number | null };
  paths: string[];
};

function means(value: unknown): ArmMeans {
  const found = record(value);
  return { passed: Math.max(0, num(found.passed) ?? 0), cost: num(found.cost_usd), turns: num(found.turns) };
}

export function verdictOf(value: unknown): Verdict | null {
  if (typeof value !== "object" || value === null) return null;
  const found = record(value);
  const saving = record(found.saving);
  return {
    won: found.won === true,
    reason: typeof found.reason === "string" ? found.reason : null,
    tasks: Math.max(0, num(found.tasks) ?? 0),
    needed: MIN_HOLDOUT,
    a: means(found.a),
    b: means(found.b),
    saving: { cost: num(saving.cost_usd), turns: num(saving.turns) },
    paths: Array.isArray(found.paths) ? found.paths.filter((item): item is string => typeof item === "string") : [],
  };
}

/** What both arms add up to over the replays that reported them: tool errors, agent time and the largest context. */
export type ArmTotals = { replays: number; passed: number; toolErrors: number | null; durationMs: number | null; peak: number | null };

export function armTotals(arms: ChangeReplay["arms"], arm: "a" | "b"): ArmTotals {
  const found = record(record(arms)[arm]);
  return {
    replays: Math.max(0, num(found.replays) ?? 0),
    passed: Math.max(0, num(found.passed) ?? 0),
    toolErrors: num(found.tool_errors),
    durationMs: num(found.duration_ms),
    peak: num(found.peak_context_tokens),
  };
}

/** One arm of one replay: whether its verify passed, or null while it has not reported. */
export function armPassed(replay: Pick<Replay, "a" | "b">, arm: "a" | "b"): boolean | null {
  const result = replay[arm];
  if (typeof result !== "object" || result === null) return null;
  return (result as { passed?: unknown }).passed === true;
}

/** Whether the change merges at a lower tier than its proposal's, because its replays won. */
export function lowered(changeTier: number, proposalTier: number, verdict: Verdict | null): boolean {
  return changeTier < proposalTier && verdict?.won === true;
}
