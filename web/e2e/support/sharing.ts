import { expect } from "@playwright/test";

import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { API_URL } from "./env";
import { type Account, bearerClient, machineToken, newAccount } from "./hub";
import type { LiveWorker } from "./runs";
import { PROTOCOL_HEADER } from "./workers";

/**
 * Sharing (plan credential-sharing) through the real API: another member to share with, a share written as
 * `evo-agents hub secret share` writes it, and a doctor report as the daemon sends it. The people are made-up accounts
 * of the fake GitHub.
 */
export type ShareWrite = components["schemas"]["ShareWrite"];
export type DoctorFinding = { code: string; severity: "high" | "medium" | "low"; detail: string };

/** A member of the hub besides the spec's own, signed up through the API, so a share can name them. */
export async function otherMember(prefix: string): Promise<Account> {
  const account = newAccount(prefix);
  await machineToken(account);
  return account;
}

/** `owner`'s secret `name` shared as `body` says, the terms not acknowledged unless it says so; returns the share. */
export async function shareByApi(owner: Account, name: string, body: Omit<ShareWrite, "terms_ack"> & { terms_ack?: boolean }) {
  const api = bearerClient(await machineToken(owner));
  return call(api.POST("/v1/secrets/{name}/shares", { params: { path: { name } }, body: { terms_ack: false, ...body } }));
}

/** The daemon of `live` sending its doctor's findings. */
export async function doctorReport(live: LiveWorker, findings: DoctorFinding[]): Promise<void> {
  const response = await fetch(`${API_URL}/v1/worker/doctor`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${live.token}`, ...PROTOCOL_HEADER },
    body: JSON.stringify({ findings }),
  });
  expect(response.status).toBe(200);
}

/** The worker of `live` narrowed to unit `slug` by its owner `owner`. */
export async function narrowByApi(owner: Account, live: LiveWorker, slug: string): Promise<void> {
  const api = bearerClient(await machineToken(owner));
  await call(api.PUT("/v1/org/workers/{worker_id}/units/{slug}", { params: { path: { worker_id: live.worker.id, slug } } }));
}

export const CLEAN_REPORT: DoctorFinding[] = [{ code: "disk-low", severity: "medium", detail: "12 GB free" }];
export const HIGH_REPORT: DoctorFinding[] = [{ code: "token-in-env", severity: "high", detail: "a token is in the shell profile" }];
