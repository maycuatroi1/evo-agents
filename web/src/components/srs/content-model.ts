import type { SrsDiffItem, SrsItem } from "./api";
import { childrenOf, codeKey, isRow } from "./model";

/**
 * What an item of a version holds, as the SRS pages show it: a use case's fields and Activity Flow (read by the hub
 * from its body, evo_agents.core_plugins.srs.markdown.use_case_fields), the business rules and screens around it, a
 * section's text with its table rows, the codes inside a line of text, and a unified diff of an item as hunks.
 */

/** A use case's fields in the template's order, by the key the hub reads them into (evo_agents.core_plugins.srs.
 * USE_CASE_FIELDS): the classic ones, those of intent, context and expectation, or both. */
export const USE_CASE_FIELDS = [
  "id",
  "status",
  "objective",
  "intent",
  "actor",
  "context",
  "trigger",
  "pre_condition",
  "post_condition",
  "expectation",
] as const;
export type UseCaseField = (typeof USE_CASE_FIELDS)[number];

const HIDDEN_FIELDS = new Set(["steps", "step_label", "dash", "tail", "cells", "components", "group"]);

function text(value: unknown): string | null {
  if (typeof value === "string") return value.trim() || null;
  if (typeof value === "number") return String(value);
  return null;
}

/** The fields a use case's table of items holds: the template's first, in its order, then any other it names. */
export function factsOf(item: SrsItem): { name: string; known: boolean; value: string }[] {
  const facts: { name: string; known: boolean; value: string }[] = [];
  for (const name of USE_CASE_FIELDS) {
    const value = text(item.fields[name]);
    if (value) facts.push({ name, known: true, value });
  }
  for (const [name, raw] of Object.entries(item.fields)) {
    if ((USE_CASE_FIELDS as readonly string[]).includes(name) || HIDDEN_FIELDS.has(name)) continue;
    const value = text(raw);
    if (value) facts.push({ name, known: false, value });
  }
  return facts;
}

export type StepTable = { columns: string[]; rows: Record<string, string>[] };

/** A use case's Activity Flow: its columns (step, then the actors) and rows, as the hub read them. */
export function stepsOf(item: SrsItem): StepTable {
  const raw = Array.isArray(item.fields.steps) ? item.fields.steps : [];
  const rows = raw.filter((row): row is Record<string, string> => typeof row === "object" && row !== null);
  const columns: string[] = [];
  for (const row of rows) for (const name of Object.keys(row)) if (!columns.includes(name)) columns.push(name);
  return { columns, rows };
}

/** The steps of its use case a business rule says it applies at ("2", "2.1"). */
export function stepRefs(item: SrsItem): string[] {
  return Array.isArray(item.fields.steps) ? item.fields.steps.filter((step): step is string => typeof step === "string") : [];
}

/** The business rules under a use case, in the document's order. */
export function rulesOf(items: readonly SrsItem[], useCase: SrsItem): SrsItem[] {
  return childrenOf(items, useCase.key).filter((item) => item.kind === "business_rule");
}

/** The use case a business rule belongs to: the nearest one above it. */
export function parentUseCase(items: readonly SrsItem[], item: SrsItem): SrsItem | null {
  const byKey = new Map(items.map((one) => [one.key, one]));
  let parent = item.parent === null ? undefined : byKey.get(item.parent);
  while (parent) {
    if (parent.kind === "use_case") return parent;
    parent = parent.parent === null ? undefined : byKey.get(parent.parent);
  }
  return null;
}

function mentions(body: string, code: string | null): boolean {
  if (!code) return false;
  return codeKey(body).includes(codeKey(code));
}

/** The screens a use case uses: those it links (uses_screen), then those whose text names its code. */
export function screensOf(items: readonly SrsItem[], useCase: SrsItem): { screen: SrsItem; linked: boolean }[] {
  const linked = new Set(useCase.refs.filter((ref) => ref.rel === "uses_screen").map((ref) => ref.to));
  const screens = items.filter((item) => item.kind === "screen");
  return screens
    .filter((screen) => linked.has(screen.key) || mentions(screen.body, useCase.code))
    .map((screen) => ({ screen, linked: linked.has(screen.key) }));
}

/** The use cases that use a screen: those that link it, then those its text names. */
export function screenUsers(items: readonly SrsItem[], screen: SrsItem): SrsItem[] {
  return items.filter(
    (item) =>
      item.kind === "use_case" &&
      (item.refs.some((ref) => ref.rel === "uses_screen" && ref.to === screen.key) || mentions(screen.body, item.code)),
  );
}

/** A heading's text as the document reads it: its body, its table rows, then the text after the table. */
export function sectionMarkdown(items: readonly SrsItem[], item: SrsItem): string {
  const rows = childrenOf(items, item.key).filter(isRow);
  const parts = [item.body.trim()];
  if (rows.length) parts[0] = [parts[0], ...rows.map((row) => row.body.trim())].filter(Boolean).join("\n");
  const tail = text(item.fields.tail);
  if (tail) parts.push(tail);
  return parts.filter(Boolean).join("\n\n");
}

/**
 * The prose of a use case's body outside its tables (the items table and the Activity Flow, which the page draws from
 * the fields) and their one-line bold captions; empty when the body is only those.
 */
export function proseOutsideTables(body: string): string {
  const kept: string[] = [];
  let fence = false;
  for (const line of body.split("\n")) {
    if (/^\s*(```|~~~)/.test(line)) fence = !fence;
    if (!fence && (/^\s*\|/.test(line) || /^\s*\*\*[^*]+\*\*\s*$/.test(line))) continue;
    kept.push(line);
  }
  return kept.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

// Codes inside text

export type CodePart = string | { code: string; ref: string };

function escape(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * `line` cut into its text and the codes of `items` it names (a step's "Checks the cart (BR ORD1-1)"), each code or
 * alias with the code of its item, which its page goes by; the longest code wins where two overlap.
 */
export function splitCodes(line: string, items: readonly SrsItem[]): CodePart[] {
  const byCode = new Map<string, string>();
  for (const item of items) {
    for (const code of [item.code, ...item.aliases]) if (code && code.trim()) byCode.set(codeKey(code), item.code ?? item.key);
  }
  if (byCode.size === 0 || !line) return line ? [line] : [];
  const codes = [...byCode.keys()].sort((a, b) => b.length - a.length).map((code) => escape(code).replace(/ /g, "\\s+"));
  const pattern = new RegExp(`(?<![\\w-])(${codes.join("|")})(?![\\w-])`, "gi");
  const parts: CodePart[] = [];
  let at = 0;
  for (const match of line.matchAll(pattern)) {
    const start = match.index ?? 0;
    if (start > at) parts.push(line.slice(at, start));
    parts.push({ code: match[0], ref: byCode.get(codeKey(match[0])) ?? match[0] });
    at = start + match[0].length;
  }
  if (at < line.length) parts.push(line.slice(at));
  return parts;
}

// A unified diff of an item's text

export type DiffLine = { kind: "context" | "added" | "removed"; text: string; old: number | null; new: number | null };
export type DiffHunk = { oldStart: number; newStart: number; lines: DiffLine[] };

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/** The hunks of a diff the hub wrote without its file header (evo_agents.core_plugins.srs.diff.lines_between). */
export function parseHunks(lines: readonly string[]): DiffHunk[] {
  const hunks: DiffHunk[] = [];
  let hunk: DiffHunk | null = null;
  let oldLine = 0;
  let newLine = 0;
  for (const line of lines) {
    const head = HUNK.exec(line);
    if (head) {
      oldLine = Number(head[1]);
      newLine = Number(head[2]);
      hunk = { oldStart: oldLine, newStart: newLine, lines: [] };
      hunks.push(hunk);
      continue;
    }
    if (!hunk) {
      hunk = { oldStart: 1, newStart: 1, lines: [] };
      hunks.push(hunk);
      oldLine = 1;
      newLine = 1;
    }
    const sign = line[0];
    const body = line.slice(1);
    if (sign === "+") hunk.lines.push({ kind: "added", text: body, old: null, new: newLine++ });
    else if (sign === "-") hunk.lines.push({ kind: "removed", text: body, old: oldLine++, new: null });
    else hunk.lines.push({ kind: "context", text: sign === " " ? body : line, old: oldLine++, new: newLine++ });
  }
  return hunks;
}

export type ChangeCounts = Record<SrsDiffItem["change"], number>;

export function changeCounts(items: readonly SrsDiffItem[]): ChangeCounts {
  const counts: ChangeCounts = { changed: 0, added: 0, removed: 0, moved: 0 };
  for (const item of items) counts[item.change] += 1;
  return counts;
}
