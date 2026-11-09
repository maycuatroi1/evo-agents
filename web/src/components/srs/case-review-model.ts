import { diffWords, MIN_SHARED, sharedShare, type WordPart } from "@/lib/word-diff";

import type { SrsDocument, SrsFinding, SrsItem, SrsReviewRun, SrsUseCase, SrsVersion } from "./api";
import { rulesOf, stepsOf } from "./content-model";
import { FINDING_LEVEL_ORDER, isDeciding, reviewHolds } from "./findings-model";
import type { ItemLinks } from "./item-body";
import type { ReviewBlock } from "./model";

/**
 * The pure model of a use case's review (/p/{project}/x/srs/documents/{slug}/versions/{n}/cases/{key}): the fields a
 * first-time reviewer reads, what this version changed in them and in the business rules under the use case compared
 * with the version approved last, @ba's open findings about it, and what lets the viewer approve it now. Nothing here
 * reads the API; the hub stays the gate of every write.
 */

/**
 * What the cards of the page read: the version under review and its items, the use case in it (null when this version
 * drops it), the version approved last it is compared with (`approved`, null when there is none older to compare
 * with) with its items and the use case in it (null when this version adds it), whether that version was read
 * (`compared`), where the use case stands in the review (null when the version needs no review of it) and where its
 * codes link.
 */
export type CaseView = {
  project: string;
  slug: string;
  latest: SrsDocument;
  version: SrsVersion;
  items: SrsItem[];
  item: SrsItem | null;
  approved: number | null;
  compared: boolean;
  baseItems: SrsItem[];
  base: SrsItem | null;
  useCase: SrsUseCase | null;
  links: ItemLinks;
};

/** The use case as the page reads it: in this version, else as the version approved last holds it. */
export function shownCase(view: Pick<CaseView, "item" | "base" | "items" | "baseItems">): { item: SrsItem; items: SrsItem[] } | null {
  if (view.item) return { item: view.item, items: view.items };
  return view.base ? { item: view.base, items: view.baseItems } : null;
}

/** The fields the page shows, in the order a reader takes them, each read from its key or an older template's. */
export const CASE_FIELDS = [
  { key: "actor", from: ["actor"] },
  { key: "intent", from: ["intent", "objective"] },
  { key: "context", from: ["context"] },
  { key: "trigger", from: ["trigger"] },
  { key: "pre_condition", from: ["pre_condition"] },
  { key: "expectation", from: ["expectation", "post_condition"] },
] as const;
export type CaseField = (typeof CASE_FIELDS)[number]["key"];

/** What this version did to a field or a rule, compared with the version approved last. */
export type Change = "same" | "added" | "changed" | "removed";

function fieldText(item: SrsItem | null, from: readonly string[]): string | null {
  if (!item) return null;
  for (const name of from) {
    const value = item.fields[name];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number") return String(value);
  }
  return null;
}

const same = (a: string | null, b: string | null) => (a ?? "").replace(/\s+/g, " ").trim() === (b ?? "").replace(/\s+/g, " ").trim();

export type FieldRow = { key: CaseField; before: string | null; after: string | null; change: Change };

/**
 * The fields of the use case as the page lists them, each with its text in the approved version (`before`) and in this
 * one (`after`). With no use case on one side (a new one, or one this version drops) nothing is marked: the page says
 * so once for the whole use case. A field empty on both sides is left out.
 */
export function fieldRows(before: SrsItem | null, after: SrsItem | null): FieldRow[] {
  const compared = before !== null && after !== null;
  return CASE_FIELDS.flatMap(({ key, from }) => {
    const old = fieldText(before, from);
    const now = fieldText(after, from);
    if (old === null && now === null) return [];
    const change: Change = !compared || same(old, now) ? "same" : old === null ? "added" : now === null ? "removed" : "changed";
    return [{ key, before: compared ? old : null, after: after ? now : old, change }];
  });
}

/**
 * A field longer than REWRITE_MIN_CHARS (its two texts together) that keeps less than REWRITE_SHARED of them, or changes
 * more than REWRITE_RUNS runs, reads as rewritten (`fieldView`); a short one reads word by word whatever it changed.
 */
export const REWRITE_MIN_CHARS = 200;
export const REWRITE_SHARED = 0.7;
export const REWRITE_RUNS = 8;

/**
 * How a changed field reads best: word by word when the change is small, else as rewritten, its new text shown whole
 * and the word-by-word view one click away; a long rewrite marked word by word is a wall of tints nobody reads.
 */
export function fieldView(before: string, after: string): { kind: "words"; parts: WordPart[] } | { kind: "rewritten" } {
  const parts = fieldParts(before, after);
  if (before.length + after.length <= REWRITE_MIN_CHARS) return { kind: "words", parts };
  const runs = parts.filter((part) => part.kind !== "same").length;
  return sharedShare(parts) >= REWRITE_SHARED && runs <= REWRITE_RUNS ? { kind: "words", parts } : { kind: "rewritten" };
}

/** The words of a changed field: kept, removed and added runs; the whole text replaced when they share too little. */
export function fieldParts(before: string, after: string): WordPart[] {
  const parts = diffWords(before, after);
  if (parts && sharedShare(parts) >= MIN_SHARED) return parts;
  return [
    ...(before ? [{ kind: "del" as const, text: before }] : []),
    ...(after ? [{ kind: "add" as const, text: after }] : []),
  ];
}

export type RuleRow = { rule: SrsItem; change: Change };

const ruleText = (rule: SrsItem) => JSON.stringify([rule.code, rule.title.trim(), rule.body.trim(), rule.fields.steps ?? null]);

/**
 * The business rules under the use case in this version, each marked added or changed against the approved one, and
 * the rules this version removed. Without a use case to compare with, nothing is marked.
 */
export function ruleRows(
  beforeItems: readonly SrsItem[],
  before: SrsItem | null,
  afterItems: readonly SrsItem[],
  after: SrsItem | null,
): { rows: RuleRow[]; removed: SrsItem[] } {
  const old = before ? rulesOf(beforeItems, before) : [];
  const now = after ? rulesOf(afterItems, after) : [];
  if (!before || !after) return { rows: (after ? now : old).map((rule) => ({ rule, change: "same" })), removed: [] };
  const byKey = new Map(old.map((rule) => [rule.key, rule]));
  const kept = new Set(now.map((rule) => rule.key));
  return {
    rows: now.map((rule) => {
      const was = byKey.get(rule.key);
      return { rule, change: !was ? "added" : ruleText(was) === ruleText(rule) ? "same" : "changed" };
    }),
    removed: old.filter((rule) => !kept.has(rule.key)),
  };
}

/** Whether this version changed the use case's Activity Flow. */
export function stepsChanged(before: SrsItem | null, after: SrsItem | null): boolean {
  if (!before || !after) return false;
  return JSON.stringify(stepsOf(before).rows) !== JSON.stringify(stepsOf(after).rows);
}

/** The key of an item and of every item under it: what a finding about the use case or one of its rules names. */
export function keysUnder(items: readonly SrsItem[], key: string): Set<string> {
  const keys = new Set([key]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const item of items) {
      if (item.parent !== null && keys.has(item.parent) && !keys.has(item.key)) {
        keys.add(item.key);
        grew = true;
      }
    }
  }
  return keys;
}

/**
 * The open findings as the review of a use case shows them: those to decide about it (or a rule under it), its other
 * ones (advice only), and those to decide elsewhere in the document, which hold its approval too since the hub gates
 * an approval on every open finding of level decide of the document.
 */
export type CaseFindings = { decide: SrsFinding[]; advice: SrsFinding[]; elsewhere: SrsFinding[] };

export function caseFindings(findings: readonly SrsFinding[], keys: ReadonlySet<string>): CaseFindings {
  const open = findings.filter((finding) => finding.status === "open").sort((a, b) => a.id - b.id);
  const about = (finding: SrsFinding) => finding.item !== null && keys.has(finding.item);
  return {
    decide: open.filter((finding) => about(finding) && isDeciding(finding)),
    advice: open
      .filter((finding) => about(finding) && !isDeciding(finding))
      .sort((a, b) => FINDING_LEVEL_ORDER[a.level] - FINDING_LEVEL_ORDER[b.level] || a.id - b.id),
    elsewhere: open.filter((finding) => !about(finding) && isDeciding(finding)),
  };
}

/**
 * Why Approve is not possible yet: @ba still checking the version, or points to settle (about this use case, and
 * elsewhere in the document); null when nothing the page knows holds it. Findings not read yet hold nothing here: the
 * hub's 409 still refuses the approval.
 */
export type ApproveGate = { kind: "ba" } | { kind: "findings"; here: number; elsewhere: number } | null;

export function approveGate(run: SrsReviewRun | null, findings: CaseFindings | null): ApproveGate {
  if (reviewHolds(run)) return { kind: "ba" };
  const here = findings?.decide.length ?? 0;
  const elsewhere = findings?.elsewhere.length ?? 0;
  return here + elsewhere > 0 ? { kind: "findings", here, elsewhere } : null;
}

/**
 * What the viewer may do on the use case: review it; wait while their account or the config loads; or why not: they
 * only read the project, they wrote a version since the one approved last, the version is no longer in review, or this
 * version does not change the use case, so it needs no review of its own.
 */
export type CaseAccessKind = "review" | "waiting" | "reader" | "author" | "closed" | "unchanged";

export function caseAccess({
  signedIn,
  block,
  configPending,
  toReview,
}: {
  signedIn: boolean;
  block: ReviewBlock | null;
  configPending: boolean;
  toReview: boolean;
}): CaseAccessKind {
  if (!signedIn) return "waiting";
  if (block === "reader") return "reader";
  if (block === "not_in_review") return "closed";
  // An author waits for the config before being told they cannot review: self_approval may let them.
  if (block === "author") return configPending ? "waiting" : "author";
  return toReview ? "review" : "unchanged";
}

/** A finding's choice as the reviewer sees it: "A", "B", up to the hub's four. */
export function optionKey(index: number): string {
  return "ABCD"[index] ?? String(index + 1);
}
