import { contentText } from "./log-model";
import { commandInput, unwrapShell } from "./shell-command";
import { diffStat, editText } from "./trace-diff";
import { TOOL_KINDS, type ToolKind, type ToolStatus, type ToolCall, type PlanEntry, type RawEvent, type TraceItem, type TraceEvent } from "./trace-types";

/** An event shown raw is cut to this many characters of JSON. */
const RAW_CHARS = 20_000;

export type Body = Record<string, unknown>;

export function isRecord(value: unknown): value is Body {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

export function int(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

export function json(value: unknown, space?: number): string {
  try {
    return JSON.stringify(value, null, space) ?? "";
  } catch {
    return String(value);
  }
}

function time(at: string): number {
  const value = Date.parse(at);
  return Number.isNaN(value) ? 0 : value;
}

/** Milliseconds from `from` to `to`, never below 0: the hub and the worker each time their own events. */
export function between(from: string, to: string): number {
  return Math.max(0, time(to) - time(from));
}

/** The words of a message-like body: `content` (ACP), then `text`, then `message`. */
export function messageText(body: Body): string | null {
  return contentText(body.content) ?? str(body.text) ?? contentText(body.message) ?? null;
}

// Tool calls.

const KIND_ALIASES: Record<string, ToolKind> = { write: "edit", patch: "edit", bash: "execute", shell: "execute", grep: "search", glob: "search" };

function toolKind(value: unknown): ToolKind {
  const kind = typeof value === "string" ? value.toLowerCase() : "";
  if ((TOOL_KINDS as readonly string[]).includes(kind)) return kind as ToolKind;
  return KIND_ALIASES[kind] ?? "other";
}

const FAILED = new Set(["failed", "error", "errored", "cancelled", "canceled", "rejected"]);
const DONE = new Set(["completed", "complete", "done", "success", "succeeded"]);

function toolStatus(value: unknown): ToolStatus | null {
  const status = typeof value === "string" ? value.toLowerCase().replace(/-/g, "_") : null;
  if (!status) return null;
  if (FAILED.has(status)) return "failed";
  if (DONE.has(status)) return "completed";
  if (status === "in_progress" || status === "inprogress" || status === "running") return "in_progress";
  return "pending";
}

const ARG_KEYS = ["command", "cmd", "file_path", "filePath", "notebook_path", "pattern", "query", "url", "path", "skill", "description", "prompt"] as const;

/** What a tool was asked to do, in one line: the first of its command, path, pattern, query or url. */
export function toolArgument(input: unknown): string | null {
  if (typeof input === "string") return str(input) ? oneLine(input) : null;
  if (!isRecord(input)) return null;
  for (const key of ARG_KEYS) {
    const value = input[key];
    if (typeof value === "string" && value.trim()) return oneLine(key === "command" || key === "cmd" ? unwrapShell(value) : value);
    if (Array.isArray(value) && value.length && value.every((item) => typeof item === "string")) return oneLine(value.join(" "));
  }
  if (Array.isArray(input.changes)) {
    const paths = input.changes.flatMap((change) => (isRecord(change) && typeof change.path === "string" ? [change.path] : []));
    if (paths.length) return paths.join(", ");
  }
  if (Array.isArray(input.todos)) return null; // a checklist, which the plan event that comes with it shows
  for (const value of Object.values(input)) if (typeof value === "string" && value.trim()) return oneLine(value);
  return null;
}

function oneLine(text: string): string {
  const line = text.trim().split("\n")[0];
  return text.trim().includes("\n") ? `${line} …` : line;
}

/** The exit code of a command, as Codex (`exitCode`), opencode (`exit`) or Claude Code's text ("Exit code 1") give it. */
export function exitCode(rawOutput: unknown, text: string | null): number | null {
  if (isRecord(rawOutput)) {
    const code = int(rawOutput.exitCode) ?? int(rawOutput.exit) ?? int(rawOutput.exit_code);
    if (code !== null) return code;
  }
  const match = text ? /^Exit code (\d+)\b/.exec(text) : null;
  return match ? Number(match[1]) : null;
}

const RAW_OUTPUT_META = new Set(["exitCode", "exit", "exit_code", "durationMs", "duration_ms"]);

/** A tool's output: the words of its content (ACP), else its rawOutput as text or JSON (without the exit code). */
function toolOutput(body: Body): string | null {
  const content = Array.isArray(body.content) ? body.content.filter((item) => !(isRecord(item) && item.type === "diff")) : body.content;
  const text = contentText(content);
  if (text !== null && text !== "") return text;
  const raw = body.rawOutput;
  if (typeof raw === "string") return str(raw);
  if (isRecord(raw)) {
    const rest = Object.fromEntries(Object.entries(raw).filter(([key]) => !RAW_OUTPUT_META.has(key)));
    if (Object.keys(rest).length === 0) return null;
    if (Object.keys(rest).length === 1 && typeof Object.values(rest)[0] === "string") return Object.values(rest)[0] as string;
    return json(rest, 2);
  }
  if (Array.isArray(raw) && raw.length) return json(raw, 2);
  return str(body.output) ?? str(body.result);
}

function inputJson(input: unknown, arg: string | null): string | null {
  if (!isRecord(input)) return null;
  const keys = Object.keys(input).filter((key) => key !== "description");
  if (keys.length === 0) return null;
  // One key that the row already shows says nothing more.
  if (keys.length === 1 && typeof input[keys[0]] === "string" && arg !== null) return null;
  return json(input, 2);
}

export function newTool(event: TraceEvent, id: string | null): ToolCall {
  return {
    id,
    title: null,
    kind: "other",
    arg: null,
    status: "pending",
    startedAt: event.at,
    endedAt: null,
    ms: null,
    exitCode: null,
    description: null,
    output: null,
    diff: null,
    change: null,
    input: null,
  };
}

/** A `tool_call` (or a later one of the same id) into the row. */
export function applyCall(tool: ToolCall, body: Body, at: string, first: boolean): void {
  tool.title = str(body.title) ?? str(body.name) ?? str(body.tool) ?? tool.title;
  if (body.kind !== undefined) tool.kind = toolKind(body.kind);
  else if (tool.kind === "other" && tool.title) tool.kind = toolKind(tool.title);
  const input = body.rawInput ?? body.input ?? body.arguments;
  if (input !== undefined) {
    tool.arg = toolArgument(input) ?? tool.arg;
    const description = isRecord(input) ? str(input.description) : null;
    tool.description = description && description !== tool.arg ? oneLine(description) : tool.description;
    if (tool.kind === "edit") {
      tool.diff = diffStat(input, body.content) ?? tool.diff;
      tool.change = editText(input, body.content) ?? tool.change;
    } else if (tool.kind === "execute") {
      tool.input = commandInput(input) ?? tool.input;
    } else {
      tool.input = inputJson(input, tool.arg);
    }
  }
  const status = toolStatus(body.status);
  // A call that comes after its update leaves the update's final status alone.
  if (status && (first || tool.endedAt === null)) tool.status = status;
  if (first || time(at) < time(tool.startedAt)) tool.startedAt = at;
  if (tool.endedAt !== null) tool.ms = between(tool.startedAt, tool.endedAt);
}

/** A `tool_call_update` into the row: the latest status, title and output win, as ACP replaces them. */
export function applyUpdate(tool: ToolCall, body: Body, at: string): void {
  const status = toolStatus(body.status);
  if (status) tool.status = status;
  tool.title = str(body.title) ?? tool.title;
  if (body.kind !== undefined) tool.kind = toolKind(body.kind);
  const output = toolOutput(body);
  if (output !== null) tool.output = output;
  const code = exitCode(body.rawOutput, output);
  if (code !== null) tool.exitCode = code;
  if (tool.kind === "edit") tool.diff = diffStat(undefined, body.content) ?? tool.diff;
  if (status === "completed" || status === "failed") {
    tool.endedAt = at;
    tool.ms = between(tool.startedAt, at);
  }
}

// Other kinds.

export function planEntries(body: Body): PlanEntry[] {
  const list = Array.isArray(body.entries) ? body.entries : Array.isArray(body.items) ? body.items : [];
  return list.flatMap((entry): PlanEntry[] => {
    if (typeof entry === "string") return [{ content: entry, status: "pending", priority: null }];
    if (!isRecord(entry)) return [];
    const content = contentText(entry.content) ?? str(entry.title) ?? str(entry.step);
    if (!content) return [];
    const status = toolStatus(entry.status);
    return [{ content, status: status === "failed" ? "completed" : (status ?? "pending"), priority: str(entry.priority) }];
  });
}

/** "decision #3 asked (scope): Which way?" as the hub writes it, else the text whole. */
export function askedQuestion(text: string): string {
  const match = /^decision #\d+ asked(?: \([^)]*\))?: ([\s\S]+)$/.exec(text);
  return match ? match[1] : text;
}

function rawLabel(body: Body): string {
  const found = isRecord(body.raw) ? body.raw : body;
  const parts = ["type", "subtype", "method"].map((key) => str(found[key])).filter((part): part is string => part !== null);
  return parts.length ? parts.join(" ") : "event";
}

export function rawEvent(event: TraceEvent, kind: string): RawEvent {
  const text = json(event.body, 2);
  return {
    seq: event.seq,
    at: event.at,
    label: kind === "output" ? rawLabel(event.body) : kind,
    json: text.length > RAW_CHARS ? `${text.slice(0, RAW_CHARS)}\n…` : text,
  };
}

export const SHOWN_KINDS = new Set(["agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update", "plan", "user_message", "state", "system", "usage_update", "output"]);

export function latestOpen(items: readonly TraceItem[]): ToolCall | undefined {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.type === "tool" && (item.tool.status === "pending" || item.tool.status === "in_progress")) return item.tool;
  }
  return undefined;
}
