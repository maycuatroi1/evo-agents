import { randomBytes } from "node:crypto";

import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { type Account, bearerClient, machineToken } from "./hub";

/**
 * Provider accounts (docs/credentials.md, section "Provider accounts") through the real API. The hub of the specs
 * (e2e/hub_serve.py) calls no real endpoint: every host is public, and its fake endpoint lists FAKE_MODELS to a key
 * that starts with GOOD_KEY and refuses any other. Keys are random, so a spec can look for them in what the browser got.
 */
export type ProviderAccountWrite = components["schemas"]["ProviderAccountWrite"];

export const GOOD_KEY = "e2e-good-"; // e2e/hub_serve.py
export const FAKE_MODELS = ["fake-large", "fake-small", "fake-coder"];

/** A key the fake endpoint takes, or with `good` false one it refuses. */
export function providerKey(good = true): string {
  return `${good ? GOOD_KEY : "e2e-bad-"}${randomBytes(12).toString("hex")}`;
}

/** `account`'s provider account `name`, created or replaced whole through the API. */
export async function putProviderByApi(account: Account, name: string, body: ProviderAccountWrite) {
  const api = bearerClient(await machineToken(account));
  return call(api.PUT("/v1/provider-accounts/{name}", { params: { path: { name } }, body }));
}

/** `account`'s provider accounts as the API lists them. */
export async function providersOf(account: Account) {
  const api = bearerClient(await machineToken(account));
  return call(api.GET("/v1/provider-accounts"));
}
