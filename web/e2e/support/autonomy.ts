import { type ApiClient, call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { type Account, ADMIN_ACCOUNT, bearerClient, machineToken } from "./hub";
import { seatByApi, unitByApi } from "./org";

/**
 * Autonomy of the local stack (plan autonomy-levels), seeded through the API as the CLI writes it: a ceiling over a
 * person, their own preference, and a stop of their own runs. Each caller's people and units are its own.
 */
type PrefSet = components["schemas"]["PrefSet"];

async function apiAs(account: Account): Promise<ApiClient> {
  return bearerClient(await machineToken(account));
}

/**
 * A unit of its own (its slug starts with `prefix`) holding `login`'s primary seat, with a ceiling of `level` set by the
 * hub admin; returns its slug.
 */
export async function ceilingOver(login: string, level: "A1" | "A2" | "A3", prefix = "auto-lab"): Promise<string> {
  const slug = await unitByApi(prefix);
  await seatByApi({ unit: slug, role: "member", login, primary: true });
  const admin = await apiAs(ADMIN_ACCOUNT);
  await call(admin.PUT("/v1/org/ceilings", { body: { unit: slug, level, note: "Trial period" } }));
  return slug;
}

/** `account`'s next preference, as `evo-agents hub autonomy set` writes it. */
export async function prefByApi(account: Account, body: Omit<PrefSet, "reset"> & { reset?: boolean }): Promise<void> {
  await call((await apiAs(account)).PUT("/v1/autonomy/prefs", { body: { reset: false, ...body } }));
}

/** Stops `account`'s own runs, or lifts that stop. */
export async function stopOwnByApi(account: Account, { lift = false, reason }: { lift?: boolean; reason?: string } = {}): Promise<void> {
  const api = await apiAs(account);
  const body = { scope: "login" as const, login: account.login, reason: lift ? undefined : reason };
  await call(lift ? api.POST("/v1/autonomy/stops/lift", { body }) : api.POST("/v1/autonomy/stops", { body }));
}
