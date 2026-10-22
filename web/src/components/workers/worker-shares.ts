import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * Whom the visitor's workers are shared with, narrowed to units of the org chart (docs/workers.md, section "Shared
 * workers"; the hub's `evo_agents.hub.server.worker_shares`), and what the newest report of each one's doctor found.
 * It lives here, not in queries.ts, which every route loads first, since only the owner's Sharing card uses it.
 */
export type WorkerShares = components["schemas"]["WorkerShares"];

/** `runs.DOCTOR_FRESH_SECONDS` of the hub: a doctor report older than this does not let a worker be narrowed. */
export const DOCTOR_FRESH_MS = 48 * 3600 * 1000;

/** Under workerKeys.all, so every worker write reloads it too. */
export const workerSharesKey = ["workers", "shares"] as const;

export const workerSharesQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: workerSharesKey,
    queryFn: ({ signal }) => call(api().GET("/v1/org/workers", { signal })),
    refetchInterval: 60_000,
  });

/** What the newest doctor report says of narrowing the worker to a unit, as the hub decides it (`share_refusal`). */
export type DoctorVerdict = { state: "none" } | { state: "stale"; at: string } | { state: "high"; count: number } | { state: "ok" };

export function doctorVerdict(entry: Pick<WorkerShares, "doctor_at" | "doctor_high">, now: number | null): DoctorVerdict {
  if (!entry.doctor_at) return { state: "none" };
  if (now !== null && now - new Date(entry.doctor_at).getTime() > DOCTOR_FRESH_MS) return { state: "stale", at: entry.doctor_at };
  if (entry.doctor_high) return { state: "high", count: entry.doctor_high };
  return { state: "ok" };
}

// Writes, each with the session's X-Evo-CSRF header.

/** Narrow the worker to unit `slug`, sharing it when it was not; 409 with the reason when its doctor says no. */
export async function addWorkerUnit(api: ApiClient, id: number, slug: string): Promise<WorkerShares> {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/org/workers/{worker_id}/units/{slug}", { params: { path: { worker_id: id, slug } }, headers }));
}

/** Take unit `slug` back from the worker; with its last unit, sharing ends too. */
export async function removeWorkerUnit(api: ApiClient, id: number, slug: string): Promise<WorkerShares> {
  const headers = await csrfHeaders(api);
  return call(api.DELETE("/v1/org/workers/{worker_id}/units/{slug}", { params: { path: { worker_id: id, slug } }, headers }));
}
