import type { Probe, ProbeFix, ProbeStatus, ProbeWeek, ProbeWeekly, ProbeWeekResult } from "./probe-queries";

/**
 * How the probe report reads on the page, without React: which statuses wear a flag (all but `holds`, which is the
 * usual and so says nothing), a week's flagged results counted, the rows of one probe across the weeks, the fix
 * proposals of a change, and a task's first line as the name of its probe. The statuses and their counts are the hub's.
 */

/** The statuses that say something is wrong, in the order a week's summary names them. */
export const FLAGGED = ["recurring", "not_reproduced", "stale", "unmeasured"] as const satisfies readonly ProbeStatus[];
export type FlaggedStatus = (typeof FLAGGED)[number];

export function isFlagged(status: ProbeStatus): status is FlaggedStatus {
  return status !== "holds";
}

/** A week's flagged results by status, those with none left out, in the order of `FLAGGED`. */
export function flaggedCounts(week: ProbeWeek): { status: FlaggedStatus; count: number }[] {
  return FLAGGED.map((status) => ({ status, count: week.totals[status] })).filter((item) => item.count > 0);
}

export function flaggedTotal(week: ProbeWeek): number {
  return FLAGGED.reduce((sum, status) => sum + week.totals[status], 0);
}

/** Whether the report has anything to show: a probe budget, a result in some week, a change without a probe, a fix. */
export function reportShown(report: ProbeWeekly): boolean {
  return (
    report.budget_usd !== null ||
    report.weeks.some((week) => week.probes.length > 0) ||
    report.unprobed.length > 0 ||
    report.fixes.length > 0
  );
}

/** The weeks that measured probe `probe`, newest first, each with that probe's results (one per runtime and model). */
export function weeksOfProbe(report: ProbeWeekly, probe: number): { week: ProbeWeek; results: ProbeWeekResult[] }[] {
  return report.weeks
    .map((week) => ({ week, results: week.probes.filter((item) => item.probe_id === probe) }))
    .filter((item) => item.results.length > 0);
}

/** The fix proposals of change `change`, as the report lists them (the newest first). */
export function fixesOf(report: ProbeWeekly, change: number | null): ProbeFix[] {
  return change === null ? [] : report.fixes.filter((fix) => fix.change_id === change);
}

/** The first line of a probe's task, cut at `max` characters with an ellipsis, as the name of the probe in a row. */
export function taskLine(task: string, max = 120): string {
  const line = task.trim().split("\n", 1)[0]?.trim() ?? "";
  return line.length > max ? `${line.slice(0, max - 1).trimEnd()}…` : line;
}

/** The project's probes by id, for the rows of the report to name. */
export function probesById(probes: readonly Probe[]): Map<number, Probe> {
  return new Map(probes.map((probe) => [probe.id, probe]));
}
