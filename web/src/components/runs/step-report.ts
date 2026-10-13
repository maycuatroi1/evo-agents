/**
 * The report a step's agent ends with, read back from its message: the plan-step contract of the execute-plan skill,
 * `key: value` lines in a fixed order from `step:` and `result: pass | fail`, with `verify_output` and `decisions` as
 * YAML block scalars (`|` and the lines indented under it). Read as Markdown its line breaks fold into one long
 * paragraph, so the Trace shows it as a card (trace-report.tsx). The report may sit in a code fence, with words before
 * or after it, which stay Markdown. Pure functions, shared by the card and its tests.
 */

const KEYS = [
  "step",
  "result",
  "commits",
  "branch",
  "files",
  "outside_scope",
  "verify_command",
  "verify_output",
  "stash",
  "decisions",
  "notes",
] as const;
type Key = (typeof KEYS)[number];

export type ReportFile = { path: string; note: string | null };

export type StepReport = {
  step: string;
  passed: boolean;
  /** The commits' shas, oldest first. */
  commits: string[];
  /** What `commits` says when it names no sha, such as "none (the verify failed)". */
  commitsNote: string | null;
  branch: string | null;
  /** Each path with what follows it in brackets: "(new)". */
  files: ReportFile[];
  /** "path: reason" lines; empty for none. */
  outsideScope: string[];
  /** The plan's verify command and, when the agent ran another, the one it ran. */
  verify: { command: string; effective: string | null } | null;
  /** The verify's last lines, verbatim. */
  verifyOutput: string | null;
  stash: string | null;
  /** One entry per "Decision:", without that word; empty for none. */
  decisions: string[];
  notes: string | null;
};

/** A message that holds a report: the Markdown before it, the report, the Markdown after it. */
export type ReportMessage = { lead: string; report: StepReport; tail: string };

const START = /^([ \t]*)step:[ \t]*\S/;
const RESULT = /^[ \t]*result:[ \t]*(pass|fail)\b/i;
const FIELD = /^([a-z_]+):(?:[ \t]+(.*))?$/;
const BLOCK = /^[|>][-+]?$/;
const OPEN_FENCE = /^[ \t]*(`{3,}|~{3,})[^`]*$/;
const SHA = /^[0-9a-f]{7,40}$/i;
const NONE = /^none\b/i;

const isKey = (word: string): word is Key => (KEYS as readonly string[]).includes(word);

/** Lines without their common leading whitespace. */
function dedent(lines: string[]): string[] {
  const indents = lines.filter((line) => line.trim()).map((line) => /^[ \t]*/.exec(line)?.[0].length ?? 0);
  const cut = indents.length ? Math.min(...indents) : 0;
  return lines.map((line) => line.slice(cut).trimEnd());
}

/** Lines without the blank ones at either end. */
function trimBlank(lines: string[]): string[] {
  let start = 0;
  let end = lines.length;
  while (start < end && !lines[start].trim()) start += 1;
  while (end > start && !lines[end - 1].trim()) end -= 1;
  return lines.slice(start, end);
}

function listOf(text: string | null, split: RegExp): string[] {
  if (!text || NONE.test(text)) return [];
  return text
    .split(split)
    .map((part) => part.trim().replace(/^[-*][ \t]+/, ""))
    .filter(Boolean);
}

function decisionsOf(text: string | null): string[] {
  if (!text) return [];
  const items: string[] = [];
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const decision = /^(?:[-*][ \t]+)?Decision:[ \t]*(.*)$/i.exec(trimmed);
    if (decision || items.length === 0) items.push(decision ? decision[1] : trimmed);
    else items[items.length - 1] = `${items[items.length - 1]} ${trimmed}`;
  }
  return items.filter((item) => item && !/^none\.?$/i.test(item));
}

function verifyOf(text: string | null): StepReport["verify"] {
  if (!text) return null;
  const at = text.search(/;[ \t]*effective:/i);
  if (at < 0) return { command: text, effective: null };
  const effective = text.slice(at).replace(/^;[ \t]*effective:[ \t]*/i, "").trim();
  return { command: text.slice(0, at).trim(), effective: effective || null };
}

/** The report in `text`, or null when it holds none: a line `step: <id>` then a line `result: pass` or `result: fail`. */
export function parseStepReport(text: string): ReportMessage | null {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const start = lines.findIndex((line, index) => START.test(line) && RESULT.test(lines[index + 1] ?? ""));
  if (start < 0) return null;
  const indent = START.exec(lines[start])?.[1].length ?? 0;

  const fence = start > 0 ? OPEN_FENCE.exec(lines[start - 1]) : null;
  let end = lines.length;
  if (fence) {
    const close = new RegExp(`^[ \\t]*${fence[1][0] === "`" ? "`" : "~"}{${fence[1].length},}[ \\t]*$`);
    const found = lines.findIndex((line, index) => index > start && close.test(line));
    if (found >= 0) end = found;
  }

  const fields = new Map<Key, { first: string | null; rest: string[] }>();
  let current: { first: string | null; rest: string[] } | null = null;
  for (const raw of lines.slice(start, end)) {
    const line = raw.slice(Math.min(indent, /^[ \t]*/.exec(raw)?.[0].length ?? 0));
    const match = FIELD.exec(line);
    if (match && isKey(match[1]) && !fields.has(match[1])) {
      const value = match[2]?.trim() ?? "";
      current = { first: value && !BLOCK.test(value) ? value : null, rest: [] };
      fields.set(match[1], current);
    } else {
      current?.rest.push(line);
    }
  }
  const lineList = (key: Key): string[] | null => {
    const field = fields.get(key);
    if (!field) return null;
    const all = trimBlank([...(field.first === null ? [] : [field.first]), ...dedent(trimBlank(field.rest))]);
    return all.length ? all : null;
  };
  const value = (key: Key): string | null => lineList(key)?.join("\n").trim() ?? null;

  const result = value("result");
  const step = value("step");
  if (!step || !result) return null;

  const commitsText = value("commits");
  const commits = commitsText && !NONE.test(commitsText)
    ? commitsText.split(/[\s,]+/).map((word) => word.replace(/[()[\]]/g, "")).filter((word) => SHA.test(word))
    : [];
  const notes = value("notes");

  const report: StepReport = {
    step,
    passed: /^pass\b/i.test(result),
    commits,
    commitsNote: commits.length === 0 ? commitsText : null,
    branch: value("branch"),
    files: listOf(value("files"), /\n|,[ \t]+/).map((file) => {
      const noted = /^(.+?)[ \t]+\(([^()]+)\)$/.exec(file);
      return noted ? { path: noted[1], note: noted[2] } : { path: file, note: null };
    }),
    outsideScope: listOf(value("outside_scope"), /\n/),
    verify: verifyOf(value("verify_command")),
    verifyOutput: lineList("verify_output")?.join("\n") ?? null,
    stash: value("stash"),
    decisions: decisionsOf(value("decisions")),
    notes: notes && !/^none\.?$/i.test(notes) ? notes : null,
  };
  return {
    lead: lines.slice(0, fence ? start - 1 : start).join("\n").trim(),
    report,
    tail: end < lines.length ? lines.slice(end + 1).join("\n").trim() : "",
  };
}

/**
 * A shell command as lines a person can read and still paste: a new line after each `&&`, `||` and `;` outside quotes,
 * the lines after the first indented. A short command stays on one line.
 */
export function shellLines(command: string, width = 80): string {
  if (command.length <= width || command.includes("\n")) return command;
  const parts: string[] = [];
  let quote: string | null = null;
  let from = 0;
  for (let index = 0; index < command.length; index += 1) {
    const char = command[index];
    if (quote) {
      if (char === "\\" && quote === '"') index += 1;
      else if (char === quote) quote = null;
      continue;
    }
    if (char === "\\") {
      index += 1;
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
      continue;
    }
    const pair = command.slice(index, index + 2);
    const operator = pair === "&&" || pair === "||" ? pair : char === ";" ? ";" : null;
    if (!operator) continue;
    parts.push(`${command.slice(from, index).trim()}${operator === ";" ? ";" : ` ${operator}`}`);
    index += operator.length - 1;
    from = index + 1;
  }
  const last = command.slice(from).trim();
  if (last) parts.push(last);
  return parts.map((part, index) => (index === 0 ? part : `  ${part}`)).join("\n");
}
