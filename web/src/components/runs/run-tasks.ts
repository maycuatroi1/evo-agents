import type { RunEvent } from "./queries";

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
 * - `task_notification`: the end, with `status` (completed, failed, stopped) and the task's `summary`.
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
  /** The seq of its last event. */
  seq: number;
};

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
  const tasks = new Map<string, RunTask>();
  const seen = new Set<number>();
  const ordered = [...events].sort((a, b) => a.seq - b.seq);
  for (const event of ordered) {
    if (seen.has(event.seq)) continue;
    seen.add(event.seq);
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
        break;
      }
    }
  }
  return [...tasks.values()];
}

export type TaskRow = { task: RunTask; children: RunTask[] };

export type TaskBoard = {
  /** What runs now: subagents (each with its own commands running) and the main agent's background commands. */
  running: TaskRow[];
  /** Subagents and background commands that ended, the last to end first. */
  finished: RunTask[];
  /** How many subagents and commands run now, children counted. */
  agents: number;
  commands: number;
};

/**
 * What the run's page shows of its tasks. A command the main agent runs in the foreground is left out: the trace shows
 * it at its end, running. Once the run is no longer active, nothing runs whatever its events last said: the CLI that
 * ran them has exited.
 */
export function taskBoard(tasks: readonly RunTask[], active: boolean): TaskBoard {
  const running = (task: RunTask) => active && task.status === "running";
  const agentIds = new Set(tasks.filter((task) => task.kind === "agent").map((task) => task.id));
  const shown = (task: RunTask) => task.kind !== "command" || task.background || (task.parent !== null && agentIds.has(task.parent));
  const rows: TaskRow[] = [];
  for (const task of tasks) {
    if (!running(task) || !shown(task)) continue;
    // A subagent's command shows under its subagent while that runs; once the subagent ended, only a background one
    // goes on, on its own row.
    const owner = task.parent !== null ? tasks.find((other) => other.id === task.parent) : undefined;
    if (owner && (running(owner) || !task.background)) continue;
    rows.push({ task, children: task.kind === "agent" ? tasks.filter((child) => child.parent === task.id && running(child)) : [] });
  }
  const finished = tasks
    .filter((task) => !running(task) && task.status !== "running" && (task.kind !== "command" || task.background))
    .sort((a, b) => (b.endedAt ?? "").localeCompare(a.endedAt ?? "") || b.seq - a.seq);
  const all = rows.flatMap((row) => [row.task, ...row.children]);
  return {
    running: rows,
    finished,
    agents: all.filter((task) => task.kind === "agent").length,
    commands: all.filter((task) => task.kind !== "agent").length,
  };
}
