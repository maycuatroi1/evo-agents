import { envVarProblem } from "@/lib/secret-rules";

import type { AgentAccount, AgentAccountChange, AgentAccountWrite } from "./agent-queries";
import { MAX_SECRET_BYTES, SECRET_NAME } from "./login-queries";
import { MAX_PRIORITY, MAX_WEIGHT } from "./queries";

/**
 * The forms of an agent's accounts without React: Add (name, variable, value, workers, end), Edit (weight, priority,
 * workers, end) and Renew (a new value, and an end if one is given), their checks and the bodies they send. The checks
 * repeat the hub's (`evo_agents/hub/server/agent_accounts.py`) so a mistake is caught before the value leaves the page;
 * the hub decides again on every write. The value is never part of a form: each dialog reads it from its password
 * field and lets it go once the hub answers.
 */

export type AgentAccountForm = {
  name: string;
  envVar: string;
  workers: string[];
  /** The day it ends, as an <input type="date"> holds it (YYYY-MM-DD); empty for no end. */
  expires: string;
  weight: string;
  priority: string;
};

export type AgentField = keyof AgentAccountForm | "value";

export type AgentProblem =
  | "nameRequired"
  | "nameInvalid"
  | "nameTaken"
  | "envVarRequired"
  | "envVarInvalid"
  | "envVarDenied"
  | "valueRequired"
  | "valueTooLong"
  | "workersRequired"
  | "expiresInvalid"
  | "expiresPast"
  | "whole"
  | "range";

export type AgentProblems = Partial<Record<AgentField, AgentProblem>>;

const DAY = /^\d{4}-\d{2}-\d{2}$/;

/** The day of an instant in UTC, as an <input type="date"> holds it. */
export function utcDayOf(at: string): string {
  return new Date(at).toISOString().slice(0, 10);
}

/** 00:00 UTC of `day`; null for no day, undefined for a text that is no day. */
export function endOf(day: string): string | null | undefined {
  if (!day) return null;
  if (!DAY.test(day)) return undefined;
  const at = new Date(`${day}T00:00:00Z`);
  if (Number.isNaN(at.getTime()) || at.toISOString().slice(0, 10) !== day) return undefined;
  return at.toISOString();
}

export function emptyAgentForm(): AgentAccountForm {
  return { name: "", envVar: "", workers: [], expires: "", weight: "1", priority: "0" };
}

export function agentFormOf(account: AgentAccount): AgentAccountForm {
  return {
    name: account.name,
    envVar: account.env_var,
    workers: [...account.workers],
    expires: account.expires_at ? utcDayOf(account.expires_at) : "",
    weight: String(account.weight),
    priority: String(account.priority),
  };
}

/**
 * The workers an admin may bind an agent's account to: their own that are not revoked and shared ones, each serving
 * the agent's project, and those the account is bound to already; by name.
 */
export function offeredWorkers(
  workers: readonly { name: string; owner: string; status: string; shared: boolean; projects: string[] }[],
  me: string | undefined,
  project: string,
  current: readonly string[] = [],
): string[] {
  const usable = workers
    .filter((worker) => worker.status !== "revoked" && (worker.owner === me || worker.shared) && worker.projects.includes(project))
    .map((worker) => worker.name);
  return [...new Set([...usable, ...current])].sort((a, b) => a.localeCompare(b));
}

/** Why `value` cannot be sent, or null when it can. */
export function valueProblem(value: string): AgentProblem | null {
  const trimmed = value.trim();
  if (!trimmed) return "valueRequired";
  if (new TextEncoder().encode(trimmed).length > MAX_SECRET_BYTES) return "valueTooLong";
  return null;
}

function endProblem(day: string, now: number, unchanged?: string): AgentProblem | null {
  if (day && day === unchanged) return null;
  const end = endOf(day);
  if (end === undefined) return "expiresInvalid";
  if (end !== null && Date.parse(end) <= now) return "expiresPast";
  return null;
}

function wholeProblem(text: string, max: number): AgentProblem | null {
  if (!/^\d+$/.test(text.trim())) return "whole";
  const value = Number(text.trim());
  return value > max ? "range" : null;
}

function collect(entries: [AgentField, AgentProblem | null][]): AgentProblems {
  return Object.fromEntries(entries.filter(([, problem]) => problem !== null));
}

/** What is wrong with a new account, field by field; `taken` are the names of the agent's accounts. */
export function addProblems(form: AgentAccountForm, value: string, taken: readonly string[], now: number): AgentProblems {
  const name = !form.name ? "nameRequired" : !SECRET_NAME.test(form.name) ? "nameInvalid" : taken.includes(form.name) ? "nameTaken" : null;
  return collect([
    ["name", name],
    ["envVar", envVarProblem(form.envVar.trim())],
    ["value", valueProblem(value)],
    ["workers", form.workers.length === 0 ? "workersRequired" : null],
    ["expires", endProblem(form.expires, now)],
  ]);
}

/** What is wrong with a change of `account`; an end it keeps is never refused, even past. */
export function editProblems(form: AgentAccountForm, account: AgentAccount, now: number): AgentProblems {
  return collect([
    ["workers", form.workers.length === 0 ? "workersRequired" : null],
    ["expires", endProblem(form.expires, now, account.expires_at ? utcDayOf(account.expires_at) : undefined)],
    ["weight", wholeProblem(form.weight, MAX_WEIGHT)],
    ["priority", wholeProblem(form.priority, MAX_PRIORITY)],
  ]);
}

/** What is wrong with a renewal: a value, and an end that is a day ahead when one is given. */
export function renewProblems(value: string, expires: string, now: number): AgentProblems {
  return collect([
    ["value", valueProblem(value)],
    ["expires", endProblem(expires, now)],
  ]);
}

export function putBody(form: AgentAccountForm, value: string): AgentAccountWrite {
  return { env_var: form.envVar.trim(), value: value.trim(), workers: [...form.workers], expires_at: endOf(form.expires) ?? null };
}

/** The fields of the form that differ from `account`; empty when nothing changed. */
export function changeBody(form: AgentAccountForm, account: AgentAccount): AgentAccountChange {
  const body: AgentAccountChange = {};
  const weight = Number(form.weight.trim());
  const priority = Number(form.priority.trim());
  if (weight !== account.weight) body.weight = weight;
  if (priority !== account.priority) body.priority = priority;
  const workers = [...form.workers].sort();
  if (workers.join("\n") !== [...account.workers].sort().join("\n")) body.workers = workers;
  if (form.expires !== (account.expires_at ? utcDayOf(account.expires_at) : "")) body.expires_at = endOf(form.expires) ?? null;
  return body;
}

/** A renewal's body: the new value, and the new end when one is given. */
export function renewBody(value: string, expires: string): AgentAccountChange {
  return expires ? { value: value.trim(), expires_at: endOf(expires) ?? null } : { value: value.trim() };
}
