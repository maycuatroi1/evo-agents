import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

import type { Ops, Spec } from "./systems";
import { systemKeys } from "./systems-queries";

/**
 * The default system of a project and the ops of a system (docs/hub.md, Agent systems): which system is the project's
 * default (`PUT /v1/projects/{p}/default-system`, an admin), and how its Orchestrator handles the project's operations
 * events: its spec's `ops` and `limits.ops_daily_usd`, its pause, what its ops threads spent and the batches of the
 * queue. Every member reads them; an admin of the project changes them.
 */
type Schemas = components["schemas"];
export type SystemOps = Schemas["SystemOps"];
export type OpsBatch = Schemas["OpsBatch"];
export type DefaultSystem = Schemas["DefaultSystem"];
export type OpsHolding = Schemas["OpsHolding"];

/** `system_spec.OPS` and `LIMITS["ops_daily_usd"]`: default, lowest and highest of each value an admin sets. */
export const OPS_BOUNDS = {
  debounce_minutes: { fallback: 5, min: 0, max: 1440 },
  max_items: { fallback: 20, min: 1, max: 1000 },
  ops_daily_usd: { fallback: 5, min: 0.01, max: 1000 },
} as const;
export type OpsField = keyof typeof OPS_BOUNDS;

/** The batches the ops panel lists, the newest first. */
export const RECENT_BATCHES = 10;

export const opsKeys = {
  ops: (project: string, slug: string) => [...systemKeys.one(project, slug), "ops"] as const,
  batches: (project: string, slug: string) => [...systemKeys.one(project, slug), "ops", "batches"] as const,
};

export const systemOpsQuery = (api: ApiSource, project: string, slug: string) =>
  queryOptions({
    queryKey: opsKeys.ops(project, slug),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/systems/{slug}/ops", { params: { path: { project, slug } }, signal })),
    refetchInterval: 30_000,
  });

export const opsBatchesQuery = (api: ApiSource, project: string, slug: string) =>
  queryOptions({
    queryKey: opsKeys.batches(project, slug),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/systems/{slug}/ops/batches", {
          params: { path: { project, slug }, query: { limit: RECENT_BATCHES } },
          signal,
        }),
      ),
    refetchInterval: 30_000,
  });

/** Make `slug` the project's default system, and no other (an admin). */
export async function setDefaultSystem(api: ApiClient, project: string, slug: string): Promise<DefaultSystem> {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/projects/{project}/default-system", { params: { path: { project } }, body: { slug }, headers }));
}

/** Pause the ops of a system with `reason`, or resume them when `reason` is null (an admin). */
export async function pauseOps(api: ApiClient, project: string, slug: string, reason: string | null): Promise<SystemOps> {
  const headers = await csrfHeaders(api);
  const params = { path: { project, slug } };
  return call(
    reason === null
      ? api.POST("/v1/projects/{project}/systems/{slug}/ops/resume", { params, headers })
      : api.POST("/v1/projects/{project}/systems/{slug}/ops/pause", { params, body: { reason }, headers }),
  );
}

export type OpsForm = { enabled: boolean; debounce_minutes: string; max_items: string; ops_daily_usd: string };

/** The form of a system's ops as the hub reads them now, defaults filled in. */
export function opsFormOf(ops: SystemOps): OpsForm {
  return {
    enabled: ops.ops.enabled,
    debounce_minutes: String(ops.ops.debounce_minutes),
    max_items: String(ops.ops.max_items),
    ops_daily_usd: String(ops.ops_daily_usd),
  };
}

/** A value of the form read as a number within its bounds; null when it is not one (whole for all but the cap). */
export function opsValue(field: OpsField, text: string): number | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const value = Number(trimmed);
  const { min, max } = OPS_BOUNDS[field];
  if (!Number.isFinite(value) || value < min || value > max) return null;
  if (field !== "ops_daily_usd" && !Number.isInteger(value)) return null;
  return value;
}

/** The fields of the form whose value is not one the hub takes. */
export function opsProblems(form: OpsForm): OpsField[] {
  return (Object.keys(OPS_BOUNDS) as OpsField[]).filter((field) => opsValue(field, form[field]) === null);
}

/** `spec` with the form's ops and ops cap: the next revision the hub stores. Only valid forms reach it. */
export function withOps(spec: Spec, form: OpsForm): Spec {
  const ops: Ops = {
    enabled: form.enabled,
    debounce_minutes: opsValue("debounce_minutes", form.debounce_minutes) ?? OPS_BOUNDS.debounce_minutes.fallback,
    max_items: opsValue("max_items", form.max_items) ?? OPS_BOUNDS.max_items.fallback,
  };
  const cap = opsValue("ops_daily_usd", form.ops_daily_usd) ?? OPS_BOUNDS.ops_daily_usd.fallback;
  return { ...spec, ops, limits: { ...spec.limits, ops_daily_usd: cap } };
}

/** What the ops of a system need a person to know now: paused, at its cap, or neither. Off is no status. */
export function opsAlert(ops: SystemOps): "paused" | "capped" | null {
  if (!ops.default || !ops.ops.enabled) return null;
  if (ops.paused) return "paused";
  return ops.spent_usd >= ops.ops_daily_usd ? "capped" : null;
}
