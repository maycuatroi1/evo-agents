import type { LedgerLine } from "./queries";

/**
 * How the web reads a proposal's ledger (docs/hub.md, The Curator's ledger): the tone of a line's mark, a commit as
 * people read it, and the figures a line holds, read defensively since `figures` is a JSON object the hub wrote.
 */

/** `ledger.ACTIONS`, `ledger.ACTORS` and `ledger.OUTCOMES`, in the hub's order. */
export const LEDGER_ACTIONS = [
  "proposed",
  "dropped",
  "accepted",
  "rejected",
  "deferred",
  "planned",
  "built",
  "build_failed",
  "pull_opened",
  "judged",
  "merged",
  "left_open",
  "closed",
  "outcome",
] as const satisfies readonly LedgerLine["action"][];
export const LEDGER_ACTORS = ["curator", "agent", "user"] as const satisfies readonly LedgerLine["actor"][];
export const LEDGER_OUTCOMES = ["keep", "revert", "unclear"] as const satisfies readonly NonNullable<LedgerLine["outcome"]>[];

export type LedgerTone = "success" | "danger" | "attention" | "neutral";
export type FigureChange = "better" | "same" | "worse";

/** The figures that set a proposal off: what each counts and its count. */
export type TriggerFigure = { key: string; what: string; value: number };
/** A figure of an outcome: its count and its share of the sessions and runs, before and after the merge. */
export type OutcomeFigure = TriggerFigure & { after: number; beforeRate: number; afterRate: number; change: FigureChange };

/**
 * The mark of a line: success for a merge, a Judge that passed and an outcome kept; danger for a Builder that failed, a
 * Judge that failed and an outcome to revert; attention for a change left open for its owner; neutral otherwise.
 */
export function ledgerTone(line: Pick<LedgerLine, "action" | "outcome" | "verdict">): LedgerTone {
  const passed = (line.verdict as { passed?: unknown } | null)?.passed;
  switch (line.action) {
    case "merged":
      return "success";
    case "build_failed":
      return "danger";
    case "left_open":
      return "attention";
    case "judged":
      return passed === true ? "success" : "danger";
    case "outcome":
      return line.outcome === "keep" ? "success" : line.outcome === "revert" ? "danger" : "neutral";
    default:
      return "neutral";
  }
}

/** A commit as its first 12 characters, as `git log --abbrev=12` writes it. */
export function shortSha(sha: string): string {
  return sha.slice(0, 12);
}

function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function metrics(figures: unknown): Record<string, unknown>[] {
  const found = (figures as { metrics?: unknown } | null)?.metrics;
  return Array.isArray(found) ? found.filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null) : [];
}

/** The figures that set a proposal off, from a `proposed` line's `figures`; the ones the hub wrote whole only. */
export function triggerFigures(figures: unknown): { activity: number | null; figures: TriggerFigure[] } {
  const found: TriggerFigure[] = [];
  for (const item of metrics(figures)) {
    const key = text(item.key);
    const value = number(item.value);
    if (key !== null && value !== null) found.push({ key, what: text(item.what) ?? key, value });
  }
  return { activity: number((figures as { activity?: unknown } | null)?.activity), figures: found };
}

const CHANGES: readonly FigureChange[] = ["better", "same", "worse"];

/** The figures of an outcome, before and after the merge, from an `outcome` line's `figures`. */
export function outcomeFigures(figures: unknown): {
  before: number | null;
  after: number | null;
  reason: string | null;
  figures: OutcomeFigure[];
} {
  const found: OutcomeFigure[] = [];
  for (const item of metrics(figures)) {
    const key = text(item.key);
    const before = number(item.before);
    const after = number(item.after);
    const change = CHANGES.find((value) => value === item.change);
    if (key === null || before === null || after === null || !change) continue;
    found.push({
      key,
      what: text(item.what) ?? key,
      value: before,
      after,
      beforeRate: number(item.before_rate) ?? 0,
      afterRate: number(item.after_rate) ?? 0,
      change,
    });
  }
  const held = figures as { activity_before?: unknown; activity_after?: unknown; reason?: unknown } | null;
  return {
    before: number(held?.activity_before),
    after: number(held?.activity_after),
    reason: text(held?.reason),
    figures: found,
  };
}

/** Whether a figure counts dollars rather than things: the cost of the runs. */
export function isMoney(key: string): boolean {
  return key === "cost_usd";
}

/** The revert the hub proposed on an `outcome` line, when it did. */
export function revertProposal(line: Pick<LedgerLine, "details">): number | null {
  const found = (line.details as { revert_proposal_id?: unknown } | null)?.revert_proposal_id;
  return typeof found === "number" && Number.isInteger(found) && found > 0 ? found : null;
}

/** A pull request's address when it is one a browser may open: https only. */
export function safeUrl(url: string | null): string | null {
  return url !== null && url.startsWith("https://") ? url : null;
}
