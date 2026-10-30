/**
 * A tracked-changes Markdown of two versions of a document: the new version, with the words it added and the words it
 * removed marked in place (remark-tracked.ts), for SafeMarkdown's `tracked` mode to render as one document.
 *
 * - Lines come from the line diff (line-diff.ts). In a changed block the n-th removed line pairs with the n-th added
 *   one: when both open with the same block syntax (heading, list item, quote, indent) the rest is diffed by words
 *   (word-diff.ts) and marked word by word; otherwise, or when they share too little, the old line is marked removed
 *   and the new one added, each whole. A line left without a pair is marked whole, its block syntax kept outside the
 *   mark so a removed heading still reads as a heading.
 * - A mark never goes inside Markdown syntax: a pair whose changed words hold emphasis, code, link or table syntax, or
 *   fall inside a code span, a link's target, an autolink or an entity, is marked whole instead.
 * - A fenced code block or a table with a change inside is marked as a block: its old version whole as removed, then
 *   its new version whole as added. A table whose header and delimiter rows stay is marked row by row instead.
 * - A link reference definition stays out of the marks: a removed one is left out, an added one kept as it is.
 *
 * Both versions have any mark character, and any character reference to one (&#x2E04;, &#11780;), replaced by U+FFFD
 * first, so a document cannot forge a mark. Every step is linear or bounded in the size of the documents, which are
 * untrusted input: no regular expression here can backtrack over a long line.
 */

import { type DiffLine, diffLines } from "./line-diff";
import { ADD_CLOSE, ADD_OPEN, ANY_MARK, BLOCK_ADD, BLOCK_DEL, DEL_CLOSE, DEL_OPEN, type TrackedKind } from "./remark-tracked";
import { type Budget, diffWords, MIN_SHARED, newBudget, sharedShare, type WordPart } from "./word-diff";

const FENCE_OPEN = /^(\s*)(`{3,}|~{3,})/;
const FENCE_CLOSE = /^\s*(`{3,}|~{3,})\s*$/;
// Each run of white space has one place to go, so a long line that fails to match fails in linear time.
const DELIMITER = /^\s*(?:\|\s*)?:?-+:?\s*(?:\|\s*:?-+:?\s*)*(?:\|\s*)?$/;
const PREFIX = /^[ \t]*(?:>[ \t]?)*[ \t]*(?:(?:[-+*]|\d{1,9}[.)])[ \t]+(?:\[[ xX]\][ \t]+)?|#{1,6}(?:[ \t]+|$))?/;
const DEFINITION = /^\s{0,3}\[[^\]]+\]:/;
/** A numeric character reference CommonMark decodes to a mark (U+2E00 to U+2E05); no named one decodes to a mark. */
const MARK_REFERENCE = /&#(?:[xX]0*2[eE]0[0-5]|0*1177[6-9]|0*1178[01]);/g;
const SYNTAX = /[*_~`[\]\\|<>]/;
const PROTECTED = [/\]\([^)]*\)?/g, /\]\[[^\]]*\]?/g, /<[^>\s]*>?/g, /(?:https?:\/\/|www\.)\S+/g, /&#?\w+;/g];

type Region = { kind: "fence" | "table"; start: number; end: number };

/** The fenced code blocks and tables of a version, as [start, end) line ranges, and the region of each line. */
function regions(lines: readonly string[]): { list: Region[]; at: Int32Array } {
  const list: Region[] = [];
  const at = new Int32Array(lines.length).fill(-1);
  const add = (region: Region) => {
    for (let i = region.start; i < region.end; i++) at[i] = list.length;
    list.push(region);
  };
  let i = 0;
  while (i < lines.length) {
    const open = FENCE_OPEN.exec(lines[i]);
    if (open) {
      let j = i + 1;
      while (j < lines.length) {
        const close = FENCE_CLOSE.exec(lines[j]);
        j++;
        if (close && close[1][0] === open[2][0] && close[1].length >= open[2].length) break;
      }
      add({ kind: "fence", start: i, end: j });
      i = j;
      continue;
    }
    if (lines[i].includes("|") && i + 1 < lines.length && lines[i + 1].includes("|") && DELIMITER.test(lines[i + 1])) {
      let j = i + 2;
      while (j < lines.length && lines[j].trim() !== "" && lines[j].includes("|") && !FENCE_OPEN.test(lines[j])) j++;
      add({ kind: "table", start: i, end: j });
      i = j;
      continue;
    }
    i++;
  }
  return { list, at };
}

function wrap(text: string, kind: TrackedKind): string {
  const core = text.trim();
  if (!core) return kind === "add" ? text : "";
  const lead = text.slice(0, text.length - text.trimStart().length);
  const trail = text.slice(text.trimEnd().length);
  return kind === "add" ? `${lead}${ADD_OPEN}${core}${ADD_CLOSE}${trail}` : `${lead}${DEL_OPEN}${core}${DEL_CLOSE}${trail}`;
}

function splitPrefix(line: string): [string, string] {
  const prefix = PREFIX.exec(line)?.[0] ?? "";
  return [prefix, line.slice(prefix.length)];
}

/** A line marked whole, its block syntax outside the mark; null for a removed definition, which is left out. */
function wholeLine(line: string, kind: TrackedKind): string | null {
  if (DEFINITION.test(line)) return kind === "add" ? line : null;
  const [prefix, content] = splitPrefix(line);
  return content.trim() ? prefix + wrap(content, kind) : kind === "add" ? line : null;
}

/** A table row marked whole: each cell's text inside the mark, the pipes outside. */
function wholeRow(line: string, kind: TrackedKind): string {
  let out = "";
  let cell = "";
  for (let i = 0; i < line.length; i++) {
    const char = line[i];
    if (char === "\\" && i + 1 < line.length) {
      cell += char + line[++i];
      continue;
    }
    if (char === "|") {
      out += wrap(cell, kind) || cell;
      out += "|";
      cell = "";
      continue;
    }
    cell += char;
  }
  return out + (wrap(cell, kind) || cell);
}

/**
 * The code spans of a line, as CommonMark finds them: a run of backticks opens one that the next run of the same length
 * closes; a run no later run closes is text. One pass over the runs, so a line of backticks stays linear.
 */
function codeSpans(text: string, found: [number, number][]): void {
  const runs: [number, number][] = [];
  for (let i = 0; i < text.length; ) {
    if (text[i] !== "`") {
      i++;
      continue;
    }
    const start = i;
    while (i < text.length && text[i] === "`") i++;
    runs.push([start, i - start]);
  }
  // For each run, the index of the next run of the same length.
  const next = new Int32Array(runs.length).fill(-1);
  const lastOf = new Map<number, number>();
  for (let r = runs.length - 1; r >= 0; r--) {
    next[r] = lastOf.get(runs[r][1]) ?? -1;
    lastOf.set(runs[r][1], r);
  }
  for (let r = 0; r < runs.length; r++) {
    const close = next[r];
    if (close < 0) continue;
    found.push([runs[r][0], runs[close][0] + runs[close][1]]);
    r = close;
  }
}

function ranges(text: string): [number, number][] {
  const found: [number, number][] = [];
  codeSpans(text, found);
  for (const pattern of PROTECTED) for (const match of text.matchAll(pattern)) found.push([match.index, match.index + match[0].length]);
  return found;
}

/** Whether a mark among the parts would land inside Markdown syntax. */
function touchesSyntax(before: string, after: string, parts: readonly WordPart[]): boolean {
  const old = ranges(before);
  const now = ranges(after);
  const overlaps = (list: [number, number][], from: number, to: number) => list.some(([start, end]) => from < end && to > start);
  let i = 0;
  let j = 0;
  for (const part of parts) {
    const size = part.text.length;
    if (part.kind !== "same" && SYNTAX.test(part.text)) return true;
    if (part.kind === "del" && overlaps(old, i, i + size)) return true;
    if (part.kind === "add" && overlaps(now, j, j + size)) return true;
    if (part.kind !== "add") i += size;
    if (part.kind !== "del") j += size;
  }
  return false;
}

/** A removed line and the added line it pairs with, as the lines that show both. */
function pairLines(before: string, after: string, budget: Budget): string[] {
  if (DEFINITION.test(before) || DEFINITION.test(after)) return [after];
  const [oldPrefix, oldContent] = splitPrefix(before);
  const [newPrefix, newContent] = splitPrefix(after);
  if (oldPrefix !== newPrefix) return [wholeLine(before, "del"), wholeLine(after, "add")].filter((line) => line !== null);
  const parts = diffWords(oldContent, newContent, budget);
  if (!parts || sharedShare(parts) < MIN_SHARED || touchesSyntax(oldContent, newContent, parts)) {
    const both = [wrap(oldContent, "del"), wrap(newContent, "add")].filter((text) => text.trim()).map((text) => text.trim());
    return [newPrefix + both.join(" ")];
  }
  return [newPrefix + parts.map((part) => (part.kind === "same" ? part.text : wrap(part.text, part.kind))).join("")];
}

export function markedMarkdown(before: string, after: string, budget: Budget = newBudget()): string {
  const oldText = before.replace(ANY_MARK, "\uFFFD").replace(MARK_REFERENCE, "\uFFFD");
  const newText = after.replace(ANY_MARK, "\uFFFD").replace(MARK_REFERENCE, "\uFFFD");
  const lines = diffLines(oldText, newText);
  const oldLines: string[] = [];
  const newLines: string[] = [];
  for (const line of lines) {
    if (line.oldNo !== null) oldLines.push(line.text);
    if (line.newNo !== null) newLines.push(line.text);
  }
  const oldRegions = regions(oldLines);
  const newRegions = regions(newLines);

  // Regions of both versions in one numbering, joined in groups through the lines they keep.
  const shift = oldRegions.list.length;
  // Union-find by size with path halving, iterative: thousands of chained regions neither recurse nor go quadratic.
  const parent = Int32Array.from({ length: shift + newRegions.list.length }, (_, index) => index);
  const size = new Int32Array(parent.length).fill(1);
  const find = (x: number): number => {
    while (parent[x] !== x) {
      parent[x] = parent[parent[x]];
      x = parent[x];
    }
    return x;
  };
  const union = (x: number, y: number) => {
    let a = find(x);
    let b = find(y);
    if (a === b) return;
    if (size[a] < size[b]) [a, b] = [b, a];
    parent[b] = a;
    size[a] += size[b];
  };
  const touched = new Set<number>();
  const oldOf = (line: DiffLine) => (line.oldNo === null ? -1 : oldRegions.at[line.oldNo - 1]);
  const newOf = (line: DiffLine) => (line.newNo === null ? -1 : newRegions.at[line.newNo - 1]);
  for (const line of lines) {
    const a = oldOf(line);
    const b = newOf(line);
    if (line.kind === "same" && a >= 0 && b >= 0) union(a, b + shift);
  }
  for (const line of lines) {
    const a = oldOf(line);
    const b = newOf(line);
    if (a >= 0 && (line.kind === "del" || b < 0)) touched.add(a);
    if (b >= 0 && (line.kind === "add" || a < 0)) touched.add(b + shift);
  }
  const touchedGroups = new Set([...touched].map(find));

  // A group of one table on each side whose header and delimiter rows stay is marked row by row.
  const sameAt = new Map<number, number>();
  for (const line of lines) if (line.kind === "same") sameAt.set((line.oldNo ?? 0) - 1, (line.newNo ?? 0) - 1);
  const members = new Map<number, number[]>();
  for (let id = 0; id < parent.length; id++) {
    const root = find(id);
    const list = members.get(root);
    if (list) list.push(id);
    else members.set(root, [id]);
  }
  const byRows = (group: number) => {
    const ids = members.get(group) ?? [];
    if (ids.length !== 2 || ids[0] >= shift || ids[1] < shift) return false;
    const old = oldRegions.list[ids[0]];
    const now = newRegions.list[ids[1] - shift];
    if (old.kind !== "table" || now.kind !== "table") return false;
    return sameAt.get(old.start) === now.start && sameAt.get(old.start + 1) === now.start + 1;
  };
  // Each group's mode once, so a group of thousands of regions is not walked again for each of its lines.
  const modes = new Map<number, "rows" | "blocks" | null>();
  const mode = (region: number) => {
    if (region < 0) return null;
    const group = find(region);
    let found = modes.get(group);
    if (found === undefined) {
      found = touchedGroups.has(group) ? (byRows(group) ? "rows" : "blocks") : null;
      modes.set(group, found);
    }
    return found;
  };

  const out: string[] = [];
  const blank = () => {
    if (out.length && out[out.length - 1].trim() !== "") out.push("");
  };
  const emitted = new Set<number>();
  const emitBlocks = (group: number) => {
    if (emitted.has(group)) return;
    emitted.add(group);
    const ids = members.get(group) ?? [];
    for (const [kind, list, source, offset] of [
      ["del", oldRegions.list, oldLines, 0],
      ["add", newRegions.list, newLines, shift],
    ] as const) {
      for (const id of ids.filter((id) => (offset === 0 ? id < shift : id >= shift))) {
        const region = list[id - offset];
        const indent = /^\s*/.exec(source[region.start])?.[0] ?? "";
        blank();
        out.push(indent + (kind === "add" ? BLOCK_ADD : BLOCK_DEL), "");
        out.push(...source.slice(region.start, region.end), "");
      }
    }
  };

  // Changed lines outside any marked region, waiting for the end of their block to be paired.
  let dels: string[] = [];
  let adds: string[] = [];
  const flush = () => {
    let i = 0;
    let j = 0;
    while (i < dels.length || j < adds.length) {
      if (i < dels.length && !dels[i].trim()) {
        blank();
        i++;
      } else if (j < adds.length && !adds[j].trim()) {
        blank();
        j++;
      } else if (i < dels.length && j < adds.length) out.push(...pairLines(dels[i++], adds[j++], budget));
      else if (i < dels.length) {
        const line = wholeLine(dels[i++], "del");
        if (line !== null) out.push(line);
      } else {
        out.push(wholeLine(adds[j], "add") ?? adds[j]);
        j++;
      }
    }
    dels = [];
    adds = [];
  };

  for (const line of lines) {
    const a = oldOf(line);
    const b = newOf(line);
    const oldMode = mode(a);
    const newMode = mode(b < 0 ? -1 : b + shift);
    if (oldMode === "blocks" || newMode === "blocks") {
      flush();
      if (oldMode === "blocks") emitBlocks(find(a));
      if (newMode === "blocks") emitBlocks(find(b + shift));
      // A kept line outside the new version's marked blocks still shows.
      if (line.kind === "same" && newMode !== "blocks") out.push(line.text);
      else if (line.kind === "add" && newMode !== "blocks") adds.push(line.text);
      continue;
    }
    if (oldMode === "rows" || newMode === "rows") {
      flush();
      out.push(line.kind === "same" ? line.text : wholeRow(line.text, line.kind));
      continue;
    }
    if (line.kind === "same") {
      flush();
      // A blank line outside any region only parts blocks: one is enough beside the blank lines marks bring.
      if (line.text.trim() === "") blank();
      else out.push(line.text);
    } else (line.kind === "del" ? dels : adds).push(line.text);
  }
  flush();
  return out.join("\n");
}
