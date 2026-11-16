import type { AccountPool, PoolAccount, PoolPolicy } from "./account-pools-queries";
import { CEILING_PCT, RESERVE_PCT } from "./model";
import { type AccountUsage, STRATEGIES, type Strategy } from "./queries";

/**
 * A pool's policy as its card edits it (docs/credentials.md, section "Account pools"): the strategy a run of its
 * agents picks an account by, and quota-pacing's reserve and ceiling; and the usage of each account it holds.
 */

export type PoolPolicyForm = { strategy: Strategy; reservePct: string; ceilingPct: string };

const isStrategy = (value: string): value is Strategy => (STRATEGIES as readonly string[]).includes(value);

/** The form of a pool's policy; a strategy this page does not know reads as round-robin. */
export const poolPolicyFormOf = (pool: Pick<AccountPool, "strategy" | "hard_reserve_pct" | "five_hour_ceiling_pct">): PoolPolicyForm => ({
  strategy: isStrategy(pool.strategy) ? pool.strategy : "round-robin",
  reservePct: String(pool.hard_reserve_pct),
  ceilingPct: String(pool.five_hour_ceiling_pct),
});

const percent = (text: string, bounds: { min: number; max: number }) => {
  const trimmed = text.trim();
  if (!/^\d+$/.test(trimmed)) return { value: null, problem: "whole" as const };
  const value = Number(trimmed);
  return value < bounds.min || value > bounds.max ? { value: null, problem: "range" as const } : { value, problem: null };
};

/** The problems of a pool's policy form; the thresholds are checked under quota-pacing alone. */
export function poolPolicyProblems(form: PoolPolicyForm): Partial<Record<"reservePct" | "ceilingPct", "whole" | "range">> {
  if (form.strategy !== "quota-pacing") return {};
  const problems: Partial<Record<"reservePct" | "ceilingPct", "whole" | "range">> = {};
  const reserve = percent(form.reservePct, RESERVE_PCT).problem;
  const ceiling = percent(form.ceilingPct, CEILING_PCT).problem;
  if (reserve) problems.reservePct = reserve;
  if (ceiling) problems.ceilingPct = ceiling;
  return problems;
}

/**
 * What a PATCH of the pool sends: what changed from `pool`, null when nothing did. The thresholds go only under
 * quota-pacing, the one strategy that reads them.
 */
export function poolPolicyBody(form: PoolPolicyForm, pool: Pick<AccountPool, "strategy" | "hard_reserve_pct" | "five_hour_ceiling_pct">): PoolPolicy | null {
  const body: PoolPolicy = {};
  if (form.strategy !== pool.strategy) body.strategy = form.strategy;
  if (form.strategy === "quota-pacing") {
    const reserve = percent(form.reservePct, RESERVE_PCT).value;
    const ceiling = percent(form.ceilingPct, CEILING_PCT).value;
    if (reserve !== null && reserve !== pool.hard_reserve_pct) body.hard_reserve_pct = reserve;
    if (ceiling !== null && ceiling !== pool.five_hour_ceiling_pct) body.five_hour_ceiling_pct = ceiling;
  }
  return Object.keys(body).length > 0 ? body : null;
}

const STATUSES = new Set(["allowed", "allowed_warning", "rejected"]);

/** A pool account's last usage as the usage bars read it, or null when the hub kept none or one of another shape. */
export function poolUsage(account: Pick<PoolAccount, "last_usage">): AccountUsage | null {
  const usage = account.last_usage;
  if (!usage || typeof usage.status !== "string" || !STATUSES.has(usage.status)) return null;
  return usage as AccountUsage;
}
