import type { RunEvent, RunEventKind, RunState } from "./queries";

/**
 * How the run page reads a run's events: each event becomes one line of the log, in seq order, with a short text,
 * a group the filters use, and a tone. Event bodies come from three runtimes through the worker's adapters, so every
 * shape is read defensively: the known keys of the Agent Client Protocol updates first (`content`, `title`,
 * `rawInput`, `entries`), then common ones (`text`, `name`, `output`), and the body's JSON when nothing matches.
 * Pure functions, shared by the page and its tests.
 */

export const LOG_GROUPS = ["agent", "tools", "output", "system"] as const;
export type LogGroup = (typeof LOG_GROUPS)[number];

/** The filter each kind falls under. The owner's messages sit with the agent's: they are one conversation. */
export const KIND_GROUP: Record<RunEventKind, LogGroup> = {
  agent_message_chunk: "agent",
  agent_thought_chunk: "agent",
  plan: "agent",
  user_message: "agent",
  tool_call: "tools",
  tool_call_update: "tools",
  output: "output",
  system: "system",
  state: "system",
  usage_update: "system",
};

/** A line longer than this is shortened in the browser; the hub keeps the event whole (up to 64 KiB of JSON). */
export const MAX_LINE_CHARS = 10_000;
/** Above this many lines shown, the log renders only the rows in view. */
export const VIRTUAL_THRESHOLD = 2_000;

export type LineTone = "ok" | "error" | null;

export type RunMove = { seq: number; at: string; from: RunState | null; to: RunState; actor: string | null; reason: string | null };

export type LogLine = {
  seq: number;
  at: string;
  kind: RunEventKind;
  group: LogGroup;
  text: string;
  tone: LineTone;
  /** The hub cut the event's body to 64 KiB of JSON. */
  truncated: boolean;
  /** The browser shortened the text to MAX_LINE_CHARS. */
  shortened: boolean;
  /** The text in lower case, for the search. */
  haystack: string;
};

/** Says a move between states in the page's language; the log keeps what it returns as the line's text. */
export type DescribeMove = (move: RunMove) => string;

type Body = Record<string, unknown>;

function isRecord(value: unknown): value is Body {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function compactJson(value: unknown, limit = 600): string {
  let text: string;
  try {
    text = JSON.stringify(value) ?? "";
  } catch {
    text = String(value);
  }
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

/**
 * The text of an ACP content value: a string, a content block (`{type: "text", text}`), a tool call content
 * (`{type: "content", content: {...}}`, `{type: "diff", path}`), or a list of them.
 */
export function contentText(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    const parts = value.map(contentText).filter((part): part is string => part !== null && part !== "");
    return parts.length ? parts.join("\n") : null;
  }
  if (!isRecord(value)) return null;
  if (typeof value.text === "string") return value.text;
  if (value.type === "diff" && typeof value.path === "string") return `diff ${value.path}`;
  if ("content" in value) return contentText(value.content);
  return null;
}

/** The words of a message-like body: `text`, then `content`, then `message`. */
function messageText(body: Body): string | null {
  return str(body.text) ?? contentText(body.content) ?? contentText(body.message) ?? null;
}

const INPUT_KEYS = ["command", "cmd", "file_path", "filePath", "path", "pattern", "query", "url", "description"] as const;

/** What a tool was asked to do, in one short phrase: its command, path or pattern when it has one. */
function inputSummary(input: unknown): string | null {
  if (typeof input === "string") return input.length > 300 ? `${input.slice(0, 300)}…` : input;
  if (!isRecord(input)) return null;
  for (const key of INPUT_KEYS) {
    const value = input[key];
    if (typeof value === "string" && value.trim()) return value;
    if (Array.isArray(value) && value.every((item) => typeof item === "string")) return value.join(" ");
  }
  return Object.keys(input).length ? compactJson(input, 300) : null;
}

const FAILED_STATUSES = new Set(["failed", "error", "errored", "cancelled", "canceled"]);

function toolStatus(body: Body): string | null {
  return str(body.status) ?? (isRecord(body.state) ? str(body.state.status) : null);
}

function toolLine(body: Body): { text: string; tone: LineTone } {
  const title =
    str(body.title) ?? str(body.name) ?? str(body.tool) ?? str(body.toolName) ?? str(body.kind) ?? null;
  const input = inputSummary(body.rawInput ?? body.input ?? body.arguments ?? body.args);
  const status = toolStatus(body);
  const head = [title, input && input !== title ? input : null].filter(Boolean).join(": ");
  const tone: LineTone = status && FAILED_STATUSES.has(status.toLowerCase()) ? "error" : null;
  return { text: head || compactJson(body), tone };
}

function toolUpdateLine(body: Body): { text: string; tone: LineTone } {
  const status = toolStatus(body);
  const output =
    contentText(body.content) ??
    str(body.rawOutput) ??
    str(body.output) ??
    str(body.result) ??
    (body.rawOutput !== undefined ? compactJson(body.rawOutput) : null) ??
    str(body.title);
  const tone: LineTone = status && FAILED_STATUSES.has(status.toLowerCase()) ? "error" : null;
  const text = [status ? `[${status}]` : null, output].filter(Boolean).join(" ");
  return { text: text || compactJson(body), tone };
}

function planLine(body: Body): string {
  const entries = Array.isArray(body.entries) ? body.entries : Array.isArray(body.items) ? body.items : null;
  if (!entries) return messageText(body) ?? compactJson(body);
  return entries
    .map((entry) => {
      if (!isRecord(entry)) return String(entry);
      const status = str(entry.status);
      const text = contentText(entry.content) ?? str(entry.title) ?? compactJson(entry);
      return status ? `[${status}] ${text}` : text;
    })
    .join("\n");
}

function usageLine(body: Body): string {
  const parts: string[] = [];
  const visit = (value: unknown, prefix: string) => {
    if (typeof value === "number" && Number.isFinite(value)) parts.push(`${prefix} ${value}`);
    else if (isRecord(value)) for (const [key, inner] of Object.entries(value)) visit(inner, prefix ? `${prefix}.${key}` : key);
  };
  visit(body, "");
  return parts.length ? parts.join(", ") : compactJson(body);
}

function systemLine(body: Body): { text: string; tone: LineTone } {
  const text = messageText(body) ?? compactJson(body);
  const output = str(body.output);
  const code = body.exit_code;
  const tone: LineTone = typeof code === "number" ? (code === 0 ? "ok" : "error") : null;
  return { text: output ? `${text}\n${output.replace(/\s+$/, "")}` : text, tone };
}

/** A `state` event as a move, or null for an event of another kind. */
export function moveOf(event: Pick<RunEvent, "seq" | "at" | "kind" | "body">): RunMove | null {
  if (event.kind !== "state") return null;
  const body = event.body;
  const to = str(body.to);
  if (!to) return null;
  return {
    seq: event.seq,
    at: event.at,
    from: (str(body.from) as RunState | null) ?? null,
    to: to as RunState,
    actor: str(body.actor),
    reason: str(body.reason),
  };
}

const FAILED_MOVES = new Set<string>(["failed", "lost"]);
const GOOD_MOVES = new Set<string>(["done", "review"]);

/** One event as one line of the log. */
export function toLogLine(event: RunEvent, describe: DescribeMove): LogLine {
  const body: Body = isRecord(event.body) ? event.body : {};
  let text: string;
  let tone: LineTone = null;
  switch (event.kind) {
    case "agent_message_chunk":
    case "agent_thought_chunk":
      text = messageText(body) ?? compactJson(body);
      break;
    case "user_message": {
      const words = messageText(body) ?? compactJson(body);
      const from = str(body.from);
      text = from ? `${from}: ${words}` : words;
      break;
    }
    case "tool_call":
      ({ text, tone } = toolLine(body));
      break;
    case "tool_call_update":
      ({ text, tone } = toolUpdateLine(body));
      break;
    case "plan":
      text = planLine(body);
      break;
    case "usage_update":
      text = usageLine(body);
      break;
    case "system":
      ({ text, tone } = systemLine(body));
      break;
    case "state": {
      const move = moveOf(event);
      text = move ? describe(move) : compactJson(body);
      tone = move && FAILED_MOVES.has(move.to) ? "error" : move && GOOD_MOVES.has(move.to) ? "ok" : null;
      break;
    }
    default:
      text = messageText(body) ?? str(body.line) ?? compactJson(body);
  }
  const shortened = text.length > MAX_LINE_CHARS;
  if (shortened) text = text.slice(0, MAX_LINE_CHARS);
  return {
    seq: event.seq,
    at: event.at,
    kind: event.kind,
    group: KIND_GROUP[event.kind] ?? "output",
    text,
    tone,
    truncated: event.truncated,
    shortened,
    haystack: text.toLowerCase(),
  };
}

/** The lines a filter and a search leave, in order. `query` matches anywhere in the text, in any case. */
export function filterLines(lines: readonly LogLine[], group: LogGroup | null, query: string): readonly LogLine[] {
  const needle = query.trim().toLowerCase();
  if (group === null && needle === "") return lines;
  return lines.filter((line) => (group === null || line.group === group) && (needle === "" || line.haystack.includes(needle)));
}

/** How many lines each filter holds. */
export function groupCounts(lines: readonly LogLine[]): Record<LogGroup, number> {
  const counts: Record<LogGroup, number> = { agent: 0, tools: 0, output: 0, system: 0 };
  for (const line of lines) counts[line.group] += 1;
  return counts;
}

/**
 * The parts of `text` around each match of `query` (any case), for highlighting: odd positions are matches. An
 * empty query gives the text whole.
 */
export function splitMatches(text: string, query: string): string[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [text];
  const lower = text.toLowerCase();
  if (lower.length !== text.length) return [text]; // a letter whose lower case is longer: positions would not match
  const parts: string[] = [];
  let from = 0;
  for (let at = lower.indexOf(needle); at !== -1; at = lower.indexOf(needle, from)) {
    parts.push(text.slice(from, at), text.slice(at, at + needle.length));
    from = at + needle.length;
  }
  parts.push(text.slice(from));
  return parts;
}
