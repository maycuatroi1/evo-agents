import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { ApiSource } from "@/lib/queries";
import { isCoreRuntime, type RequestedRuntime } from "@/lib/runtimes";
import type { Limits, Spec, SystemSummary } from "@/lib/systems";

import type { SystemDispatched } from "./plan-run-outcome";
import { DEFAULT_PLAN_TIMEOUT, PLAN_TIMEOUT_CHOICES, type PlanTimeout, type RunMode } from "./queries";
import type { Schemas } from "./run-types";

/**
 * Where Run plan sends a plan (docs/hub.md): a plan run on a worker (POST .../plan-runs), or the plan handed to the
 * entry agent of one of the project's agent systems (POST .../plans/{plan_id}/system-dispatch), whose trace then holds
 * the plan's steps. The hub remembers each member's last choice per project (GET and PUT .../plan-run-choice) so the
 * dialog opens on it; the account and Run as me are never remembered, and the hub refuses them (422).
 */
export type PlanRunChoice = Schemas["PlanRunChoice"];
export type PlanRunChoiceIn = Schemas["PlanRunChoiceIn"];
export type PlanTrace = Schemas["PlanTrace"];
export type PlanTarget = PlanRunChoice["target"];

export { isSystemDispatched, type PlanInSystem, type PlanRunOutcome, type SystemDispatched } from "./plan-run-outcome";

/** `system_spec.LIMITS` defaults: a spec saved by the hub always holds them, an older one may not. */
export const DEFAULT_LIMITS = { trace_usd: 2, trace_runs: 12, daily_usd: 5 } satisfies Limits;

/** The limits of a trace the dialog shows: a spec's own, over `DEFAULT_LIMITS` for the keys it does not name. */
export type TraceLimits = Limits & typeof DEFAULT_LIMITS;

/** About how many runs of a trace one step of a plan takes: the entry agent's turn and the agent it hands the step to. */
export const RUNS_PER_STEP = 2;

export const planRunChoiceKey = (project: string) => ["projects", project, "plan-run-choice"] as const;

/** The caller's remembered choice in the project, with the fields the hub put back to their default. */
export const planRunChoiceQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: planRunChoiceKey(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/plan-run-choice", { params: { path: { project } }, signal })),
    staleTime: 0,
    retry: false, // an older hub answers 404: the dialog opens on its defaults
  });

export async function savePlanRunChoice(api: ApiClient, project: string, body: PlanRunChoiceIn): Promise<PlanRunChoice> {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/projects/{project}/plan-run-choice", { params: { path: { project } }, body, headers }));
}

/** Hand the plan to the entry agent of system `system` (409 while the plan has an active run or trace, or no pending step). */
export async function dispatchPlanToSystem(api: ApiClient, project: string, planId: string, system: string): Promise<SystemDispatched> {
  const headers = await csrfHeaders(api);
  return call(
    api.POST("/v1/projects/{project}/plans/{plan_id}/system-dispatch", {
      params: { path: { project, plan_id: planId } },
      body: { system },
      headers,
    }),
  );
}

/**
 * The form's fields as the dialog starts them: the remembered choice, else the dialog's own defaults (a member who saved
 * none gets the hub's with `saved` false, whose timeout is the API's 4 hours, not the dialog's 24).
 */
export type ChoiceFields = {
  target: PlanTarget;
  system: string | null;
  agent: string | null;
  runtime: RequestedRuntime;
  model: string;
  mode: RunMode;
  timeout: PlanTimeout;
  perStep: boolean;
};

export const DEFAULT_FIELDS: ChoiceFields = {
  target: "worker",
  system: null,
  agent: null,
  runtime: "any",
  model: "",
  mode: "headless",
  timeout: DEFAULT_PLAN_TIMEOUT,
  perStep: false,
};

export function fieldsOf(choice: PlanRunChoice | undefined): ChoiceFields {
  if (!choice?.saved) return DEFAULT_FIELDS;
  const runtime = choice.runtime === "any" || isCoreRuntime(choice.runtime) ? choice.runtime : "any";
  const timeout = (PLAN_TIMEOUT_CHOICES as readonly number[]).includes(choice.timeout_h) ? choice.timeout_h : DEFAULT_FIELDS.timeout;
  return {
    target: choice.target === "system" && choice.system ? "system" : "worker",
    system: choice.system ?? null,
    agent: choice.agent ?? null,
    runtime,
    model: choice.model ?? "",
    mode: choice.mode,
    timeout,
    perStep: choice.session_per_step,
  };
}

/** The choice to remember after a Run: the target, and the worker's fields as they stand, kept for the next time. */
export function choiceBody(fields: ChoiceFields, model: string | null): PlanRunChoiceIn {
  const system = fields.target === "system" ? fields.system : null;
  return {
    target: system ? "system" : "worker",
    system,
    agent: fields.agent,
    runtime: fields.runtime,
    model,
    mode: fields.mode,
    timeout_h: fields.timeout,
    session_per_step: fields.perStep,
  };
}

/** A system the dialog offers: one with an entry agent, with its purpose and limits once its spec is read. */
export type SystemOption = {
  slug: string;
  name: string;
  entry: string;
  purpose: string | null;
  limits: TraceLimits | null;
};

export function systemOptions(systems: readonly SystemSummary[], specs: ReadonlyMap<string, Spec>): SystemOption[] {
  return systems
    .filter((system) => system.entry_agent)
    .map((system) => {
      const spec = specs.get(system.slug);
      return {
        slug: system.slug,
        name: system.name || system.slug,
        entry: system.entry_agent ?? "",
        purpose: spec ? spec.purpose.trim() || null : null,
        limits: spec ? { ...DEFAULT_LIMITS, ...spec.limits } : null,
      };
    });
}

/** The runs a trace may need for `pending` steps when that is more than its limit allows; null when it fits. */
export function overLimit(pending: number, limits: TraceLimits | null): { runs: number; limit: number } | null {
  if (!limits) return null;
  const runs = pending * RUNS_PER_STEP;
  return runs > limits.trace_runs ? { runs, limit: limits.trace_runs } : null;
}
