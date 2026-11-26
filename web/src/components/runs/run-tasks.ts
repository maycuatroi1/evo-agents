import { contentText } from "./log-model";
import type { RunEvent } from "./queries";
import { commandGist, unwrapShell } from "./shell-command";

/**
 * What a run has going besides its main thread: the subagents it started and the commands it left running in the
 * background, read from the task events Claude Code sends and the worker logs as `output` events (`{raw: {type:
 * "system", subtype, ...}}`, evo_agents/worker/runtimes/claude_code.py):
 *
 * - `task_started`: a task begins, or begins again (a subagent that ended its turn and was woken by a background
 *   command it started): `task_id`, `task_type` (`local_agent` for a subagent, `local_bash` for a command),
 *   `description`, `subagent_type`, `tool_use_id` (the call that started it), `parent_task_id` (the subagent a command
 *   belongs to) and `is_backgrounded`.
 * - `task_progress`: a subagent's running total (`usage.tool_uses`, `usage.total_tokens`) and what it does now
 *   (`description`, "Running " and its last call's description; `last_tool_name`).
 * - `task_updated`: a patch, `is_backgrounded` once a command is sent to the background, `status` once it ends.
 * - `task_notification`: the end, with `status` (completed, failed, stopped) and the task's `summary`: a background
 *   subagent's whole report, a command's description.
 *
 * Beside them, the tool calls that started the tasks: an `Agent` call (`Task` before Claude Code renamed it) holds the
 * subagent's brief (`rawInput.prompt`, which `task_started` may repeat as `prompt`) and, for a subagent in the
 * foreground, its report as the call's result; a `Bash` call holds the command a task runs. An `Agent` call no task
 * event names (a runtime that sends none) is a subagent of its own, its id the call's.
 *
 * Each event of a subagent names the `Agent` call that started it in its body's `_meta.parentToolCallId` (the worker
 * since step 1 of agent-centric-run-page, evo_agents/worker/runtimes/common.py `with_parent`); the main agent's name
 * none, and so do all the events of older runs. `eventsOfAgent` splits a run's events that way.
 *
 * Events are read in seq order, each seq once. Other runtimes send none of these, so their runs have no tasks. Pure
 * functions, shared by the page and its tests.
 */

export type TaskKind = "agent" | "command" | "other";
export type TaskStatus = "running" | "completed" | "failed" | "stopped";

export type RunTask = {
  id: string;
  kind: TaskKind;
  /** The runtime's own type ("local_agent", "local_bash", ...). */
  type: string;
  /** What the agent called it when it started it. */
  name: string;
  /** A subagent's type ("plan-step", "Explore"). */
  agentType: string | null;
  /** The subagent this task belongs to, when a subagent started it. */
  parent: string | null;
  background: boolean;
  /** The tool call that started it, the row the trace shows for it. */
  callId: string | null;
  status: TaskStatus;
  /** When it first started, and when it last ended (null while it runs). */
  startedAt: string;
  endedAt: string | null;
  /** A subagent's last call, as its last progress said it ("Wait for verify run"). */
  activity: string | null;
  lastTool: string | null;
  toolUses: number | null;
  tokens: number | null;
  /** The first line of what it reported when it ended. */
  summary: string | null;
  /** A subagent's brief, whole, as the main agent wrote it. */
  prompt: string | null;
  /** A subagent's report, whole: its `task_notification`'s summary in the background, its call's result otherwise. */
  result: string | null;
  /** A command's line as the trace names it (`commandName`, rule G1), from the call that started it. */
  command: string | null;
  /** The seq of its first and its last event. */
  firstSeq: number;
  seq: number;
};

/** The key of an event's `_meta` that names the `Agent` call whose subagent the event is of. */
export const PARENT_TOOL_CALL = "parentToolCallId";

/** The `Agent` call whose subagent `event` is of, or null for an event of the main agent (and every older event). */
export function parentOf(event: Pick<RunEvent, "body">): string | null {
  const body = event.body as Raw | null;
  const meta = body && typeof body === "object" ? (body as Raw)._meta : null;
  if (!meta || typeof meta !== "object" || Array.isArray(meta)) return null;
  return text((meta as Raw)[PARENT_TOOL_CALL]);
}

/**
 * The events of one agent of the run: the main agent's (`call` null), which are all the events of a run older than
 * subagent ids, or those of the subagent that the `Agent` call `call` started.
 */
export function eventsOfAgent<T extends Pick<RunEvent, "body">>(events: readonly T[], call: string | null): T[] {
  return events.filter((event) => parentOf(event) === call);
}

/** Whether any event of the run names a subagent: a run older than subagent ids has none. */
export function namesAgents(events: readonly Pick<RunEvent, "body">[]): boolean {
  return events.some((event) => parentOf(event) !== null);
}

/** Claude Code's tool that starts a subagent, `Task` before it was renamed. */
export const AGENT_TOOLS: ReadonlySet<string> = new Set(["Agent", "Task"]);

/** Assignments to environment variables at the head of a command: `CI=1 FOO="a b" pytest` runs `pytest`. */
const ASSIGNMENTS = /^(?:[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|[^\s;&|]*)\s+)+/;

/**
 * A command as a row names it, rule G1 of the run page: without a shell wrapper (`bash -lc '...'`), the `cd`, the
 * chain of `export`s and the other setup it starts with (`commandGist`), and the variables it assigns before the
 * program; a command that is all of that stays whole.
 */
export function commandName(command: string): string {
  const whole = unwrapShell(command.trim());
  const gist = commandGist(whole);
  const rest = gist.replace(ASSIGNMENTS, "").trim();
  // commandGist keeps the last part of a command that is setup only: such a command has no program to name it by.
  if (/^(?:cd|export)\s/.test(rest) && !/&&|;|\|/.test(rest)) return whole;
  return rest || gist;
}

/** Claude Code's result of an `Agent` call it runs in the background: no report, only how to reach the agent. */
const LAUNCHED = /^Async agent launched/;
/** What Claude Code adds after a foreground subagent's report: its id and usage, for the main agent. */
const AGENT_TRAILER = /\n+agentId: [\s\S]*$/;

/** A subagent's report as its `Agent` call returned it, or null when the call only launched it. */
function callReport(output: string | null): string | null {
  if (!output || LAUNCHED.test(output.trim())) return null;
  return text(output.replace(AGENT_TRAILER, ""));
}

/** What a brief asks the subagent to report: its section that says so, at most this many lines. */
const ASK_LINES = 16;
const ASK_HEAD = /^\s*(?:#{1,6}\s+|\*\*)?\s*(?:report|return|reply|respond|deliverables?|output format|what to (?:report|return)|when (?:you are )?done|final (?:report|answer|message))\b/i;
const HEADING = /^\s*#{1,6}\s+/;

/**
 * The part of a subagent's brief that says what it should report: from the first line that starts with "Report",
 * "Return", "Reply", "Deliverables", "Output format" or "When done" (a heading or not) to the next heading, at most
 * ASK_LINES lines; null when the brief says nothing of the kind.
 */
export function reportAsk(prompt: string | null): string | null {
  if (!prompt) return null;
  const lines = prompt.split("\n");
  const start = lines.findIndex((line) => ASK_HEAD.test(line));
  if (start < 0) return null;
  const level = HEADING.test(lines[start]) ? (/^\s*(#+)/.exec(lines[start])?.[1].length ?? 1) : 7;
  const taken: string[] = [lines[start]];
  for (const line of lines.slice(start + 1)) {
    const heading = /^\s*(#+)\s+/.exec(line);
    if (heading && heading[1].length <= level) break;
    if (taken.length >= ASK_LINES) break;
    taken.push(line);
  }
  return text(taken.join("\n"));
}

/** The first lines of a brief, for a card: at most `lines` lines that are not blank, each cut at `chars`. */
export function briefLines(prompt: string | null, lines = 3, chars = 240): string[] {
  if (!prompt) return [];
  return prompt
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, lines)
    .map((line) => (line.length > chars ? `${line.slice(0, chars - 1)}…` : line));
}

/** The task of a subagent or command by its id, or by the id of the call that started it. */
export function taskOf(tasks: readonly RunTask[], id: string | null): RunTask | null {
  if (!id) return null;
  return tasks.find((task) => task.id === id) ?? tasks.find((task) => task.callId === id) ?? null;
}

type Raw = Record<string, unknown>;

const SUBTYPES = new Set(["task_started", "task_progress", "task_updated", "task_notification"]);
const ENDS: Record<string, TaskStatus> = { completed: "completed", failed: "failed", stopped: "stopped", killed: "stopped" };
const ACTIVITY_PREFIX = "Running ";

const text = (value: unknown): string | null => (typeof value === "string" && value.trim() ? value.trim() : null);
const count = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null);

function kindOf(type: string): TaskKind {
  if (type === "local_agent") return "agent";
  if (type === "local_bash") return "command";
  return "other";
}

/** The task event an event holds, or null. */
function taskEvent(event: RunEvent): Raw | null {
  if (event.kind !== "output") return null;
  const raw = (event.body as Raw | null)?.raw;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const data = raw as Raw;
  if (data.type !== "system" || !SUBTYPES.has(data.subtype as string) || !text(data.task_id)) return null;
  return data;
}

function firstLine(value: unknown): string | null {
  const found = text(value);
  return found ? found.split("\n", 1)[0].trim() || null : null;
}

/** The run's tasks in the order they started. */
export function runTasks(events: readonly RunEvent[]): RunTask[] {
  const tasks = new Map<string, Reading>();
  const calls = new Map<string, StartCall>();
  const seen = new Set<number>();
  const ordered = [...events].sort((a, b) => a.seq - b.seq);
  for (const event of ordered) {
    if (seen.has(event.seq)) continue;
    seen.add(event.seq);
    readCall(event, calls);
    const data = taskEvent(event);
    if (!data) continue;
    const id = data.task_id as string;
    let task = tasks.get(id);
    if (!task) {
      const type = text(data.task_type) ?? (text(data.subagent_type) ? "local_agent" : "unknown");
      task = {
        id,
        kind: kindOf(type),
        type,
        name: text(data.description) ?? id,
        agentType: null,
        parent: null,
        background: false,
        callId: null,
        status: "running",
        startedAt: event.at,
        endedAt: null,
        activity: null,
        lastTool: null,
        toolUses: null,
        tokens: null,
        summary: null,
        prompt: null,
        result: null,
        command: null,
        firstSeq: event.seq,
        seq: event.seq,
      };
      tasks.set(id, task);
    }
    task.seq = event.seq;
    task.agentType = text(data.subagent_type) ?? task.agentType;
    task.parent = text(data.parent_task_id) ?? task.parent;
    task.callId = text(data.tool_use_id) ?? task.callId;
    const usage = data.usage as Raw | undefined;
    if (usage && typeof usage === "object") {
      task.toolUses = count(usage.tool_uses) ?? task.toolUses;
      task.tokens = count(usage.total_tokens) ?? task.tokens;
    }
    switch (data.subtype) {
      case "task_started":
        // A task that starts again (a subagent woken by its own background command) runs again.
        if (text(data.task_type)) {
          task.type = data.task_type as string;
          task.kind = kindOf(task.type);
        }
        task.name = text(data.description) ?? task.name;
        task.prompt = text(data.prompt) ?? task.prompt;
        task.background = data.is_backgrounded === true;
        task.status = "running";
        task.endedAt = null;
        break;
      case "task_progress": {
        const said = text(data.description);
        task.activity = said ? (said.startsWith(ACTIVITY_PREFIX) ? said.slice(ACTIVITY_PREFIX.length) : said) : task.activity;
        task.lastTool = text(data.last_tool_name) ?? task.lastTool;
        break;
      }
      case "task_updated": {
        const patch = data.patch as Raw | undefined;
        if (patch && typeof patch === "object") {
          if (typeof patch.is_backgrounded === "boolean") task.background = patch.is_backgrounded;
          const ended = ENDS[patch.status as string];
          if (ended && task.status === "running") {
            task.status = ended;
            task.endedAt = event.at;
          }
        }
        break;
      }
      case "task_notification": {
        // How it ended, which task_updated may have said first (killed there is stopped here).
        task.status = ENDS[data.status as string] ?? "completed";
        task.endedAt ??= event.at;
        task.summary = firstLine(data.summary) ?? task.summary;
        if (task.kind === "agent") task.notified = text(data.summary) ?? task.notified;
        break;
      }
    }
  }
  return finish([...tasks.values()], calls);
}

/** What the tool call that started a task says of it: a subagent's brief and report, a command's line. */
type StartCall = {
  seq: number;
  at: string;
  title: string | null;
  input: Raw | null;
  status: string | null;
  output: string | null;
  endedAt: string | null;
};

function readCall(event: RunEvent, calls: Map<string, StartCall>) {
  if (event.kind !== "tool_call" && event.kind !== "tool_call_update") return;
  const body = event.body as Raw | null;
  if (!body || typeof body !== "object") return;
  const id = text(body.toolCallId) ?? text(body.id);
  if (!id) return;
  let found = calls.get(id);
  if (!found) {
    found = { seq: event.seq, at: event.at, title: null, input: null, status: null, output: null, endedAt: null };
    calls.set(id, found);
  }
  found.title = text(body.title) ?? found.title;
  const input = body.rawInput ?? body.input;
  if (input && typeof input === "object" && !Array.isArray(input)) found.input = input as Raw;
  const status = text(body.status);
  if (status) found.status = status;
  if (event.kind === "tool_call_update") {
    found.output = contentText(body.content) ?? (typeof body.rawOutput === "string" ? body.rawOutput : null) ?? found.output;
    if (status === "completed" || status === "failed") found.endedAt = event.at;
  }
}

/** A subagent's report as its notification holds it, before `finish` picks the report. */
type Reading = RunTask & { notified?: string | null };

function finish(read: Reading[], calls: Map<string, StartCall>): RunTask[] {
  const named = new Set(read.map((task) => task.callId).filter((id): id is string => id !== null));
  const tasks: RunTask[] = read.map(({ notified = null, ...task }) => {
    const started = task.callId ? calls.get(task.callId) : undefined;
    if (task.kind === "agent") {
      task.prompt ??= text(started?.input?.prompt);
      const report = callReport(started?.output ?? null);
      task.result = task.background ? (notified ?? report) : (report ?? notified);
    } else if (task.kind === "command") {
      const line = text(started?.input?.command) ?? text(started?.input?.cmd);
      task.command = line ? commandName(line) : null;
    }
    return task;
  });
  // An Agent call no task event names, from a runtime that sends none: a subagent of its own.
  for (const [id, call] of calls) {
    if (named.has(id) || !call.title || !AGENT_TOOLS.has(call.title)) continue;
    const input = call.input ?? {};
    const launched = !!call.output && LAUNCHED.test(call.output.trim());
    const ended = !launched && (call.status === "completed" || call.status === "failed");
    tasks.push({
      id,
      kind: "agent",
      type: "local_agent",
      name: text(input.description) ?? text(input.name) ?? id,
      agentType: text(input.subagent_type),
      parent: null,
      background: launched || input.run_in_background === true,
      callId: id,
      status: ended ? (call.status === "failed" ? "failed" : "completed") : "running",
      startedAt: call.at,
      endedAt: ended ? call.endedAt : null,
      activity: null,
      lastTool: null,
      toolUses: null,
      tokens: null,
      summary: firstLine(callReport(call.output)),
      prompt: text(input.prompt),
      result: callReport(call.output),
      command: null,
      firstSeq: call.seq,
      seq: call.seq,
    });
  }
  return tasks.sort((a, b) => a.firstSeq - b.firstSeq);
}
