import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";

import type { Worker } from "./queries";

export type WorkerUpdate = components["schemas"]["WorkerUpdate"];

/**
 * Change the worker's slots, labels, name, plugins or version lock: each field the body gives, at least one. Only its
 * owner may; the daemon takes the new values from the answer to its next heartbeat. It lives here, not in queries.ts,
 * because queries.ts sits in a chunk every route loads first, and only the owner's Settings card writes these.
 */
export async function updateWorker(api: ApiClient, id: number, body: WorkerUpdate): Promise<Worker> {
  const headers = await csrfHeaders(api);
  return call(api.PATCH("/v1/workers/{worker_id}", { params: { path: { worker_id: id } }, body, headers }));
}

/**
 * Ask the worker's daemon to update itself to nightly now (`ask`), or withdraw that. The rights are those of drain; the
 * page offers it to the owner only. 409 says why the hub refused: a locked version, a daemon too old, or nightly already.
 */
export async function setUpdateNow(api: ApiClient, id: number, ask: boolean): Promise<Worker> {
  const headers = await csrfHeaders(api);
  const params = { path: { worker_id: id } };
  if (ask) return call(api.POST("/v1/workers/{worker_id}/update-now", { params, headers }));
  return call(api.POST("/v1/workers/{worker_id}/update-now/cancel", { params, headers }));
}

/**
 * Ask the worker's daemon to install Chromium (`ask`), as `evo-agents worker browser install` does, or withdraw that. The
 * rights are those of drain; the page offers it to the owner only. 409 says why the hub refused: a daemon too old, or
 * Chromium there already.
 */
export async function setBrowserInstall(api: ApiClient, id: number, ask: boolean): Promise<Worker> {
  const headers = await csrfHeaders(api);
  const params = { path: { worker_id: id } };
  if (ask) return call(api.POST("/v1/workers/{worker_id}/browser-install", { params, headers }));
  return call(api.POST("/v1/workers/{worker_id}/browser-install/cancel", { params, headers }));
}
