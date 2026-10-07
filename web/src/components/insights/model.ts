import type { RunFigures, RunStats } from "./queries";

/**
 * The Insights page's figures, read from GET .../runs/stats as the charts and tables show them. Nothing here is
 * estimated: a day without a run that started has no duration (null, a gap in the line), and a day without a run that
 * ended done, failed or lost has no failure rate.
 */

/** A chart's height with its axes: the card keeps it while Recharts loads, so nothing moves when it arrives. */
export const CHART_HEIGHT = 224;

/** A day's figures, or the whole range's (`day` null), flattened for the charts and tables. */
export type Figures = {
  done: number;
  failed: number;
  lost: number;
  cancelled: number;
  /** Lost and cancelled together: one neutral segment of the outcome chart, as both are neutral states. */
  stopped: number;
  /** Every run that ended, in any of the four states. */
  ended: number;
  /** Failed or lost, of `counted`: a run cancelled by a person is no failure of the agent, so it is left out. */
  failing: number;
  counted: number;
  /** `failing` over `counted`, from 0 to 1; null when nothing was counted. */
  rate: number | null;
  /** The 50th and 90th percentile of how long the runs ran, in seconds; null when none of them started. */
  p50: number | null;
  p90: number | null;
  cacheRead: number;
  input: number;
  output: number;
  reasoning: number;
  tokens: number;
  /** The runs whose usage the hub could read; the others add no token. */
  withUsage: number;
};

export type DayRow = Figures & { day: string };

/** The outcome chart's stack, bottom to top, in `success-solid`, `danger-solid` and `neutral-solid`. */
export const OUTCOME_SERIES = ["done", "failed", "stopped"] as const;
export type OutcomeSeries = (typeof OUTCOME_SERIES)[number];

/** The token chart's stack, bottom to top, in the UsageMeter's order and colours (`chart-5`, `chart-1`, `chart-3`, `chart-4`). */
export const TOKEN_SERIES = ["cacheRead", "input", "output", "reasoning"] as const;
export type TokenSeries = (typeof TOKEN_SERIES)[number];

/** The duration chart's two lines: the median, the main measure (`chart-1`), and the 90th percentile (`chart-2`). */
export const DURATION_SERIES = ["p50", "p90"] as const;
export type DurationSeries = (typeof DURATION_SERIES)[number];

/**
 * The mark of each series, a CSS colour that follows the theme: outcomes in their state's solid mark (web/DESIGN.md,
 * States), the failure rate in the failed one, durations in `chart-1` and `chart-2` in order, tokens as the UsageMeter
 * draws them. Text never wears these; a swatch or a line key beside it does.
 */
export const SERIES_COLORS = {
  done: "var(--success-solid)",
  failed: "var(--danger-solid)",
  stopped: "var(--neutral-solid)",
  lost: "var(--neutral-solid)",
  cancelled: "var(--neutral-solid)",
  rate: "var(--danger-solid)",
  p50: "var(--chart-1)",
  p90: "var(--chart-2)",
  cacheRead: "var(--chart-5)",
  input: "var(--chart-1)",
  output: "var(--chart-3)",
  reasoning: "var(--chart-4)",
} as const;
export type SeriesKey = keyof typeof SERIES_COLORS;

export function figuresOf(source: RunFigures): Figures {
  const { done, failed, lost, cancelled } = source;
  const failing = failed + lost;
  const counted = done + failing;
  const tokens = source.cache_read_tokens + source.input_tokens + source.output_tokens + source.reasoning_tokens;
  return {
    done,
    failed,
    lost,
    cancelled,
    stopped: lost + cancelled,
    ended: done + failed + lost + cancelled,
    failing,
    counted,
    rate: counted > 0 ? failing / counted : null,
    p50: source.p50_seconds,
    p90: source.p90_seconds,
    cacheRead: source.cache_read_tokens,
    input: source.input_tokens,
    output: source.output_tokens,
    reasoning: source.reasoning_tokens,
    tokens,
    withUsage: source.runs_with_usage,
  };
}

/** Every day of the range, the oldest first, as the API lists them. */
export function dayRows(stats: Pick<RunStats, "by_day">): DayRow[] {
  return stats.by_day.map((day) => ({ day: day.day, ...figuresOf(day) }));
}

/**
 * The series of a stacked day that hold a value, lowest and highest: the highest gets the rounded data end, and each
 * one above the lowest stands 2 px off the one below it. Null for a day with nothing in the stack.
 */
export function stackEnds<K extends string>(row: Record<K, number>, keys: readonly K[]): { bottom: K | null; top: K | null } {
  const shown = keys.filter((key) => row[key] > 0);
  return { bottom: shown[0] ?? null, top: shown[shown.length - 1] ?? null };
}

/** A UTC day ("2026-10-07") as a Date at its midnight in UTC, to format with `timeZone: "UTC"`. */
export function utcMidnight(day: string): Date {
  return new Date(`${day}T00:00:00Z`);
}

/** Whole hours, minutes and seconds of a duration in seconds, rounded to the second. */
export function durationParts(total: number): { hours: number; minutes: number; seconds: number } {
  const whole = Math.max(0, Math.round(total));
  return { hours: Math.floor(whole / 3600), minutes: Math.floor(whole / 60) % 60, seconds: whole % 60 };
}

/** Steps of the duration axis, in seconds: each a duration people count in. */
const DURATION_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10_800, 21_600, 43_200, 86_400];

/**
 * The duration axis's ticks for values up to `max` seconds: from 0, at most `count` steps of a round duration (15 s,
 * 5 min, 1 h), the last at or above `max`.
 */
export function durationTicks(max: number, count = 4): number[] {
  const top = Number.isFinite(max) && max > 0 ? max : 60;
  const step = DURATION_STEPS.find((candidate) => candidate * count >= top) ?? Math.ceil(top / count / 86_400) * 86_400;
  const steps = Math.max(1, Math.ceil(top / step));
  return Array.from({ length: steps + 1 }, (_, index) => index * step);
}

/** The highest p90 (else p50) of the days, for the duration axis. */
export function longestDuration(rows: readonly Pick<DayRow, "p50" | "p90">[]): number {
  return rows.reduce((max, row) => Math.max(max, row.p90 ?? 0, row.p50 ?? 0), 0);
}
