/**
 * A word diff on top of the line diff (line-diff.ts): a changed line is cut into words, runs of white space and single
 * other characters, and the two lines' words are matched by the same shortest edit script. A word is a run of letters,
 * combining marks and digits (Unicode classes), so a Vietnamese word keeps its accents whole whether its text is
 * precomposed or decomposed. Within each changed run the removed words come before the added ones, and a space kept
 * between two changed runs joins them, so "a b c" to "x y z" reads as one replacement rather than three.
 *
 * Every search is bounded: a pair of lines whose search would keep over MAX_CELLS diagonal entries, or that comes after
 * a document's pairs have spent WORD_BUDGET entries, has no word diff and is marked whole.
 */

import { type DiffKind, type DiffLine, editScript, MAX_CELLS, toIds } from "./line-diff";

export type WordPart = { kind: DiffKind; text: string };

/** The entries all the pairs of one document may spend, so a document of 2 MiB rewritten line by line stays fast. */
export const WORD_BUDGET = 4 * MAX_CELLS;

/** Below this share of unchanged characters, two lines read better as one replaced by the other than word by word. */
export const MIN_SHARED = 0.3;

const TOKEN = /[\p{L}\p{M}\p{N}]+|\s+|[^\p{L}\p{M}\p{N}\s]\p{M}*/gu;

/** A budget of diagonal entries shared by the searches of one document. */
export type Budget = { left: number };

export function newBudget(cells: number = WORD_BUDGET): Budget {
  return { left: cells };
}

export function tokenize(text: string): string[] {
  return text.match(TOKEN) ?? [];
}

const blank = (text: string) => text.trim() === "";

/**
 * The parts of `before` and `after`: "same" text in both, "del" text only in `before`, "add" text only in `after`.
 * Joining the same and del parts gives `before`, the same and add parts `after`. Null when the search is over budget.
 */
export function diffWords(before: string, after: string, budget: Budget = newBudget()): WordPart[] | null {
  const a = tokenize(before);
  const b = tokenize(after);
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head++;
  let tail = 0;
  while (tail < a.length - head && tail < b.length - head && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail++;

  const [ai, bi] = toIds(a.slice(head, a.length - tail), b.slice(head, b.length - tail));
  const middle = editScript(ai, bi, Math.min(MAX_CELLS, budget.left), budget);
  if (middle === null) return null;
  const script: DiffKind[] = [...new Array<DiffKind>(head).fill("same"), ...middle, ...new Array<DiffKind>(tail).fill("same")];

  // Runs of the script: an unchanged run of words, or a changed run with its removed and added text.
  type Run = { same: string } | { del: string; add: string };
  const runs: Run[] = [];
  let i = 0;
  let j = 0;
  for (let k = 0; k < script.length; ) {
    if (script[k] === "same") {
      let text = "";
      while (k < script.length && script[k] === "same") {
        text += a[i++];
        j++;
        k++;
      }
      runs.push({ same: text });
      continue;
    }
    let del = "";
    let add = "";
    while (k < script.length && script[k] !== "same") {
      if (script[k] === "del") del += a[i++];
      else add += b[j++];
      k++;
    }
    runs.push({ del, add });
  }

  // A run of white space alone between two changed runs joins them.
  const joined: Run[] = [];
  for (let k = 0; k < runs.length; k++) {
    const run = runs[k];
    const prev = joined[joined.length - 1];
    const next = runs[k + 1];
    if ("same" in run && blank(run.same) && prev && !("same" in prev) && next && !("same" in next)) {
      prev.del += run.same + next.del;
      prev.add += run.same + next.add;
      k++;
      continue;
    }
    if (!("same" in run) && prev && !("same" in prev)) {
      prev.del += run.del;
      prev.add += run.add;
      continue;
    }
    joined.push("same" in run ? run : { ...run });
  }

  const parts: WordPart[] = [];
  for (const run of joined) {
    if ("same" in run) parts.push({ kind: "same", text: run.same });
    else {
      if (run.del) parts.push({ kind: "del", text: run.del });
      if (run.add) parts.push({ kind: "add", text: run.add });
    }
  }
  return parts;
}

/** The share of the two texts' characters the parts keep unchanged, 1 for two empty texts. */
export function sharedShare(parts: readonly WordPart[]): number {
  let same = 0;
  let total = 0;
  for (const part of parts) {
    if (part.kind === "same") {
      same += 2 * part.text.length;
      total += 2 * part.text.length;
    } else total += part.text.length;
  }
  return total === 0 ? 1 : same / total;
}

/**
 * The words of each line of a line diff that differ from the line it is paired with, for a row to emphasise them.
 * In each changed block the n-th removed line pairs with the n-th added one; a line with no pair, a pair over budget
 * or one that shares less than MIN_SHARED of its text has null (the row is marked whole). A removed line's parts are
 * same and del, an added line's same and add.
 */
export function lineWords(lines: readonly DiffLine[], budget: Budget = newBudget()): (WordPart[] | null)[] {
  const out = new Array<WordPart[] | null>(lines.length).fill(null);
  let k = 0;
  while (k < lines.length) {
    if (lines[k].kind === "same") {
      k++;
      continue;
    }
    const dels: number[] = [];
    const adds: number[] = [];
    while (k < lines.length && lines[k].kind !== "same") {
      (lines[k].kind === "del" ? dels : adds).push(k);
      k++;
    }
    for (let p = 0; p < Math.min(dels.length, adds.length); p++) {
      const parts = diffWords(lines[dels[p]].text, lines[adds[p]].text, budget);
      if (!parts || sharedShare(parts) < MIN_SHARED) continue;
      out[dels[p]] = parts.filter((part) => part.kind !== "add");
      out[adds[p]] = parts.filter((part) => part.kind !== "del");
    }
  }
  return out;
}
