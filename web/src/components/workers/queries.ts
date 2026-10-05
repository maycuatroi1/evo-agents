import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

import { recordHeartbeats } from "./heartbeats";

/**
 * What the workers pages read and write (docs/workers.md). A member sees their own workers; a hub admin sees every
 * worker and may drain or revoke any of them, but only the owner undrains one. Another member's worker answers 404,
 * the same as an id the hub never gave out.
 */
type Schemas = components["schemas"];
export type Worker = Schemas["Worker"];
export type Pairing = Schemas["Pairing"];
export type PairingState = Schemas["PairingState"];
export type PairingRequest = Schemas["PairingRequest"];

/** The list and a worker's page poll the hub this often, so a heartbeat or a drain shows within seconds. */
export const WORKERS_REFRESH_MS = 10_000;
/** A pairing is asked about this often while its code waits for a machine. */
export const PAIRING_POLL_MS = 2_000;

/** `workers.WORKER_NAME` and `workers.LABEL` of the API. */
export const WORKER_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;
export const LABEL = /^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/;
export const MAX_LABELS = 16;
export const MIN_SLOTS = 1;
export const MAX_SLOTS = 8;

export const workerKeys = {
  all: ["workers"] as const,
  list: ["workers", "list"] as const,
  one: (id: number) => ["workers", "one", id] as const,
  pairing: (id: number) => ["workers", "pairing", id] as const,
};

/**
 * Every worker the visitor may see, revoked ones included: the page filters them, and counts each status. Each
 * answer read in the browser is also an observation of the workers' heartbeats (heartbeats.ts).
 */
export const workersQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: workerKeys.list,
    queryFn: async ({ signal }) => {
      const workers = await call(api().GET("/v1/workers", { params: { query: { revoked: true } }, signal }));
      recordHeartbeats(workers, Date.now());
      return workers;
    },
    refetchInterval: WORKERS_REFRESH_MS,
  });

export const workerQuery = (api: ApiSource, id: number) =>
  queryOptions({
    queryKey: workerKeys.one(id),
    queryFn: async ({ signal }) => {
      const worker = await call(api().GET("/v1/workers/{worker_id}", { params: { path: { worker_id: id } }, signal }));
      recordHeartbeats([worker], Date.now());
      return worker;
    },
    refetchInterval: WORKERS_REFRESH_MS,
  });

/** A pairing, asked about until a machine joins with its code, or the code expires or locks. */
export const pairingQuery = (api: ApiSource, id: number) =>
  queryOptions({
    queryKey: workerKeys.pairing(id),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/workers/pairings/{pairing_id}", { params: { path: { pairing_id: id } }, signal })),
    refetchInterval: (query) => (query.state.data && query.state.data.status !== "waiting" ? false : PAIRING_POLL_MS),
    refetchIntervalInBackground: true, // the machine may join while the person is in a terminal window
  });

// Writes, each with the session's X-Evo-CSRF header.

export async function createPairing(api: ApiClient, body: PairingRequest): Promise<Pairing> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/workers/pairings", { body, headers }));
}

export type WorkerAction = "drain" | "undrain" | "revoke";

export async function changeWorker(api: ApiClient, id: number, action: WorkerAction): Promise<Worker> {
  const headers = await csrfHeaders(api);
  const params = { path: { worker_id: id } };
  if (action === "drain") return call(api.POST("/v1/workers/{worker_id}/drain", { params, headers }));
  if (action === "undrain") return call(api.POST("/v1/workers/{worker_id}/undrain", { params, headers }));
  return call(api.POST("/v1/workers/{worker_id}/revoke", { params, headers }));
}

/** A worker id as the API accepts it: a positive bigint written in digits; null for anything else. */
export function parseWorkerId(text: string): number | null {
  if (!/^[1-9][0-9]{0,18}$/.test(text)) return null;
  const id = Number(text);
  return Number.isSafeInteger(id) ? id : null;
}

export const WORKERS_HREF = "/workers" as Route;

export function workerHref(id: number): Route {
  return `/workers/${id}` as Route;
}
