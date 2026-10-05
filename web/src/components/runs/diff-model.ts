/**
 * A run's diff as the worker uploads it (`git diff --binary <base> HEAD`, at most 8 MiB), read into files, hunks and
 * lines for the diff page. Binary patches are noted, never shown. Paths that git quotes (spaces, non-ASCII, control
 * characters) are unquoted. Past `maxLines` lines the reading stops and says so. Pure functions, shared by the page
 * and its tests.
 */

export type DiffLineKind = "context" | "added" | "removed" | "note";

export type DiffLine = { kind: DiffLineKind; text: string; old: number | null; new: number | null };

export type DiffHunk = {
  oldStart: number;
  oldLines: number;
  newStart: number;
  newLines: number;
  /** The text git prints after the second @@: the function or heading the hunk sits in. */
  section: string;
  lines: DiffLine[];
};

export type FileStatus = "added" | "deleted" | "renamed" | "copied" | "modified";

export type DiffFile = {
  path: string;
  /** The path before a rename or copy; null otherwise. */
  from: string | null;
  status: FileStatus;
  binary: boolean;
  /** Only the mode changed (or nothing git shows as text). */
  modeOnly: boolean;
  additions: number;
  deletions: number;
  hunks: DiffHunk[];
};

export type ParsedDiff = {
  files: DiffFile[];
  additions: number;
  deletions: number;
  /** Lines read before `maxLines` stopped the reading; the rest of the diff is not in `files`. */
  cut: boolean;
};

/** Lines the page renders at most; a longer diff is downloaded instead. */
export const MAX_DIFF_LINES = 20_000;

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$/;

/** A path as git prints it: `"a/caf\303\251 file"` with C escapes and octal UTF-8 bytes, or plain. */
export function unquotePath(text: string): string {
  if (!(text.length >= 2 && text.startsWith('"') && text.endsWith('"'))) return text;
  const body = text.slice(1, -1);
  const bytes: number[] = [];
  const simple: Record<string, number> = { n: 10, t: 9, r: 13, b: 8, f: 12, v: 11, a: 7, '"': 34, "\\": 92 };
  for (let index = 0; index < body.length; index += 1) {
    const char = body[index];
    if (char !== "\\") {
      bytes.push(...new TextEncoder().encode(char));
      continue;
    }
    const next = body[index + 1] ?? "";
    if (/[0-7]/.test(next)) {
      const octal = body.slice(index + 1, index + 4).match(/^[0-7]{1,3}/)?.[0] ?? next;
      bytes.push(Number.parseInt(octal, 8) & 0xff);
      index += octal.length;
    } else if (next in simple) {
      bytes.push(simple[next]);
      index += 1;
    } else {
      bytes.push(92);
    }
  }
  return new TextDecoder().decode(new Uint8Array(bytes));
}

/** `a/x` or `b/x` without its prefix; `/dev/null` as null. */
function sidePath(raw: string): string | null {
  const text = unquotePath(raw.replace(/\t.*$/, "").trim());
  if (text === "/dev/null") return null;
  return text.replace(/^[ab]\//, "");
}

/** The two paths of `diff --git a/x b/y`, when they can be told apart (git quotes paths with spaces). */
function gitHeaderPaths(rest: string): { a: string | null; b: string | null } {
  const quoted = rest.match(/^("(?:[^"\\]|\\.)*"|\S+) ("(?:[^"\\]|\\.)*"|\S+)$/);
  if (quoted) return { a: sidePath(quoted[1]), b: sidePath(quoted[2]) };
  // Unquoted paths with spaces: "a/p q b/p q", split in the middle when both halves name the same path.
  const half = (rest.length - 1) / 2;
  if (Number.isInteger(half) && rest[half] === " ") {
    const a = rest.slice(0, half);
    const b = rest.slice(half + 1);
    if (a.slice(2) === b.slice(2)) return { a: sidePath(a), b: sidePath(b) };
  }
  return { a: null, b: null };
}

function newFile(header: string): DiffFile {
  const { a, b } = gitHeaderPaths(header);
  return { path: b ?? a ?? header, from: null, status: "modified", binary: false, modeOnly: false, additions: 0, deletions: 0, hunks: [] };
}

export function parseDiff(text: string, maxLines = MAX_DIFF_LINES): ParsedDiff {
  const files: DiffFile[] = [];
  let file: DiffFile | null = null;
  let hunk: DiffHunk | null = null;
  let oldLine = 0;
  let newLine = 0;
  let inBinary = false;
  let lines = 0;
  let cut = false;
  let additions = 0;
  let deletions = 0;
  const rows = text.split("\n");
  if (rows.length && rows[rows.length - 1] === "") rows.pop();

  for (const row of rows) {
    if (row.startsWith("diff --git ")) {
      file = newFile(row.slice("diff --git ".length));
      files.push(file);
      hunk = null;
      inBinary = false;
      continue;
    }
    if (!file) continue; // text before the first file (a commit message, say)
    if (hunk) {
      const sign = row[0];
      if (sign === " " || sign === "+" || sign === "-" || row === "") {
        if (lines >= maxLines) {
          cut = true;
          break;
        }
        lines += 1;
        const body = row.slice(1);
        if (sign === "+") {
          hunk.lines.push({ kind: "added", text: body, old: null, new: newLine });
          newLine += 1;
          file.additions += 1;
          additions += 1;
        } else if (sign === "-") {
          hunk.lines.push({ kind: "removed", text: body, old: oldLine, new: null });
          oldLine += 1;
          file.deletions += 1;
          deletions += 1;
        } else {
          hunk.lines.push({ kind: "context", text: body, old: oldLine, new: newLine });
          oldLine += 1;
          newLine += 1;
        }
        continue;
      }
      if (sign === "\\") {
        hunk.lines.push({ kind: "note", text: row.slice(2), old: null, new: null });
        continue;
      }
    }
    const match = HUNK.exec(row);
    if (match) {
      hunk = {
        oldStart: Number(match[1]),
        oldLines: match[2] === undefined ? 1 : Number(match[2]),
        newStart: Number(match[3]),
        newLines: match[4] === undefined ? 1 : Number(match[4]),
        section: match[5] ?? "",
        lines: [],
      };
      oldLine = hunk.oldStart;
      newLine = hunk.newStart;
      file.hunks.push(hunk);
      continue;
    }
    if (inBinary) continue;
    if (row.startsWith("new file mode")) file.status = "added";
    else if (row.startsWith("deleted file mode")) file.status = "deleted";
    else if (row.startsWith("rename from ")) {
      file.status = "renamed";
      file.from = unquotePath(row.slice("rename from ".length));
    } else if (row.startsWith("rename to ")) file.path = unquotePath(row.slice("rename to ".length));
    else if (row.startsWith("copy from ")) {
      file.status = "copied";
      file.from = unquotePath(row.slice("copy from ".length));
    } else if (row.startsWith("copy to ")) file.path = unquotePath(row.slice("copy to ".length));
    else if (row.startsWith("--- ")) {
      const path = sidePath(row.slice(4));
      if (path === null) file.status = "added";
    } else if (row.startsWith("+++ ")) {
      const path = sidePath(row.slice(4));
      if (path === null) file.status = "deleted";
      else file.path = path;
    } else if (row === "GIT binary patch" || row.startsWith("Binary files ")) {
      file.binary = true;
      inBinary = row === "GIT binary patch";
    }
  }
  for (const each of files) each.modeOnly = !each.binary && each.hunks.length === 0 && each.status === "modified";
  return { files, additions, deletions, cut };
}

/** An anchor id for a file of the diff, stable for its position. */
export function fileAnchor(index: number): string {
  return `file-${index + 1}`;
}
