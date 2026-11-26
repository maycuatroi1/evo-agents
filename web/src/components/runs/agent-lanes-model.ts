import type { RunEvent } from "./queries";
import type { RunTask } from "./run-tasks";
import { parentOf } from "./run-tasks";

/** What the Agents section draws: one lane for the main agent, one for each sub-agent, one for background commands. */

export type LaneScope = "step" | "run";

/** A stretch of a lane's time in epoch ms: work, or the main agent waiting on a sub-agent it handed work to. */
export type Segment = { from: number; to: number; kind: "work" | "wait"; running: boolean };

/** A point on the main agent's lane: it handed work over (`handoff`) or a sub-agent gave its report back (`return`). */
export type Mark = { at: number; kind: "handoff" | "return"; agent: string };

export type LaneStatus = "working" | "waiting" | "running" | "completed" | "failed" | "stopped" | "idle";

export type Lane = {
  id: string;
  kind: "main" | "agent" | "commands";
  /** The sub-agent's name, or null for the main agent and the commands, which the page names. */
  name: string | null;
  status: LaneStatus;
  /** What it does now: the last call of the main agent, a sub-agent's last call, the newest background command. */
  work: string | null;
  /** How many background commands the commands lane holds, and how many run. */
  count: number;
  live: number;
  segments: Segment[];
  marks: Mark[];
  task: RunTask | null;
  /** The call a command lane shows in the trace. */
  call: string | null;
  /** Finished lanes fold when the run has many. */
  done: boolean;
};

export type Lanes = { lanes: Lane[]; from: number; to: number };

const ms = (iso: string | null) => (iso ? Date.parse(iso) : Number.NaN);
/** The first and the last time the events carry: the hub's own state events are numbered by arrival, not by time. */
function span(events: readonly RunEvent[]): [number, number] {
  const times = events.map((event) => ms(event.at)).filter((time) => !Number.isNaN(time));
  return times.length ? [Math.min(...times), Math.max(...times)] : [Number.NaN, Number.NaN];
}

const STEP_DONE = /\bworker\s+step\s+\S+\s+done\b/;

/** The command of a tool call event, when it has one. */
function commandOf(event: RunEvent): string | null {
  const input = event.body.rawInput;
  if (typeof input !== "object" || input === null) return null;
  const command = (input as Record<string, unknown>).command;
  return typeof command === "string" ? command : null;
}

/**
 * Where "This step" starts: after the last `worker step <id> done` the main agent ran, that is, where the step it works
 * on began. When that done is the run's last word (the step it reports is the one the run did), the step began after the
 * one before it. Without one, at the run's first event.
 */
export function stepStart(events: readonly RunEvent[]): number {
  const [first] = span(events);
  const marks = events.filter((event) => event.kind === "tool_call" && parentOf(event) === null && STEP_DONE.test(commandOf(event) ?? ""));
  const work = (event: RunEvent) => event.kind === "tool_call" || event.kind === "agent_message_chunk";
  for (let i = marks.length - 1; i >= 0; i--) {
    const after = events.some((event) => event.seq > marks[i].seq && work(event) && !STEP_DONE.test(commandOf(event) ?? ""));
    if (after) return ms(marks[i].at);
  }
  return Number.isNaN(first) ? 0 : first;
}

function merge(spans: [number, number][]): [number, number][] {
  const sorted = [...spans].sort((a, b) => a[0] - b[0]);
  const out: [number, number][] = [];
  for (const [from, to] of sorted) {
    const last = out[out.length - 1];
    if (last && from <= last[1]) last[1] = Math.max(last[1], to);
    else out.push([from, to]);
  }
  return out;
}

/** The part of [from, to] a lane draws inside the window, or null when none of it falls there. */
function clip(from: number, to: number, lo: number, hi: number): [number, number] | null {
  const a = Math.max(from, lo);
  const b = Math.min(to, hi);
  return b >= a && (b > a || (from >= lo && from <= hi)) ? [a, b] : null;
}

const endOf = (task: RunTask, running: boolean, to: number) => (running ? to : (ms(task.endedAt) || to));

/** What the main agent's last tool call said it did, for the line under its name. */
function mainWork(events: readonly RunEvent[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    if (event.kind !== "tool_call" || parentOf(event) !== null) continue;
    const input = event.body.rawInput as Record<string, unknown> | undefined;
    const description = typeof input?.description === "string" ? input.description : null;
    const title = typeof event.body.title === "string" ? event.body.title : null;
    return description ?? title;
  }
  return null;
}

/**
 * The lanes of a run on one time axis, from its events and its tasks (run-tasks.ts): the main agent (its time split into
 * work and waiting on the sub-agents it handed work to and did not send to the background; a mark where it handed work
 * over and where a report came back), each sub-agent, and one lane for the background commands. Empty when the run has
 * neither a sub-agent nor a background command. `active` is whether the run still runs; `now` ends the open stretches.
 */
export function buildLanes(events: readonly RunEvent[], tasks: readonly RunTask[], active: boolean, now: number | null, scope: LaneScope): Lanes | null {
  const agents = tasks.filter((task) => task.kind === "agent");
  const commands = tasks.filter((task) => task.kind === "command" && task.background);
  if (!agents.length && !commands.length) return null;
  const [firstEvent, lastEvent] = span(events);
  const runFrom = Number.isNaN(firstEvent) ? Math.min(...tasks.map((task) => ms(task.startedAt))) : firstEvent;
  const last = Number.isNaN(lastEvent) ? runFrom : lastEvent;
  const to = Math.max(active && now !== null ? now : last, last, ...tasks.map((task) => ms(task.endedAt) || 0), runFrom + 1_000);
  const from = Math.min(scope === "step" ? Math.max(stepStart(events), runFrom) : runFrom, to - 1_000);

  const running = (task: RunTask) => active && task.status === "running";
  const lanes: Lane[] = [];

  // Waiting: the main agent has a foreground sub-agent out.
  const waits = merge(
    agents
      .filter((task) => !task.background)
      .map((task): [number, number] => [ms(task.startedAt), endOf(task, running(task), to)])
      .filter(([a, b]) => !Number.isNaN(a) && !Number.isNaN(b)),
  );
  const mainSegments: Segment[] = [];
  let cursor = from;
  const live = (b: number) => active && b >= to;
  for (const [a, b] of waits) {
    const w = clip(a, b, from, to);
    if (!w) continue;
    if (w[0] > cursor) mainSegments.push({ from: cursor, to: w[0], kind: "work", running: false });
    mainSegments.push({ from: w[0], to: w[1], kind: "wait", running: live(w[1]) });
    cursor = w[1];
  }
  if (cursor < to) mainSegments.push({ from: cursor, to, kind: "work", running: live(to) });
  const waiting = agents.some((task) => !task.background && running(task));
  const marks: Mark[] = [];
  for (const task of agents) {
    const start = ms(task.startedAt);
    if (start >= from && start <= to) marks.push({ at: start, kind: "handoff", agent: task.name });
    const end = ms(task.endedAt);
    if (!running(task) && task.status !== "running" && end >= from && end <= to) marks.push({ at: end, kind: "return", agent: task.name });
  }
  lanes.push({
    id: "main",
    kind: "main",
    name: null,
    status: !active ? "idle" : waiting ? "waiting" : "working",
    work: mainWork(events),
    count: 0,
    live: 0,
    segments: mainSegments,
    marks: marks.sort((a, b) => a.at - b.at),
    task: null,
    call: null,
    done: !active,
  });

  const byStart = [...agents].sort((a, b) => ms(b.startedAt) - ms(a.startedAt));
  for (const task of byStart) {
    const isRunning = running(task);
    const span = clip(ms(task.startedAt), endOf(task, isRunning, to), from, to);
    lanes.push({
      id: task.id,
      kind: "agent",
      name: task.name,
      status: isRunning ? "running" : task.status === "running" ? "stopped" : task.status,
      work: isRunning ? (task.activity ? (task.lastTool ? `${task.lastTool}: ${task.activity}` : task.activity) : null) : task.summary,
      count: 0,
      live: 0,
      segments: span ? [{ from: span[0], to: span[1], kind: "work", running: isRunning && span[1] >= to }] : [],
      marks: [],
      task,
      call: task.callId,
      done: !isRunning,
    });
  }

  if (commands.length) {
    const live = commands.filter(running);
    const segments: Segment[] = [];
    for (const task of commands) {
      const isRunning = running(task);
      const span = clip(ms(task.startedAt), endOf(task, isRunning, to), from, to);
      if (span) segments.push({ from: span[0], to: span[1], kind: "work", running: isRunning && span[1] >= to });
    }
    const newest = [...(live.length ? live : commands)].sort((a, b) => b.seq - a.seq)[0];
    lanes.push({
      id: "commands",
      kind: "commands",
      name: null,
      status: live.length ? "running" : "idle",
      work: newest.command ?? newest.name,
      count: commands.length,
      live: live.length,
      segments,
      marks: [],
      task: null,
      call: newest.callId,
      done: live.length === 0,
    });
  }
  return { lanes, from, to };
}
