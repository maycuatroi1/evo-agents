import type { components } from "@/lib/api/schema";

/**
 * Agent profiles of a project (docs/hub.md, Agent profiles; `evo_agents.hub.profiles` of the API): who an agent is
 * (handle, name, colour, avatar, description, instructions) and what its runs may use on eight axes, each every value
 * (`all`) or an allowlist. The project's default agent, @<project>, keeps every permission and no instructions; a run
 * dispatched without naming an agent goes under it. The hub enforces the rules again on every dispatch and claim: what
 * this module checks in the browser only spares a refusal.
 */
type Schemas = components["schemas"];
export type Agent = Schemas["Agent"];
export type AgentAxisRule = Schemas["AgentAxis"];
export type AgentPermissions = Schemas["AgentPermissions"];
export type AgentCreate = Schemas["AgentCreate"];
export type AgentEdit = Schemas["AgentEdit"];
export type AgentVersion = Schemas["AgentVersion"];

/** `profiles.AXES`, in the API's order. */
export const AXES = ["credential", "tool", "skill", "worker", "project", "model", "runtime", "environment"] as const satisfies readonly (keyof AgentPermissions)[];
export type Axis = (typeof AXES)[number];

export const LABEL_PREFIX = "label:";
/** `author.AUTHOR_SKILL`: the skill an author run (New plan, Revise with agent) writes its plan with. */
export const AUTHOR_SKILL = "create-exec-plan";

/** The rule of one axis; an axis the API left out allows every value. */
export function axisRule(permissions: AgentPermissions | null | undefined, axis: Axis): AgentAxisRule {
  return permissions?.[axis] ?? { mode: "all" };
}

/** What an allowlist names on `axis`; null when the axis allows every value. */
export function allowed(permissions: AgentPermissions | null | undefined, axis: Axis): string[] | null {
  const rule = axisRule(permissions, axis);
  return rule.mode === "allowlist" ? (rule.allow ?? []) : null;
}

const folded = (axis: Axis) => axis === "skill" || axis === "worker";

/** Whether `value` passes `axis`: skill and worker names compare ignoring case, as the hub compares them. */
export function allows(permissions: AgentPermissions | null | undefined, axis: Axis, value: string): boolean {
  const allow = allowed(permissions, axis);
  if (allow === null) return true;
  if (!folded(axis)) return allow.includes(value);
  const wanted = value.toLowerCase();
  return allow.some((item) => item.toLowerCase() === wanted);
}

/** Whether the worker axis lets worker `name` claim a run: by its name, or by one of its labels as `label:<label>`. */
export function allowsWorker(permissions: AgentPermissions | null | undefined, name: string, labels: readonly string[]): boolean {
  const allow = allowed(permissions, "worker");
  if (allow === null) return true;
  const names = new Set(allow.map((item) => item.toLowerCase()));
  return names.has(name.toLowerCase()) || labels.some((label) => names.has(`${LABEL_PREFIX}${label.toLowerCase()}`));
}

/** The agents a run can go under, the project's default first, then by handle; deleted ones take no new run. */
export function usableAgents(agents: readonly Agent[]): Agent[] {
  return agents
    .filter((agent) => agent.deleted_at === null)
    .sort((a, b) => Number(b.is_default) - Number(a.is_default) || a.handle.localeCompare(b.handle));
}

// Dispatching under an agent: what the browser can tell before the hub refuses.

/** Why the agent refuses what a dispatch asks; the hub answers 422 with the same reason. */
export type AgentProblem =
  | { kind: "deleted" }
  | { kind: "runtime"; runtime: string; allow: string[] }
  | { kind: "model"; model: string; allow: string[] }
  | { kind: "workers"; allow: string[] }
  | { kind: "daemon" }
  | { kind: "skill"; skill: string };

/** `runs.AGENT_PROFILES`: the heartbeat feature of a daemon whose adapters apply an agent's tools, skills and model. */
export const AGENT_PROFILES_FEATURE = "agent_profiles";

/**
 * `profiles.needs_worker`: whether only a worker whose daemon says it applies agent profiles may claim the agent's runs,
 * since it limits tools, skills or models, or adds instructions, which the hub cannot apply itself.
 */
export function needsProfileDaemon(agent: Agent): boolean {
  return (["tool", "skill", "model"] as const).some((axis) => allowed(agent.permissions, axis) !== null) || agent.instructions !== "";
}

type WorkerLike = { name: string; labels: string[]; features?: string[] };

/**
 * The workers of the visitor's that may claim the agent's runs, in their order: those its worker axis names, and when
 * it needs one, only those whose daemon applies agent profiles; the hub's claim keeps to the same.
 */
export function agentWorkers<W extends WorkerLike>(workers: readonly W[], agent: Agent | null): W[] {
  if (!agent) return [...workers];
  const daemon = needsProfileDaemon(agent);
  return workers.filter(
    (worker) => allowsWorker(agent.permissions, worker.name, worker.labels) && (!daemon || (worker.features ?? []).includes(AGENT_PROFILES_FEATURE)),
  );
}

/**
 * What the agent refuses of a dispatch: a runtime it leaves out (or any runtime, when it allows none), a model it leaves
 * out, a list of the visitor's workers none of which it names, or none of which runs a daemon that applies agent
 * profiles when it needs one (only when they were read, `workers` not null and not empty), and for an author run the
 * skill it writes its plan with.
 */
export function agentProblems(
  agent: Agent | null,
  asked: { runtime: string; model?: string | null; workers?: readonly WorkerLike[] | null; skill?: string | null },
): AgentProblem[] {
  if (!agent) return [];
  const problems: AgentProblem[] = [];
  if (agent.deleted_at !== null) problems.push({ kind: "deleted" });
  const runtimes = allowed(agent.permissions, "runtime");
  if (runtimes !== null && (runtimes.length === 0 || (asked.runtime !== "any" && !runtimes.includes(asked.runtime)))) {
    problems.push({ kind: "runtime", runtime: asked.runtime, allow: runtimes });
  }
  const models = allowed(agent.permissions, "model");
  if (models !== null && asked.model && !models.includes(asked.model)) problems.push({ kind: "model", model: asked.model, allow: models });
  const workers = allowed(agent.permissions, "worker");
  if (asked.workers && asked.workers.length > 0 && agentWorkers(asked.workers, agent).length === 0) {
    const named = asked.workers.some((worker) => allowsWorker(agent.permissions, worker.name, worker.labels));
    problems.push(named || workers === null ? { kind: "daemon" } : { kind: "workers", allow: workers });
  }
  if (asked.skill && !allows(agent.permissions, "skill", asked.skill)) problems.push({ kind: "skill", skill: asked.skill });
  return problems;
}

/** The models to offer for the agent: those it allows when it limits them, else what the workers list. */
export function agentModels(agent: Agent | null, suggestions: string[]): string[] {
  const allow = allowed(agent?.permissions, "model");
  return allow === null ? suggestions : [...allow];
}

/** The dispatch body's `profile`: none for the project's default, so a body reads as it did before agents. */
export function profileField(agent: Agent | null): { profile?: string } {
  return agent && !agent.is_default ? { profile: agent.handle } : {};
}
