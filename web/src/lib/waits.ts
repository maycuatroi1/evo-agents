import type { components } from "@/lib/api/schema";

/**
 * A plan run's wait (docs/workers.md, `evo-agents worker wait`): a decision of category `wait` that asks nobody
 * anything. Its `waiting_on` says what it waits for, as the hub read it: a run of a GitHub Actions workflow, a step of
 * another plan, the answer of another agent, the merge check of a pull request, a time, or an event of the hub. This
 * module holds what the web works out of it (web/DESIGN.md, The wait panel): the state of a workflow's run, job and
 * step, the progress by step, the time it took and the time left, and the subject a pill names.
 */
type Schemas = components["schemas"];
export type WaitingOn = Schemas["WaitingOn"];
export type WaitKind = WaitingOn["kind"];
export type WorkflowRunWait = Schemas["WorkflowRunWait"];
export type WorkflowJob = Schemas["WorkflowJob"];
export type WorkflowStep = Schemas["WorkflowStep"];
export type PlanStepWait = Schemas["PlanStepWait"];
export type AgentAnswerWait = Schemas["AgentAnswerWait"];
export type MergeCheckWait = Schemas["MergeCheckWait"];
export type EventWait = Schemas["EventWait"];

/** `runs.WAIT_DECISION_CATEGORY`. */
export const WAIT_CATEGORY = "wait";

/** Whether a decision is a wait: it asks nobody anything, and the hub answers it when what it waits for comes. */
export function isWait(decision: { category: string }): boolean {
  return decision.category === WAIT_CATEGORY;
}

/** Whether every open decision of a list is a wait, and there is one: the run waits on something else, not a person. */
export function onlyWaits(decisions: readonly { category: string; state?: string }[]): boolean {
  const open = decisions.filter((decision) => decision.state === undefined || decision.state === "open");
  return open.length > 0 && open.every(isWait);
}

/** A run, job or step of GitHub Actions as the panel shows it: its GitHub `status` and `conclusion` in one word. */
export type ActionsState = "done" | "running" | "failed" | "skipped" | "cancelled" | "queued";

const FAILED = new Set(["failure", "timed_out", "startup_failure", "action_required"]);

export function actionsState(status: string | null, conclusion: string | null): ActionsState {
  if (status === "completed" || conclusion !== null) {
    if (conclusion === null || conclusion === "success" || conclusion === "neutral") return "done";
    if (FAILED.has(conclusion)) return "failed";
    if (conclusion === "skipped") return "skipped";
    if (conclusion === "cancelled" || conclusion === "stale") return "cancelled";
    return "done";
  }
  return status === "in_progress" ? "running" : "queued";
}

/** Whether a state is over: the step or job ran, failed, was skipped or cancelled. */
export function isOver(state: ActionsState): boolean {
  return state !== "running" && state !== "queued";
}

export type WorkflowProgress = {
  state: ActionsState;
  /** Steps over, and steps in all, of the jobs the hub read (a job not read yet counts none). */
  done: number;
  total: number;
  /** The step running now and its job; null when none runs. */
  current: { job: WorkflowJob; step: WorkflowStep; index: number } | null;
  /** The step that failed first and its job; null when none failed. */
  failed: { job: WorkflowJob; step: WorkflowStep } | null;
  /** How long the run ran: to now while it runs, to its last update once it is over; null before it started. */
  elapsedMs: number | null;
  /** About how long it has left, from how long its last runs took; null once over, or with no past runs. */
  leftMs: number | null;
};

function ms(value: string | null): number | null {
  if (value === null) return null;
  const time = Date.parse(value);
  return Number.isNaN(time) ? null : time;
}

/** The steps of a job as GitHub counts them: those listed, or more when the hub knows of more. */
export function stepsTotal(job: WorkflowJob): number {
  return Math.max(job.steps_total, job.steps.length);
}

/**
 * Where a run of a workflow is: its state, its steps over of all, the step running now, the step that failed, the
 * time it took and about how long it has left. `now` is the browser's clock (null on the server): the time counts on
 * between two reads of the hub, from the timestamps it read.
 */
export function workflowProgress(run: WorkflowRunWait, now: number | null): WorkflowProgress {
  const state = actionsState(run.status, run.conclusion);
  let done = 0;
  let total = 0;
  let current: WorkflowProgress["current"] = null;
  let failed: WorkflowProgress["failed"] = null;
  for (const job of run.jobs) {
    total += stepsTotal(job);
    job.steps.forEach((step, index) => {
      const stepState = actionsState(step.status, step.conclusion);
      if (isOver(stepState)) done += 1;
      if (stepState === "running" && current === null) current = { job, step, index };
      if (stepState === "failed" && failed === null) failed = { job, step };
    });
  }
  const start = ms(run.started_at) ?? ms(run.created_at);
  const end = isOver(state) ? (ms(run.updated_at) ?? ms(run.read_at)) : (now ?? ms(run.read_at));
  const elapsedMs = start !== null && end !== null ? Math.max(0, end - start) : null;
  const leftMs =
    !isOver(state) && run.typical_seconds !== null && elapsedMs !== null ? Math.max(0, run.typical_seconds * 1000 - elapsedMs) : null;
  return { state, done: Math.min(done, total), total, current, failed, elapsedMs, leftMs };
}

/** Whether a job of the panel starts open: one running or failed shows its steps at once. */
export function jobOpens(job: WorkflowJob): boolean {
  const state = actionsState(job.status, job.conclusion);
  return state === "running" || state === "failed";
}

/** The short commit a workflow ran on. */
export function shortSha(sha: string | null): string | null {
  return sha ? sha.slice(0, 7) : null;
}

/**
 * What a pill names for a wait, with the words for each kind left to the caller: the workflow and its steps done, the
 * plan and its step, the agent's handle, the pull request, the time, the event's name or kind.
 */
export type WaitSubject =
  | { kind: "workflow_run"; workflow: string; done: number; total: number }
  | { kind: "plan_step"; plan: string; step: string }
  | { kind: "agent_answer"; handle: string | null; run: number | null }
  | { kind: "merge_check"; number: number }
  | { kind: "time"; at: string | null }
  | { kind: "event"; name: string }
  /** A wait known only by the agent's reason, such as one of Home's overview, which does not say what it waits for. */
  | { kind: "reason"; reason: string }
  | { kind: "unknown" };

export function waitSubject(on: WaitingOn | null | undefined, wakeAt: string | null = null): WaitSubject {
  if (!on) return wakeAt ? { kind: "time", at: wakeAt } : { kind: "unknown" };
  if (on.kind === "workflow_run" && on.workflow_run) {
    const run = on.workflow_run;
    const { done, total } = workflowProgress(run, null);
    return { kind: "workflow_run", workflow: run.workflow ?? run.title ?? `#${run.run_id}`, done, total };
  }
  if (on.kind === "plan_step" && on.plan_step) return { kind: "plan_step", plan: on.plan_step.plan, step: on.plan_step.step };
  if (on.kind === "agent_answer" && on.agent_answer) {
    return { kind: "agent_answer", handle: on.agent_answer.handle, run: on.agent_answer.run?.id ?? null };
  }
  if (on.kind === "merge_check" && on.merge_check) return { kind: "merge_check", number: on.merge_check.number };
  if (on.kind === "event" && on.event) return { kind: "event", name: on.event.name ?? on.event.kind };
  if (on.kind === "time") return { kind: "time", at: on.time?.wake_at ?? wakeAt };
  return { kind: "unknown" };
}

/** Hours and minutes of a span, for the panel's "4m 12s so far" and "in 1 h 29 min". */
export function spanParts(spanMs: number): { hours: number; minutes: number; seconds: number } {
  const total = Math.max(0, Math.floor(spanMs / 1000));
  return { hours: Math.floor(total / 3600), minutes: Math.floor((total % 3600) / 60), seconds: total % 60 };
}

/** The merge judges that spoke (passed or failed) of all. */
export function judgesSpoke(check: MergeCheckWait): { spoke: number; total: number } {
  return { spoke: check.judges.filter((judge) => judge.state === "passed" || judge.state === "failed").length, total: check.judges.length };
}
