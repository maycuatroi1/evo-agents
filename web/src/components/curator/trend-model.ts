/**
 * A figure over time as the Curator's trend cards draw it (trend-card.tsx, trend-charts.tsx): one row per night or per
 * measure, oldest first, each holding a value per series or null where the source had none (a gap, never a zero).
 */

/** The chart's height with its axes: the card keeps it while Recharts loads, so nothing moves when it arrives. */
export const TREND_HEIGHT = 224;

/** The palette of web/DESIGN.md (Charts), in order: the first series is always the main measure. */
export const TREND_COLORS = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)"] as const;

export type TrendSeries = {
  /** A key of each row's values, also the CSS name of its colour (`--color-<key>`): letters, digits and `_` only. */
  key: string;
  label: string;
  color: string;
};

export type TrendRow = {
  /** The night (YYYY-MM-DD) or the moment (ISO 8601) of the row, its id in the table. */
  at: string;
  /** The row as the axis names it ("Oct 7"), and as the tooltip and the table name it ("Oct 7, 2026"). */
  tick: string;
  title: string;
  values: Record<string, number | null>;
};

export type TrendKind = "bars" | "lines";

/** What a value reads as on the axis and in the tooltip and table: a count, a share, dollars, a duration. */
export type TrendFormat = (value: number) => string;

/** A series of `TREND_COLORS[index]`, so a card's series take the palette in order. */
export function series(keys: readonly { key: string; label: string }[]): TrendSeries[] {
  return keys.map((item, index) => ({ ...item, color: TREND_COLORS[index % TREND_COLORS.length] }));
}

/** The rows as Recharts reads them: flat, each series' value under its key, null kept so a line breaks there. */
export function chartData(rows: readonly TrendRow[]): Record<string, string | number | null>[] {
  return rows.map((row) => ({ at: row.at, tick: row.tick, title: row.title, ...row.values }));
}

/**
 * The series of a stacked row that hold a value, lowest and highest: the highest gets the rounded data end, and each
 * one above the lowest stands 2 px off the one below it. Null for a row with nothing in the stack.
 */
export function stackEnds(values: Record<string, number | null>, keys: readonly string[]): { bottom: string | null; top: string | null } {
  const shown = keys.filter((key) => (values[key] ?? 0) > 0);
  return { bottom: shown[0] ?? null, top: shown[shown.length - 1] ?? null };
}

/** The sum of a stacked row; null when no series holds a value. */
export function stackTotal(values: Record<string, number | null>, keys: readonly string[]): number | null {
  const held = keys.map((key) => values[key]).filter((value): value is number => typeof value === "number");
  return held.length > 0 ? held.reduce((sum, value) => sum + value, 0) : null;
}

/** Whether any row holds a value of any series: a card with none says so instead of drawing empty axes. */
export function hasValues(rows: readonly TrendRow[], keys: readonly string[]): boolean {
  return rows.some((row) => keys.some((key) => typeof row.values[key] === "number"));
}

/** A column's segment: square, or with the stack's rounded data end on top (4 px). */
export function segmentPath(x: number, y: number, width: number, height: number, round: boolean): string {
  const r = round ? Math.min(4, width / 2, height) : 0;
  if (r <= 0) return `M${x},${y}h${width}v${height}h${-width}Z`;
  return `M${x},${y + height}V${y + r}A${r},${r} 0 0 1 ${x + r},${y}H${x + width - r}A${r},${r} 0 0 1 ${x + width},${y + r}V${y + height}Z`;
}

/** The latest value of a series, the last row that holds one; null when none does. */
export function latest(rows: readonly TrendRow[], key: string): number | null {
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const value = rows[index].values[key];
    if (typeof value === "number") return value;
  }
  return null;
}

/** The values of a series, oldest first, nulls as 0, for a strip's sparkline; null with fewer than two values. */
export function sparkValues(rows: readonly TrendRow[], key: string): number[] | null {
  const held = rows.filter((row) => typeof row.values[key] === "number");
  return held.length >= 2 ? rows.map((row) => row.values[key] ?? 0) : null;
}
