import type { RunTask } from "./run-tasks";

/** What the run's page lists of its tasks (run-tasks.ts): subagents apart from background commands, running and ended. */

export type TaskRow = { task: RunTask; children: RunTask[] };

/** Subagents and commands, listed apart: a subagent's row holds its own commands running. */
export type TaskLists = { agents: TaskRow[]; commands: RunTask[] };

export type TaskBoard = {
  /** What runs now: subagents (each with its own commands running), then the background commands on their own. */
  running: TaskLists;
  /** Subagents and background commands that ended, apart, the last to end first. */
  finished: { agents: RunTask[]; commands: RunTask[] };
  /** How many subagents and commands run now, children counted. */
  agents: number;
  commands: number;
};

const lastEnded = (a: RunTask, b: RunTask) => (b.endedAt ?? "").localeCompare(a.endedAt ?? "") || b.seq - a.seq;

/**
 * What the run's page shows of its tasks, subagents apart from commands. A command the main agent runs in the
 * foreground is left out: the trace shows it at its end, running. Once the run is no longer active, nothing runs
 * whatever its events last said: the CLI that ran them has exited.
 */
export function taskBoard(tasks: readonly RunTask[], active: boolean): TaskBoard {
  const running = (task: RunTask) => active && task.status === "running";
  const agentIds = new Set(tasks.filter((task) => task.kind === "agent").map((task) => task.id));
  const shown = (task: RunTask) => task.kind !== "command" || task.background || (task.parent !== null && agentIds.has(task.parent));
  const agents: TaskRow[] = [];
  const commands: RunTask[] = [];
  for (const task of tasks) {
    if (!running(task) || !shown(task)) continue;
    // A subagent's command shows under its subagent while that runs; once the subagent ended, only a background one
    // goes on, on its own row.
    const owner = task.parent !== null ? tasks.find((other) => other.id === task.parent) : undefined;
    if (owner && (running(owner) || !task.background)) continue;
    if (task.kind === "agent") agents.push({ task, children: tasks.filter((child) => child.parent === task.id && running(child)) });
    else commands.push(task);
  }
  const ended = tasks.filter((task) => !running(task) && task.status !== "running" && (task.kind !== "command" || task.background));
  const children = agents.reduce((sum, row) => sum + row.children.length, 0);
  return {
    running: { agents, commands },
    finished: {
      agents: ended.filter((task) => task.kind === "agent").sort(lastEnded),
      commands: ended.filter((task) => task.kind !== "agent").sort(lastEnded),
    },
    agents: agents.length,
    commands: commands.length + children,
  };
}
