import type { RunMove } from "./log-model";
import {
  HANDBACK_STATES,
  HELD_STATES,
  isTerminalState,
  MESSAGE_STATES,
  type Run,
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

/** The numbers of a run's usage, flattened (`{"cache": {"read": 3}}` reads as `cache.read`), at most `limit`. */
export function readUsage(value: unknown, limit = 8): { key: string; value: number }[] {
  const found: { key: string; value: number }[] = [];
  const visit = (inner: unknown, prefix: string) => {
    if (found.length >= limit) return;
    if (typeof inner === "number" && Number.isFinite(inner)) found.push({ key: prefix, value: inner });
    else if (typeof inner === "object" && inner !== null && !Array.isArray(inner)) {
      for (const [key, next] of Object.entries(inner)) visit(next, prefix ? `${prefix}.${key}` : key);
    }
  };
  visit(value, "");
  return found;
}

/** The first 7 characters of a commit, as git shows it. */
export function shortSha(sha: string): string {
  return sha.slice(0, 7);
}
