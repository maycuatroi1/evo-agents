import { commandGist } from "./shell-command";
import type { ToolCall, ToolItem, TraceItem } from "./trace-model";

/**
 * The Trace's normal view: tool calls in a row that do the same kind of work are one row, named by what they did
 * ("Explored 9 files", "Ran tests 3 times, 1 failed") rather than by tool ("Bash"), as Codex and Claude Code fold theirs.
 * The runtime's bookkeeping events (`raw` items) neither show nor cut a group; the agent's words, a decision, a person's
 * message, a checklist, a move and a system line do cut it. A failed call stays in its group's count and shows under its
 * row, open; a search that finds nothing (exit 1 of grep or rg) is not a failure.
 */

export const INTENTS = ["explore", "edit", "test", "check", "git", "github", "hub", "run", "tool"] as const;
export type Intent = (typeof INTENTS)[number];

export type ToolGroup = {
  type: "group";
  /** The first call's key: a group keeps its key, and its open state, as calls join it. */
  key: string;
  seq: number;
  at: string;
  intent: Intent;
  calls: ToolItem[];
};

export type NormalItem = Exclude<TraceItem, { type: "tool" } | { type: "raw" }> | ToolGroup;

/** Wrappers before the program a command runs: `timeout 580`, `env A=1`, `uv run`, `time`, assignments. */
const PREFIX = /^(?:(?:timeout\s+\S+|env|time|nice|uv\s+run|poetry\s+run|[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|\S*))\s+)+/;

const EXPLORE = /^(?:cat|sed|head|tail|less|more|rg|grep|egrep|ls|find|fd|tree|wc|pwd|stat|file|du|jq|diff|nl|awk|git\s+(?:status|diff|log|show|branch|rev-parse|ls-files|blame|grep))\b/;
const TEST = /^(?:pytest|vitest|jest|playwright|python3?\s+-m\s+pytest|go\s+test|cargo\s+test|(?:pnpm|npm|yarn|bun)\s+(?:run\s+|exec\s+)?(?:test|e2e|vitest|playwright)\b)/;
const CHECK = /^(?:ruff|mypy|pyright|eslint|tsc|lint-imports|prettier|biome|black|isort|(?:pnpm|npm|yarn|bun)\s+(?:run\s+|exec\s+)?(?:lint|typecheck|check|format|eslint|tsc)\b)/;

/** The program a shell line runs once its setup and wrappers are off: `cd x && timeout 9 pytest -q` as `pytest -q`. */
export function programOf(command: string): string {
  return commandGist(command).replace(PREFIX, "").trim();
}

/** What kind of work a call does. */
export function intentOf(tool: Pick<ToolCall, "kind" | "arg" | "title">): Intent {
  switch (tool.kind) {
    case "read":
    case "search":
    case "fetch":
      return "explore";
    case "edit":
    case "delete":
    case "move":
      return "edit";
    case "execute": {
      const program = programOf(tool.arg ?? tool.title ?? "");
      if (EXPLORE.test(program)) return "explore";
      if (TEST.test(program)) return "test";
      if (CHECK.test(program)) return "check";
      if (/^git\b/.test(program)) return "git";
      if (/^gh\b/.test(program)) return "github";
      if (/^(?:evo-agents|evo-ak|evo)\b/.test(program)) return "hub";
      return "run";
    }
    default:
      return "tool";
  }
}

/** A call that failed: a failed status or a non-zero exit, except a search that found nothing. */
export function callFailed(tool: ToolCall): boolean {
  if (tool.exitCode === 1 && tool.kind === "execute" && /^(?:rg|grep|egrep)\b/.test(programOf(tool.arg ?? ""))) return false;
  return tool.status === "failed" || (tool.exitCode !== null && tool.exitCode !== 0);
}

export function callRunning(tool: ToolCall): boolean {
  return tool.status === "pending" || tool.status === "in_progress";
}

/** Group the trace's items for its normal view. */
export function groupByIntent(items: readonly TraceItem[]): NormalItem[] {
  const shown: NormalItem[] = [];
  let group: ToolGroup | null = null;
  for (const item of items) {
    if (item.type === "raw") continue;
    if (item.type === "tool") {
      const intent = intentOf(item.tool);
      if (group && group.intent === intent) {
        group.calls.push(item);
        continue;
      }
      group = { type: "group", key: `g${item.key}`, seq: item.seq, at: item.at, intent, calls: [item] };
      shown.push(group);
      continue;
    }
    group = null;
    shown.push(item);
  }
  return shown;
}

/** The last path segment of a path-like token, for "Explored queue.ts, runs.py". */
function baseName(path: string): string {
  const trimmed = path.replace(/["']/g, "").replace(/\/+$/, "");
  return trimmed.slice(trimmed.lastIndexOf("/") + 1) || trimmed;
}

const PATHISH = /^[\w.~/-]*[\w-]\.[A-Za-z0-9]{1,8}$|^[\w.~-]*\/[\w./-]+$/;

/** What a call worked on, when its input names it: a file's base name for reads and edits, a command's last path. */
export function objectOf(tool: ToolCall): string | null {
  const arg = tool.arg;
  if (!arg) return null;
  if (tool.kind === "read" || tool.kind === "edit" || tool.kind === "delete" || tool.kind === "move") return baseName(arg.split(",")[0].trim());
  if (tool.kind !== "execute") return null;
  const tokens = programOf(arg).split(/\s+/);
  for (let index = tokens.length - 1; index > 0; index--) {
    const token = tokens[index].replace(/[;|&)]+$/, "");
    if (PATHISH.test(token) && !token.startsWith("-")) return baseName(token);
  }
  return null;
}

/** The distinct objects of a group's calls, in the order first worked on. */
export function groupObjects(group: Pick<ToolGroup, "calls">): string[] {
  const seen = new Set<string>();
  for (const { tool } of group.calls) {
    const object = objectOf(tool);
    if (object) seen.add(object);
  }
  return [...seen];
}

/** The distinct programs of a group's calls ("ruff", "lint-imports"), for checks and git. */
export function groupPrograms(group: Pick<ToolGroup, "calls">, words = 1): string[] {
  const seen = new Set<string>();
  for (const { tool } of group.calls) {
    const program = programOf(tool.arg ?? "").split(/\s+/).slice(0, words).join(" ");
    if (program) seen.add(program);
  }
  return [...seen];
}

/** From the first call's start to the last call's end, or null while the last runs. */
export function groupSpan(group: Pick<ToolGroup, "calls">): number | null {
  const first = group.calls[0].tool;
  const last = group.calls[group.calls.length - 1].tool;
  if (last.endedAt === null) return null;
  return Math.max(0, Date.parse(last.endedAt) - Date.parse(first.startedAt));
}

/** Lines added and removed by a group's edits, when any says. */
export function groupDiff(group: Pick<ToolGroup, "calls">): { added: number; removed: number } | null {
  let added = 0;
  let removed = 0;
  let any = false;
  for (const { tool } of group.calls) {
    if (!tool.diff) continue;
    any = true;
    added += tool.diff.added;
    removed += tool.diff.removed;
  }
  return any ? { added, removed } : null;
}

/** All tool calls of the trace, for "121 tool calls". */
export function countCalls(items: readonly NormalItem[]): number {
  return items.reduce((sum, item) => sum + (item.type === "group" ? item.calls.length : 0), 0);
}
