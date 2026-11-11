import type { Charter } from "./queries";

export type Pacing = NonNullable<Charter["pacing"]>;

/**
 * When the Curator works: in its window, or through the period of its `pacing` outside the quiet hours. The zone is
 * the window's, or the pacing's.
 */
export type ShiftHours =
  | { kind: "window"; start: string; end: string; timezone: string }
  | { kind: "paced"; period: Pacing["period"]; quiet: { start: string; end: string } | null; timezone: string };

export function shiftHours(charter: Pick<Charter, "window" | "pacing">): ShiftHours {
  if (charter.pacing) {
    const { period, quiet_hours: quiet, timezone } = charter.pacing;
    return { kind: "paced", period, quiet: quiet ? { start: quiet.start, end: quiet.end } : null, timezone };
  }
  const window = charter.window ?? { start: "22:00", end: "06:00", timezone: "UTC" };
  return { kind: "window", start: window.start, end: window.end, timezone: window.timezone };
}

/** What a night may cost with a window, or a period with pacing. */
export function periodBudget(charter: Pick<Charter, "night_budget_usd" | "pacing">): number {
  return charter.pacing ? charter.pacing.budget_usd : (charter.night_budget_usd ?? 0);
}
