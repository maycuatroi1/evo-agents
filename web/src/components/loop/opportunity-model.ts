import {
  OPPORTUNITY_STATUSES,
  type OpportunityStatus,
  type ProjectOpportunity,
} from "@/lib/opportunity-queries";
import { ANSWER_REF, LOGIN_NAME, OPPORTUNITY_LIMITS, type OpportunityWrite } from "@/lib/opportunity-writes";

/**
 * The shapes of the Opportunities pages (plan opportunity-intake): the list's order (the nearest closing window first,
 * none last) and its filter in the address, the days a window has left, and the fields of the form a writer fills.
 * Pure: no React, no query.
 */

export function isOpportunityStatus(value: string | null): value is OpportunityStatus {
  return value !== null && (OPPORTUNITY_STATUSES as readonly string[]).includes(value);
}

/** The list's filter from its address (`?status=open`); a status the hub does not know reads as every status. */
export function readStatusFilter(params: { get: (name: string) => string | null }): OpportunityStatus | null {
  const status = params.get("status");
  return isOpportunityStatus(status) ? status : null;
}

/**
 * The opportunities by the end of their window, the nearest first (one already past included) and those with no date
 * last; by rank, then by code, among equal ends.
 */
export function byClosingWindow<T extends Pick<ProjectOpportunity, "window_until" | "rank" | "number">>(opportunities: readonly T[]): T[] {
  const end = (one: T) => (one.window_until ? Date.parse(`${one.window_until}T00:00:00Z`) : Number.POSITIVE_INFINITY);
  return [...opportunities].sort((a, b) => {
    const [x, y] = [end(a), end(b)];
    if (x !== y) return x < y ? -1 : 1;
    return a.rank - b.rank || a.number - b.number;
  });
}

export function statusCounts(opportunities: readonly Pick<ProjectOpportunity, "status">[]): Record<OpportunityStatus, number> {
  const counts: Record<OpportunityStatus, number> = { open: 0, captured: 0, missed: 0, dropped: 0 };
  for (const one of opportunities) counts[one.status] += 1;
  return counts;
}

/** Whole days from `now` to the end of the window (negative once past, 0 on the last day); null without a window date. */
export function daysLeft(windowUntil: string | null, now: number): number | null {
  if (!windowUntil) return null;
  const end = Date.parse(`${windowUntil}T00:00:00Z`);
  if (Number.isNaN(end)) return null;
  const today = new Date(now);
  const start = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
  return Math.round((end - start) / 86_400_000);
}

/** The answers as people type them (commas, spaces or lines apart) into codes, each once, in order. */
export function parseAnswers(text: string): string[] {
  return [...new Set(text.split(/[\s,;]+/).filter(Boolean))];
}

// The form

/** An opportunity as its form holds it: every field as typed, numbers as text until they are sent. */
export type OpportunityForm = {
  statement: string;
  beneficiary: string;
  leverage: string;
  windowUntil: string;
  windowNote: string;
  answers: string;
  rank: string;
  owner: string;
  note: string;
};

export type OpportunityField = "statement" | "beneficiary" | "leverage" | "windowUntil" | "answers" | "rank" | "owner";

export const EMPTY_OPPORTUNITY: OpportunityForm = {
  statement: "",
  beneficiary: "",
  leverage: "",
  windowUntil: "",
  windowNote: "",
  answers: "",
  rank: "",
  owner: "",
  note: "",
};

/** The form of an edit: the opportunity at the revision the page read, with no note yet. */
export function formOf(held: ProjectOpportunity): OpportunityForm {
  return {
    statement: held.statement,
    beneficiary: held.beneficiary,
    leverage: held.leverage,
    windowUntil: held.window_until ?? "",
    windowNote: held.window_note ?? "",
    answers: held.answers.join(", "),
    rank: String(held.rank),
    owner: held.owner,
    note: "",
  };
}

function wholeIn(text: string, low: number, high: number): boolean {
  return /^\d+$/.test(text) && Number(text) >= low && Number(text) <= high;
}

function isDay(text: string): boolean {
  const found = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (!found) return false;
  const date = new Date(Date.UTC(Number(found[1]), Number(found[2]) - 1, Number(found[3])));
  return date.toISOString().startsWith(text);
}

/** The fields the hub would refuse, in the form's order. */
export function formProblems(form: OpportunityForm): OpportunityField[] {
  const wrong: OpportunityField[] = [];
  for (const field of ["statement", "beneficiary", "leverage"] as const) if (!form[field].trim()) wrong.push(field);
  if (form.windowUntil.trim() && !isDay(form.windowUntil.trim())) wrong.push("windowUntil");
  const answers = parseAnswers(form.answers);
  if (answers.length > OPPORTUNITY_LIMITS.answers || answers.some((answer) => !ANSWER_REF.test(answer))) wrong.push("answers");
  if (form.rank.trim() && !wholeIn(form.rank.trim(), 1, OPPORTUNITY_LIMITS.rank)) wrong.push("rank");
  if (form.owner.trim() && !LOGIN_NAME.test(form.owner.trim())) wrong.push("owner");
  return wrong;
}

/**
 * The write of `form`: a new opportunity (`held` null, `if_revision` 0, so it never overwrites one another writer made)
 * or the next revision of `held` at the revision the page read, keeping its status, the reason it was closed and its
 * metric (which the form does not edit).
 */
export function writeOf(form: OpportunityForm, held: ProjectOpportunity | null): OpportunityWrite {
  return {
    statement: form.statement.trim(),
    beneficiary: form.beneficiary.trim(),
    leverage: form.leverage.trim(),
    window_until: form.windowUntil.trim() || null,
    window_note: form.windowNote.trim() || null,
    answers: parseAnswers(form.answers),
    metric: held?.metric ?? null,
    rank: form.rank.trim() ? Number(form.rank.trim()) : null,
    owner: form.owner.trim() || null,
    status: held?.status ?? "open",
    close_reason: held?.close_reason ?? null,
    note: form.note.trim() || null,
    if_revision: held ? held.revision : 0,
  };
}

/** The fields a revision names as changed, in the hub's order (`opportunities.FIELDS`), for the words of its history. */
export const REVISION_FIELDS = [
  "statement",
  "beneficiary",
  "leverage",
  "window_until",
  "window_note",
  "answers",
  "metric",
  "rank",
  "owner",
  "status",
  "close_reason",
] as const;
export type RevisionField = (typeof REVISION_FIELDS)[number];

export function isRevisionField(value: string): value is RevisionField {
  return (REVISION_FIELDS as readonly string[]).includes(value);
}

/** The evidence signals, in the hub's order (`opportunities.SIGNALS`): a problem's, then the opportunity's own. */
export const EVIDENCE_SIGNALS = [
  "complaint",
  "workaround",
  "repeated_request",
  "blocker",
  "commitment",
  "proposed_solution",
  "contradiction",
  "decision_without_reason",
  "wish",
  "external_demand",
  "window",
  "reusable_asset",
  "pull",
  "intro",
  "idle_capacity",
] as const;

/** The lines grouped by signal, the groups in the hub's order and the lines by number inside each; an unknown signal last. */
export function bySignal<T extends { signal: string; number: number }>(lines: readonly T[]): { signal: string; lines: T[] }[] {
  const order = (signal: string) => {
    const at = (EVIDENCE_SIGNALS as readonly string[]).indexOf(signal);
    return at === -1 ? EVIDENCE_SIGNALS.length : at;
  };
  const groups = new Map<string, T[]>();
  for (const line of [...lines].sort((a, b) => a.number - b.number)) groups.set(line.signal, [...(groups.get(line.signal) ?? []), line]);
  return [...groups.entries()].map(([signal, grouped]) => ({ signal, lines: grouped })).sort((a, b) => order(a.signal) - order(b.signal));
}
