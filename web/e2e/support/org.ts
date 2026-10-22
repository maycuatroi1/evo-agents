import { expect, type Locator, type Page } from "@playwright/test";

import { type ApiClient, call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { type Account, ADMIN_ACCOUNT, bearerClient, machineToken, uniqueName } from "./hub";

/**
 * The org chart of the local stack, seeded through the API as the CLI writes it. The chart is one for the whole hub
 * and the specs run in parallel, so each test makes units of its own below the root, with unique slugs, and reads only
 * those; the people in it are made-up accounts of the fake GitHub.
 */
type SeatAdd = components["schemas"]["SeatAdd"];
type Seat = components["schemas"]["OrgSeatView"];

async function apiAs(account: Account): Promise<ApiClient> {
  return bearerClient(await machineToken(account));
}

/** A unit below `parent` (the root by default), created by the stack's hub admin; returns its slug. */
export async function unitByApi(prefix: string, parent = "root", kind: "team" | "cohort" = "team"): Promise<string> {
  const slug = uniqueName(prefix);
  const api = await apiAs(ADMIN_ACCOUNT);
  await call(api.POST("/v1/org/units", { body: { slug, parent, kind, name: `Unit ${slug}` } }));
  return slug;
}

/** A seat added by `by` (the hub admin by default). */
export async function seatByApi(body: SeatAdd, by: Account = ADMIN_ACCOUNT): Promise<Seat> {
  const api = await apiAs(by);
  return call(api.POST("/v1/org/seats", { body }));
}

/** Ends the seat `id` as the hub admin. */
export async function endSeatByApi(id: number): Promise<void> {
  const api = await apiAs(ADMIN_ACCOUNT);
  await call(api.POST("/v1/org/seats/{seat_id}/end", { params: { path: { seat_id: id } } }));
}

/** Ends every live seat `login` holds, its seat at the root too, as the hub admin: they hold no seat then. */
export async function unseatByApi(login: string): Promise<void> {
  const api = await apiAs(ADMIN_ACCOUNT);
  const chart = await call(api.GET("/v1/org"));
  for (const seat of chart.seats.filter((held) => held.login === login && held.ended_at === null)) await endSeatByApi(seat.id);
}

/** The UTC day `days` from today, as the hub's dates read. */
export function dayFromNow(days: number): string {
  return new Date(Date.now() + days * 86_400_000).toISOString().slice(0, 10);
}

/** The tree's row of unit `slug`, with its seats and the units below it. */
export function unitOf(page: Page, slug: string): Locator {
  return page.locator("#main").locator(`[data-testid="org-unit"][data-slug="${slug}"]`);
}

/** The head of unit `slug`'s row: its toggle and its own menu, without those of the units below it. */
export function unitHead(page: Page, slug: string): Locator {
  return unitOf(page, slug).locator(":scope > div").first();
}

/** The seat of `login` in unit `slug` (or a unit below it): every account holds a seat at the root too, from its
 * first sign-in, so a person shows in the tree more than once. */
export function seatOf(page: Page, login: string, slug: string): Locator {
  return unitOf(page, slug).locator(`[data-testid="org-seat"][data-login="${login}"]`);
}

/** The ids of the actions a row's menu offers, after opening it; the menu is closed again. */
export async function menuActions(page: Page, trigger: Locator): Promise<string[]> {
  await trigger.click();
  const items = page.getByRole("menuitem");
  await items.first().waitFor();
  const ids = await items.evaluateAll((nodes) => nodes.map((node) => (node.getAttribute("data-testid") ?? "").replace(/^org-action-/, "")));
  await page.keyboard.press("Escape");
  // The menu gives the focus back to its trigger as it closes: wait for that, so it lands before the caller's next step.
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(trigger).toBeFocused();
  return ids;
}
