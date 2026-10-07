import { contentText, moveOf, type RunMove } from "./log-model";
import type { RunEvent } from "./queries";

/**
 * How the Trace tab of a run's page reads the run's events (the same stream as the Raw log): one item per thing the
 * agent did, in the order it did them.
 *
 * - The agent's words: consecutive `agent_message_chunk` events are one message. A turn ends at anything else the
 *   trace shows, and at a `usage_update` (a runtime reports usage once a turn or a step ends). The adapters of this
 *   repository send whole blocks of text, so chunks are joined as paragraphs.
 * - Its thinking: `agent_thought_chunk` events fold into the message that follows them ("Thought for 9s"), timed from
 *   the event before the first of them to the last of them, or stand alone when a tool call follows.
 * - Tool calls: a `tool_call` and its `tool_call_update` events, matched by `toolCallId`, are one row with its title,
 *   kind, main argument, status, duration (from the times of the events), exit code when the runtime gives one, and
 *   output (the update's `content`, else its `rawOutput`). An update that comes before its call makes the row, and the
 *   call fills it in when it comes.
 * - A `plan` is a checklist, a `user_message` the owner's words (or answer to a decision), a `state` a move between
 *   states, a `system` event a line of the worker's, or "Asked you" when it reports a decision the agent asked.
 * - `output` events, a runtime's events no adapter maps, fold together in their raw form; so does an event of a kind
 *   this page does not know.
 *
 * Events are read in seq order whatever order they come in, and each seq once. Bodies come from three runtimes, so
 * every key is read defensively. Pure functions, shared by the page and its tests.
 */

/** Lines of a tool's output shown before "Show the full output". */
export const OUTPUT_LINES = 20;
/** Above this many items the trace renders only the ones in view. */
export const TRACE_VIRTUAL_THRESHOLD = 500;
/** An event shown raw is cut to this many characters of JSON. */
const RAW_CHARS = 20_000;

export const TOOL_KINDS = ["read", "edit", "delete", "move", "search", "execute", "think", "fetch", "switch_mode", "other"] as const;
export type ToolKind = (typeof TOOL_KINDS)[number];
export type ToolStatus = "pending" | "in_progress" | "completed" | "failed";
export type Tone = "ok" | "error" | null;

export type DiffStat = { added: number; removed: number };

export type ToolCall = {
  /** The runtime's toolCallId; null when the event had none. */
  id: string | null;
  /** The runtime's title for it ("Bash", "bash", the command itself for Codex), null until the call is known. */
  title: string | null;
  kind: ToolKind;
  /** What it was asked to do, in one line: its command, path, pattern or query. */
  arg: string | null;
  status: ToolStatus;
  startedAt: string;
  /** When its last update with a final status came. */
  endedAt: string | null;
  /** From the times of its events; null while it runs. */
  ms: number | null;
  exitCode: number | null;
  /** What the agent said the call is for, when it said (Claude Code's `description`). */
  description: string | null;
  /** What it printed or returned, whole; the page cuts it at OUTPUT_LINES. */
  output: string | null;
  /** An edit's lines added and removed, when its input or output says. */
  diff: DiffStat | null;
  /** An edit's change as diff lines (`- old`, `+ new`), from its input. */
  change: string | null;
  /** The input whole, as JSON, for a read, search or other call whose input says more than `arg`. */
  input: string | null;
};

export type Thought = { text: string; ms: number };
export type PlanEntry = { content: string; status: "pending" | "in_progress" | "completed"; priority: string | null };
export type RawEvent = { seq: number; at: string; label: string; json: string };

type Base = { key: string; seq: number; at: string };
export type AgentItem = Base & { type: "agent"; text: string; thought: Thought | null; endAt: string };
export type ToolItem = Base & { type: "tool"; tool: ToolCall };
export type TraceItem =
  | AgentItem
  | ToolItem
  | (Base & { type: "plan"; entries: PlanEntry[] })
  | (Base & { type: "user"; text: string; from: string | null; decisionId: number | null })
  | (Base & { type: "move"; move: RunMove })
  | (Base & { type: "system"; text: string; tone: Tone; output: string | null })
  | (Base & { type: "ask"; decisionId: number; question: string; category: string | null; step: string | null })
  | (Base & { type: "raw"; events: RawEvent[] });

export type TraceEvent = Pick<RunEvent, "seq" | "at" | "kind" | "body">;

type Body = Record<string, unknown>;

function isRecord(value: unknown): value is Body {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function int(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function json(value: unknown, space?: number): string {
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
function messageText(body: Body): string | null {
  return contentText(body.content) ?? str(body.text) ?? contentText(body.message) ?? null;
}

function lineCount(text: string): number {
  if (!text) return 0;
  return text.replace(/\n$/, "").split("\n").length;
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

/** `/bin/zsh -lc 'ls -a'` as `ls -a`: Codex names a command by the shell line that ran it. */
export function unwrapShell(command: string): string {
  const match = /^\s*(?:\/(?:usr\/)?bin\/)?(?:ba|z)?sh\s+-l?c\s+(['"])([\s\S]*)\1\s*$/.exec(command);
  return match ? match[2] : command;
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

/** The lines an edit adds and removes, from its input (Claude Code, opencode, Codex) or its ACP diff content. */
export function diffStat(input: unknown, content: unknown): DiffStat | null {
  let added = 0;
  let removed = 0;
  let found = false;
  const pair = (before: unknown, after: unknown) => {
    if (typeof before !== "string" && typeof after !== "string") return;
    found = true;
    removed += typeof before === "string" ? lineCount(before) : 0;
    added += typeof after === "string" ? lineCount(after) : 0;
  };
  const unified = (diff: unknown) => {
    if (typeof diff !== "string") return;
    found = true;
    for (const line of diff.split("\n")) {
      if (line.startsWith("+") && !line.startsWith("+++")) added += 1;
      else if (line.startsWith("-") && !line.startsWith("---")) removed += 1;
    }
  };
  if (isRecord(input)) {
    if ("old_string" in input || "new_string" in input) pair(input.old_string, input.new_string);
    else if ("oldString" in input || "newString" in input) pair(input.oldString, input.newString);
    else if (typeof input.content === "string") pair(undefined, input.content);
    if (Array.isArray(input.edits)) for (const edit of input.edits) if (isRecord(edit)) pair(edit.old_string ?? edit.oldString, edit.new_string ?? edit.newString);
    if (Array.isArray(input.changes)) for (const change of input.changes) if (isRecord(change)) unified(change.diff);
  }
  if (!found && Array.isArray(content)) {
    for (const item of content) if (isRecord(item) && item.type === "diff") pair(item.oldText ?? undefined, item.newText);
  }
  return found ? { added, removed } : null;
}

/** An edit's change as lines of a diff (`- old`, `+ new`), for an edit whose output says nothing of it. */
function editText(input: unknown): string | null {
  if (!isRecord(input)) return null;
  const before = input.old_string ?? input.oldString;
  const after = input.new_string ?? input.newString;
  if (typeof before !== "string" && typeof after !== "string") return null;
  const lines = (text: unknown, mark: string) => (typeof text === "string" && text ? text.replace(/\n$/, "").split("\n").map((line) => `${mark} ${line}`) : []);
  return [...lines(before, "-"), ...lines(after, "+")].join("\n");
}

/** The exit code of a command, as Codex (`exitCode`), opencode (`exit`) or Claude Code's text ("Exit code 1") give it. */
function exitCode(rawOutput: unknown, text: string | null): number | null {
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

function newTool(event: TraceEvent, id: string | null): ToolCall {
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
function applyCall(tool: ToolCall, body: Body, at: string, first: boolean): void {
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
      tool.change = editText(input) ?? tool.change;
    } else if (tool.kind !== "execute") {
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
function applyUpdate(tool: ToolCall, body: Body, at: string): void {
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

function planEntries(body: Body): PlanEntry[] {
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
function askedQuestion(text: string): string {
  const match = /^decision #\d+ asked(?: \([^)]*\))?: ([\s\S]+)$/.exec(text);
  return match ? match[1] : text;
}

function rawLabel(body: Body): string {
  const found = isRecord(body.raw) ? body.raw : body;
  const parts = ["type", "subtype", "method"].map((key) => str(found[key])).filter((part): part is string => part !== null);
  return parts.length ? parts.join(" ") : "event";
}

function rawEvent(event: TraceEvent, kind: string): RawEvent {
  const text = json(event.body, 2);
  return {
    seq: event.seq,
    at: event.at,
    label: kind === "output" ? rawLabel(event.body) : kind,
    json: text.length > RAW_CHARS ? `${text.slice(0, RAW_CHARS)}\n…` : text,
  };
}

const SHOWN_KINDS = new Set(["agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update", "plan", "user_message", "state", "system", "usage_update", "output"]);

function latestOpen(items: readonly TraceItem[]): ToolCall | undefined {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item.type === "tool" && (item.tool.status === "pending" || item.tool.status === "in_progress")) return item.tool;
  }
  return undefined;
}

/**
 * The run's events as the Trace shows them. `events` may come in any order and repeat a seq; the trace reads each seq
 * once, in order.
 */
export function buildTrace(events: readonly TraceEvent[]): TraceItem[] {
  const ordered = [...events].sort((a, b) => a.seq - b.seq);
  const items: TraceItem[] = [];
  const tools = new Map<string, ToolCall>();
  let agent: AgentItem | null = null;
  let raw: (Base & { type: "raw"; events: RawEvent[] }) | null = null;
  let previousAt: string | null = null;
  let lastSeq = -Infinity;

  const close = () => {
    agent = null;
    raw = null;
  };

  for (const event of ordered) {
    if (event.seq === lastSeq) continue;
    lastSeq = event.seq;
    const body: Body = isRecord(event.body) ? event.body : {};
    const kind = SHOWN_KINDS.has(event.kind) ? event.kind : "unknown";
    const base = { seq: event.seq, at: event.at };

    switch (kind) {
      case "agent_thought_chunk": {
        raw = null;
        const text = messageText(body) ?? "";
        const start = previousAt ?? event.at;
        const open = agent as AgentItem | null;
        if (open && open.text === "" && open.thought) {
          open.thought.text = [open.thought.text, text].filter(Boolean).join("\n\n");
          open.thought.ms += between(open.endAt, event.at);
          open.endAt = event.at;
        } else {
          const item: AgentItem = { ...base, key: `a${event.seq}`, type: "agent", text: "", thought: { text, ms: between(start, event.at) }, endAt: event.at };
          items.push(item);
          agent = item;
        }
        break;
      }
      case "agent_message_chunk": {
        raw = null;
        const text = messageText(body) ?? "";
        const open = agent as AgentItem | null;
        if (open) {
          open.text = open.text && text ? `${open.text}\n\n${text}` : open.text || text;
          open.endAt = event.at;
        } else {
          const item: AgentItem = { ...base, key: `a${event.seq}`, type: "agent", text, thought: null, endAt: event.at };
          items.push(item);
          agent = item;
        }
        break;
      }
      case "tool_call": {
        close();
        const id = str(body.toolCallId) ?? str(body.id);
        const known = id !== null ? tools.get(id) : undefined;
        if (known) {
          applyCall(known, body, event.at, false);
        } else {
          const tool = newTool(event, id);
          applyCall(tool, body, event.at, true);
          if (id !== null) tools.set(id, tool);
          items.push({ ...base, key: `t${event.seq}`, type: "tool", tool });
        }
        break;
      }
      case "tool_call_update": {
        // An update changes a row already shown (or makes one): the agent's message around it goes on. One without an
        // id, which ACP does not allow but a hand-made event may be, goes to the latest call still without a result.
        const id = str(body.toolCallId) ?? str(body.id);
        let tool = id !== null ? tools.get(id) : latestOpen(items);
        if (!tool) {
          raw = null;
          tool = newTool(event, id);
          if (id !== null) tools.set(id, tool);
          items.push({ ...base, key: `t${event.seq}`, type: "tool", tool });
        }
        applyUpdate(tool, body, event.at);
        break;
      }
      case "plan":
        close();
        items.push({ ...base, key: `p${event.seq}`, type: "plan", entries: planEntries(body) });
        break;
      case "user_message":
        close();
        items.push({
          ...base,
          key: `u${event.seq}`,
          type: "user",
          text: messageText(body) ?? json(body),
          from: str(body.from),
          decisionId: int(body.decision_id),
        });
        break;
      case "state": {
        close();
        const move = moveOf(event);
        if (move) items.push({ ...base, key: `s${event.seq}`, type: "move", move });
        else items.push({ ...base, key: `s${event.seq}`, type: "system", text: json(body), tone: null, output: null });
        break;
      }
      case "system": {
        close();
        const text = messageText(body) ?? json(body);
        const decision = isRecord(body.decision) ? body.decision : null;
        const decisionId = decision ? int(decision.id) : null;
        if (decision && decisionId !== null) {
          items.push({
            ...base,
            key: `q${event.seq}`,
            type: "ask",
            decisionId,
            question: askedQuestion(text),
            category: str(decision.category),
            step: str(decision.step) ?? (int(decision.step) !== null ? String(decision.step) : null),
          });
        } else {
          const code = int(body.exit_code);
          const output = str(body.output);
          items.push({
            ...base,
            key: `y${event.seq}`,
            type: "system",
            text,
            tone: code === null ? null : code === 0 ? "ok" : "error",
            output: output ? output.replace(/\s+$/, "") : null,
          });
        }
        break;
      }
      case "usage_update":
        // A turn (or a step) ended; its figures go to the usage card.
        close();
        break;
      default: {
        agent = null;
        const open = raw as (Base & { type: "raw"; events: RawEvent[] }) | null;
        if (open) open.events.push(rawEvent(event, kind === "unknown" ? event.kind : kind));
        else {
          const item = { ...base, key: `r${event.seq}`, type: "raw" as const, events: [rawEvent(event, kind === "unknown" ? event.kind : kind)] };
          items.push(item);
          raw = item;
        }
      }
    }
    previousAt = event.at;
  }
  return items;
}

/** The first OUTPUT_LINES lines of `text`, and how many lines it has in all. */
export function cutOutput(text: string, lines = OUTPUT_LINES): { shown: string; total: number; cut: boolean } {
  const all = text.replace(/\n$/, "").split("\n");
  return { shown: all.slice(0, lines).join("\n"), total: all.length, cut: all.length > lines };
}

/** Whether a tool row starts open: a failed call is open, so what went wrong shows without a click. */
export function openByDefault(tool: ToolCall): boolean {
  return tool.status === "failed" || (tool.exitCode !== null && tool.exitCode !== 0);
}

/** A duration as its parts, for "48.2s", "8m 12s", "1h 5m". */
export function durationParts(ms: number): { hours: number; minutes: number; seconds: number; tenths: number } {
  const tenths = Math.floor(ms / 100);
  const seconds = Math.floor(ms / 1000);
  return { hours: Math.floor(seconds / 3600), minutes: Math.floor(seconds / 60) % 60, seconds: seconds % 60, tenths: tenths % 10 };
}
