/**
 * The sessions of a plan run with a session for each step, as the worker reports them in the run's usage when the run
 * ends (`usage.sessions`, evo_agents/worker/runner/sessions.py): each with its step, the agent session's id, the run
 * that ran it (a run that resumes a parked one lists the parked run's too) and the numbers of its usage. Pure
 * functions, shared by the run page and its tests.
 */

export type StepSession = {
  step: string;
  sessionId: string | null;
  runId: number | null;
  /** The largest context one request of the session read, null when its runtime did not report it. */
  peak: number | null;
  /** The tokens its requests read from the prompt cache, 0 when it reported none. */
  cacheRead: number;
};

export type StepSessions = { sessions: StepSession[]; leftOut: number };

type Body = Record<string, unknown>;

function isRecord(value: unknown): value is Body {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function readSession(item: unknown): StepSession | null {
  if (!isRecord(item)) return null;
  const step = typeof item.step === "string" || typeof item.step === "number" ? String(item.step) : null;
  if (step === null) return null;
  const runId = count(item.run_id);
  return {
    step,
    sessionId: typeof item.session_id === "string" && item.session_id ? item.session_id : null,
    runId: runId !== null && Number.isInteger(runId) ? runId : null,
    peak: count(item.peak_context_tokens),
    // Claude Code's key, Codex's, then opencode's.
    cacheRead: count(item.cache_read_input_tokens) ?? count(item.cached_input_tokens) ?? count(item.cache_read) ?? 0,
  };
}

/** The sessions a run's usage lists, in order, and how many earlier ones the worker left out; null for usage without a list. */
export function readStepSessions(usage: unknown): StepSessions | null {
  if (!isRecord(usage) || !Array.isArray(usage.sessions)) return null;
  const sessions = usage.sessions.map(readSession).filter((item): item is StepSession => item !== null);
  return { sessions, leftOut: count(usage.sessions_left_out) ?? 0 };
}
