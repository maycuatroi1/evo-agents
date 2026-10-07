import type { RunMove } from "./log-model";
import {
  HANDBACK_STATES,
  HELD_STATES,
  isTerminalState,
  MESSAGE_STATES,
  type Run,
  type RunLease,
  type RunState,
  TAKEOVER_STATES,
} from "./queries";

/**
 * How the run page reads one run: the stepper of its states, which of the owner's controls it offers, and small
 * helpers for its facts. Pure functions, shared by the page and its tests.
 */

// The stepper.

export const PHASES = ["queued", "leased", "running", "verifying", "review", "done"] as const;
export type Phase = (typeof PHASES)[number];

export type PhaseItem = {
  phase: Phase;
  /** done: passed; current: the run is in it now; stopped: the run ended there; todo: not reached. */
  status: "done" | "current" | "stopped" | "todo";
  /** When the run entered it, when known (ISO 8601). */
  at: string | null;
};

export type Stepper = {
  items: PhaseItem[];
  /** failed, lost or cancelled: how the run ended, shown on the stopped item. */
  ended: Extract<RunState, "failed" | "lost" | "cancelled"> | null;
};

const PHASE_OF: Partial<Record<RunState, Phase>> = {
  queued: "queued",
  leased: "leased",
  running: "running",
  interactive: "running",
  // A plan run's agent waits for its owner's answer between turns, or was parked for want of one: still its running phase.
  waiting: "running",
  parked: "running",
  verifying: "verifying",
  review: "review",
  done: "done",
};

type StepperRun = Pick<Run, "state" | "approval" | "queued_at" | "leased_at" | "started_at" | "finished_at">;

/** The latest move into one of `states`. */
function lastMoveTo(moves: readonly RunMove[], states: readonly RunState[]): RunMove | null {
  for (let index = moves.length - 1; index >= 0; index -= 1) {
    if (states.includes(moves[index].to)) return moves[index];
  }
  return null;
}

/**
 * The states a run goes through, in order, with where it is now. Review shows only for a run held for review. A run
 * that ended failed, lost or cancelled stops at the state it left last: the `from` of its final move when the log
 * has it, else the last timestamp the run carries.
 */
export function stepperModel(run: StepperRun, moves: readonly RunMove[]): Stepper {
  const phases = PHASES.filter((phase) => phase !== "review" || run.approval === "review" || run.state === "review");
  const firstRunning = moves.find((move) => move.to === "running" || move.to === "interactive") ?? null;
  const at: Record<Phase, string | null> = {
    queued: run.queued_at,
    leased: run.leased_at ?? lastMoveTo(moves, ["leased"])?.at ?? null,
    running: run.started_at ?? firstRunning?.at ?? null,
    verifying: lastMoveTo(moves, ["verifying"])?.at ?? null,
    review: lastMoveTo(moves, ["review"])?.at ?? null,
    done: run.state === "done" ? (run.finished_at ?? lastMoveTo(moves, ["done"])?.at ?? null) : null,
  };

  if (run.state === "done") {
    return { items: phases.map((phase) => ({ phase, status: "done", at: at[phase] })), ended: null };
  }
  if (isTerminalState(run.state)) {
    const final = lastMoveTo(moves, [run.state]);
    const from = final?.from ? PHASE_OF[final.from] : undefined;
    const stop: Phase = from ?? (run.started_at ? "running" : run.leased_at ? "leased" : "queued");
    const stopIndex = Math.max(phases.indexOf(stop), 0);
    return {
      items: phases.map((phase, index) => ({
        phase,
        status: index < stopIndex ? "done" : index === stopIndex ? "stopped" : "todo",
        at: index <= stopIndex ? at[phase] : null,
      })),
      ended: run.state as Stepper["ended"],
    };
  }
  const current = Math.max(phases.indexOf(PHASE_OF[run.state] ?? "queued"), 0);
  return {
    items: phases.map((phase, index) => ({
      phase,
      status: index < current ? "done" : index === current ? "current" : "todo",
      at: index <= current ? at[phase] : null,
    })),
    ended: null,
  };
}

// The timeline: the stepper with the time spent between its phases.

export type PhaseTone = "done" | "current" | "queued" | "waiting" | "review" | "success" | "failed" | "ended" | "todo";

export type TimelinePhase = {
  phase: Phase;
  status: PhaseItem["status"];
  /** How its node looks: running work pulses; a person is needed (waiting, parked) in attention; failed in danger. */
  tone: PhaseTone;
  /** When the run entered it; for the phase a run ended in, when it ended. */
  at: string | null;
  /** On the line to the next phase: the time from this phase to the next one, or, for the current phase, to now. */
  gapMs: number | null;
  /** The gap counts up with the clock. */
  live: boolean;
  /** A run waiting for an answer, or parked for want of one: since when. */
  since: string | null;
  /** The run's own state, named instead of the phase: interactive, waiting, parked, or how it ended. */
  state: RunState | null;
};

export type Timeline = { phases: TimelinePhase[]; ended: Stepper["ended"] };

type TimelineRun = StepperRun & Pick<Run, "waiting_since" | "parked_at">;

const LIVE_PHASES = new Set<Phase>(["leased", "running", "verifying"]);

function gap(from: string | null, to: string | number | null): number | null {
  if (!from || to === null) return null;
  const end = typeof to === "number" ? to : Date.parse(to);
  const start = Date.parse(from);
  return Number.isNaN(start) || Number.isNaN(end) ? null : Math.max(0, end - start);
}

/**
 * The run's phases as the kit's RunTimeline draws them: the stepper's phases with the time spent between each and the
 * next on the line that joins them. The current phase's gap counts up to `now` (null on the server, so nothing ticks
 * before hydration). A waiting or parked run shows on its running phase under its state, since when it waits; a run
 * that ended badly shows how on the phase it stopped in, at the time it ended, and the phases after it are skipped.
 */
export function timelineModel(run: TimelineRun, moves: readonly RunMove[], now: number | null): Timeline {
  const stepper = stepperModel(run, moves);
  const ended = stepper.ended;
  const endedAt = ended ? (run.finished_at ?? lastMoveTo(moves, [run.state])?.at ?? null) : null;
  const since = run.state === "waiting" ? run.waiting_since : run.state === "parked" ? run.parked_at : null;
  const items = stepper.items;
  const phases = items.map((item, index): TimelinePhase => {
    const at = item.status === "stopped" ? (endedAt ?? item.at) : item.at;
    const next = items[index + 1];
    const nextAt = next ? (next.status === "stopped" ? (endedAt ?? next.at) : next.status === "todo" ? null : next.at) : null;
    let tone: PhaseTone = item.status === "done" ? "done" : item.status === "todo" ? "todo" : "current";
    let state: RunState | null = null;
    let gapMs = item.status === "done" || (next && next.status === "stopped") ? gap(at, nextAt) : null;
    let live = false;
    if (item.status === "done" && item.phase === "done") tone = "success";
    if (item.status === "stopped") {
      tone = ended === "failed" ? "failed" : "ended";
      state = ended;
    }
    if (item.status === "current") {
      if (run.state === "waiting" || run.state === "parked") {
        tone = "waiting";
        state = run.state;
      } else if (run.state === "interactive") {
        state = "interactive";
      } else if (item.phase === "queued") {
        tone = "queued";
      } else if (item.phase === "review") {
        tone = "review";
      }
      if (!LIVE_PHASES.has(item.phase) && tone === "current") tone = "queued";
      gapMs = gap(tone === "waiting" ? (since ?? at) : at, now);
      live = now !== null && gapMs !== null;
    }
    return { phase: item.phase, status: item.status, tone, at, gapMs, live, since: tone === "waiting" ? since : null, state };
  });
  return { phases, ended };
}

// The owner's controls.

export type Ask = "offer" | "asked" | "none";

export type RunControls = {
  /** The visitor dispatched the run: only they steer it. */
  owner: boolean;
  cancel: Ask;
  takeover: Ask;
  handback: Ask;
  approve: boolean;
  rerun: boolean;
  message: boolean;
};

const NO_CONTROLS: RunControls = {
  owner: false,
  cancel: "none",
  takeover: "none",
  handback: "none",
  approve: false,
  rerun: false,
  message: false,
};

export type RunViewer = { login: string; role: "reader" | "writer" | "admin" | null; admin: boolean };

type ControlRun = Pick<Run, "kind" | "state" | "dispatched_by" | "cancel_requested_at" | "takeover_requested_at" | "handback_requested_at">;

/**
 * What the page offers the visitor for this run, as the API decides it (docs/workers.md): every control belongs to
 * the member who dispatched the run, and approve and rerun also need the writer role on the project. An ask the
 * owner made already (cancel of a held run, takeover, handback) shows as asked until the worker answers it. A plan run
 * is never rerun (the hub answers 409: Run plan dispatches the plan again), and never in review.
 */
export function runControls(run: ControlRun, viewer: RunViewer | null): RunControls {
  if (!viewer || viewer.login !== run.dispatched_by) return NO_CONTROLS;
  const state = run.state;
  const writer = viewer.role === "writer" || viewer.role === "admin";
  const terminal = isTerminalState(state);
  const held = (HELD_STATES as readonly string[]).includes(state);
  return {
    owner: true,
    cancel: terminal ? "none" : held && run.cancel_requested_at ? "asked" : "offer",
    takeover: (TAKEOVER_STATES as readonly string[]).includes(state) ? (run.takeover_requested_at ? "asked" : "offer") : "none",
    handback: (HANDBACK_STATES as readonly string[]).includes(state) ? (run.handback_requested_at ? "asked" : "offer") : "none",
    approve: writer && state === "review",
    rerun: writer && terminal && run.kind !== "plan",
    message: (MESSAGE_STATES as readonly string[]).includes(state) && !(held && run.cancel_requested_at),
  };
}

// Facts.

/** Bytes of `text` as UTF-8, the unit of the API's message limit. */
export function utf8Bytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

/** Seconds until the lease runs out (0 once it has), or null for a run without a lease or before the page hydrates. */
export function leaseSecondsLeft(run: Pick<Run, "state" | "lease_expires_at">, now: number | null): number | null {
  if (now === null || !run.lease_expires_at || !(HELD_STATES as readonly string[]).includes(run.state)) return null;
  return Math.max(0, Math.round((Date.parse(run.lease_expires_at) - now) / 1000));
}

/** Where a lease of the run stands: given back or taken by the hub, past its end, or still out with the worker. */
export type LeaseState = "revoked" | "expired" | "out";

export function leaseState(lease: Pick<RunLease, "revoked_at" | "expires_at">, now: number): LeaseState {
  if (lease.revoked_at) return "revoked";
  if (lease.expires_at && Date.parse(lease.expires_at) <= now) return "expired";
  return "out";
}

/** What a lease answered for: the variable of an env lease, or each origin of a git lease. */
export function leaseTargets(lease: Pick<RunLease, "kind" | "target">): string[] {
  if (lease.kind === "env") return [lease.target];
  return lease.target.split(/\s+/).filter(Boolean);
}

export type Diffstat = { files: number; insertions: number; deletions: number };

/** A run's diffstat as the worker reported it, or null when it is missing or not in that shape. */
export function readDiffstat(value: unknown): Diffstat | null {
  if (typeof value !== "object" || value === null) return null;
  const record = value as Record<string, unknown>;
  const number = (key: string) => (typeof record[key] === "number" && Number.isFinite(record[key]) ? (record[key] as number) : null);
  const files = number("files");
  const insertions = number("insertions");
  const deletions = number("deletions");
  return files === null || insertions === null || deletions === null ? null : { files, insertions, deletions };
}

export type VerifyResult = { command: string; exitCode: number | null; durationMs: number | null };

/** The verify commands the worker ran, as it reported them; entries of another shape are left out. */
export function readVerify(value: unknown): VerifyResult[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): VerifyResult[] => {
    if (typeof item !== "object" || item === null) return [];
    const record = item as Record<string, unknown>;
    if (typeof record.command !== "string") return [];
    return [
      {
        command: record.command,
        exitCode: typeof record.exit_code === "number" ? record.exit_code : null,
        durationMs: typeof record.duration_ms === "number" ? record.duration_ms : null,
      },
    ];
  });
}

/** The first 7 characters of a commit, as git shows it. */
export function shortSha(sha: string): string {
  return sha.slice(0, 7);
}
