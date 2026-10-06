import { randomBytes } from "node:crypto";

import type { Page } from "@playwright/test";

import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { API_URL } from "./env";
import { type Account, bearerClient, machineToken } from "./hub";

/**
 * Secrets and the leases of runs (docs/credentials.md) through the real API: a member's secret written with their
 * machine token, as `evo-agents hub secret set` does, and what the hub answers about it. The values are random and
 * shaped like no real token, so a spec can look for them in everything the browser was sent.
 */
export type Secret = components["schemas"]["Secret"];
export type SecretWrite = components["schemas"]["SecretWrite"];
export type RunLease = components["schemas"]["RunLease"];

/** A value no page may ever show. */
export function secretValue(label = "value"): string {
  return `e2e-${label}-${randomBytes(12).toString("hex")}`;
}

/** `account`'s secret `name`, created or replaced whole through the API. */
export async function putSecretByApi(account: Account, name: string, body: SecretWrite) {
  const api = bearerClient(await machineToken(account));
  return call(api.PUT("/v1/secrets/{name}", { params: { path: { name } }, body }));
}

/** `account`'s secrets as the API lists them. */
export async function secretsOf(account: Account): Promise<Secret[]> {
  const api = bearerClient(await machineToken(account));
  return call(api.GET("/v1/secrets"));
}

/** The leases of a run as `account` asks for them: the status, and the leases when the hub answered 200. */
export async function runCredentialsOf(account: Account, project: string, runId: number): Promise<{ status: number; body: unknown }> {
  const token = await machineToken(account);
  const response = await fetch(`${API_URL}/v1/projects/${project}/runs/${runId}/credentials`, { headers: { authorization: `Bearer ${token}` } });
  return { status: response.status, body: await response.json() };
}

/**
 * Everything the page receives from now on, as text, to look for a value in: documents, data and scripts alike, each
 * read as soon as it has arrived whole. A run's live stream never ends while the run is active, so it is left out; its
 * events are read through the API. A body the browser dropped before it could be read (a page navigated away from)
 * counts as empty.
 */
export function recordResponses(page: Page): () => Promise<string[]> {
  const bodies: Promise<string>[] = [];
  page.on("requestfinished", (request) => {
    bodies.push(
      request.response().then(async (response) => {
        if (!response || (response.headers()["content-type"] ?? "").includes("text/event-stream")) return "";
        const timeout = new Promise<string>((resolve) => setTimeout(() => resolve(""), 5_000));
        return Promise.race([response.text().catch(() => ""), timeout]);
      }),
    );
  });
  return () => Promise.all(bodies);
}
