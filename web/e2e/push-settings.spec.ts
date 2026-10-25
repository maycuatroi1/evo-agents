import type { Page } from "@playwright/test";

import { call } from "../src/lib/api/client";

import { expectNoSeriousViolations } from "./support/a11y";
import { BASE_URL } from "./support/env";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { bearerClient, machineToken } from "./support/hub";
import { open } from "./support/plans";
import { addPushDeviceByApi, fakePush, fakeSubscription } from "./support/push";
import { toast } from "./support/toast";

/**
 * The notification settings page (plan hub-mobile-phase-0, step 6) against the real API, on a hub with Web Push on
 * (e2e/hub_stack.py gives it a VAPID key). Playwright's browsers have no push service, so each spec fakes the browser's
 * PushManager before the page loads: subscribe hands back a subscription of a made-up FCM endpoint with a real P-256
 * key, so the hub keeps it as a device, and a test push is answered in the browser before it reaches the hub. The
 * service worker itself is real: the page registers /sw.js, which the proxy serves without a session. Receiving a real
 * push is checked on a real phone and laptop (step 13).
 */
test.skip(isDeployed, "adds push devices through the local stack");

const PAGE = "/settings/notifications";
const IPHONE_UA =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1";

async function devicesOf(member: Member) {
  const api = bearerClient(await machineToken(member));
  return (await call(api.GET("/v1/me/push/devices"))).devices;
}

async function settingsOf(member: Member) {
  const api = bearerClient(await machineToken(member));
  return call(api.GET("/v1/me/notification-settings"));
}

function main(page: Page) {
  return page.locator("#main");
}

test("a visitor without a session gets the service worker, and the settings page asks them to sign in", async ({ browser }) => {
  const context = await browser.newContext({ baseURL: BASE_URL });
  const page = await context.newPage();
  const worker = await page.request.get("/sw.js");
  expect(worker.status()).toBe(200);
  expect(worker.headers()["content-type"]).toMatch(/javascript/);
  const source = await worker.text();
  expect(source).toContain('addEventListener("notificationclick"');
  expect(source).toContain('addEventListener("pushsubscriptionchange"');
  await page.goto(PAGE);
  await expect(page).toHaveURL(/\/login$/);
  await context.close();
});

test("push turns on for this browser from one click in the page the user menu opens, sends a test, and turns off", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const subscription = fakeSubscription("on");
  await fakePush(page.context(), subscription);
  await open(page, "/");
  await page.getByTestId("user-menu").click();
  await page.getByTestId("user-menu-notifications").click();
  await expect(page).toHaveURL(new RegExp(`${PAGE}$`));
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Notifications");
  await expect(page.getByTestId("top-bar").getByRole("navigation")).toHaveText(/^Settings\s*\/?\s*Notifications$/);

  const card = main(page).getByTestId("push-browser");
  await expect(card).toHaveAttribute("data-state", "off");
  await expect(main(page).getByTestId("push-devices-empty")).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { e2ePermissionAsked: number }).e2ePermissionAsked)).toBe(0);
  await card.getByRole("button", { name: "Turn on push for this browser" }).click();

  await expect(card).toHaveAttribute("data-state", "on");
  // The browser asked for permission once, from the click.
  expect(await page.evaluate(() => (window as unknown as { e2ePermissionAsked: number }).e2ePermissionAsked)).toBe(1);
  await expect(toast(page, "Push is on for this browser")).toBeVisible();
  // The real service worker took over the page.
  expect(await page.evaluate(async () => (await navigator.serviceWorker.getRegistration("/"))?.active?.scriptURL)).toMatch(/\/sw\.js$/);
  const [device] = await devicesOf(me);
  expect(device).toMatchObject({ kind: "webpush", label: expect.stringMatching(/^Chrome on /) });
  const row = main(page).getByTestId("push-device");
  await expect(row).toHaveCount(1);
  await expect(row.getByTestId("push-device-here")).toHaveText("This browser");

  // A test push: what the push service said comes back as a toast.
  await page.route(`**/v1/me/push/devices/${device.id}/test`, (route) => route.fulfill({ json: { sent: true, removed: false, error: null } }));
  await card.getByTestId("push-browser-test").click();
  await expect(toast(page, `Test push sent to ${device.label}`)).toBeVisible();
  await page.unroute(`**/v1/me/push/devices/${device.id}/test`);
  await page.route(`**/v1/me/push/devices/${device.id}/test`, (route) =>
    route.fulfill({ json: { sent: false, removed: false, error: "HTTPStatusError: 400 Bad Request from fcm.googleapis.com" } }),
  );
  await row.getByTestId("push-device-test").click();
  await expect(toast(page, "The test push did not go")).toContainText("400 Bad Request");

  // Still on after a reload: the subscription and the device agree.
  await page.reload();
  await expect(main(page).getByTestId("push-browser")).toHaveAttribute("data-state", "on");

  await main(page).getByTestId("push-browser-off").click();
  await expect(main(page).getByTestId("push-browser")).toHaveAttribute("data-state", "off");
  await expect(toast(page, "Push is off for this browser")).toBeVisible();
  expect(await devicesOf(me)).toEqual([]);
});

test("a device added elsewhere is listed and removed once confirmed; one gone from its push service is removed by a test", async ({ page, member }) => {
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  const laptop = await addPushDeviceByApi(me, "Firefox on Linux");
  const phone = await addPushDeviceByApi(me, "Safari on iPhone");
  await open(page, PAGE);
  const rows = main(page).getByTestId("push-device");
  await expect(rows).toHaveCount(2);
  await expect(rows.getByTestId("push-device-name")).toHaveText(["Safari on iPhone", "Firefox on Linux"]);
  await expect(rows.getByTestId("push-device-here")).toHaveCount(0);
  await expect(main(page).getByTestId("push-devices-count")).toHaveText("2 devices");

  await page.route(`**/v1/me/push/devices/${phone.id}/test`, (route) => route.fulfill({ json: { sent: false, removed: true, error: null } }));
  await rows.first().getByRole("button", { name: "Send a test push to Safari on iPhone" }).click();
  await expect(toast(page, "Safari on iPhone no longer gets push")).toBeVisible();

  const laptopRow = main(page).locator(`[data-testid="push-device"][data-device-id="${laptop.id}"]`);
  await laptopRow.getByRole("button", { name: "Remove Firefox on Linux" }).click();
  const dialog = page.getByTestId("push-device-remove-dialog");
  await expect(dialog).toContainText("Remove Firefox on Linux?");
  await dialog.getByRole("button", { name: "Keep it" }).click();
  await expect(dialog).toBeHidden();
  await laptopRow.getByTestId("push-device-remove").click();
  await page.getByTestId("push-device-remove-dialog-confirm").click();
  await expect(toast(page, "Firefox on Linux removed")).toBeVisible();
  await expect(laptopRow).toHaveCount(0);
  expect((await devicesOf(me)).map((device) => device.id)).toEqual([phone.id]);
});

test("notifications blocked for the site are said, with no button that cannot work", async ({ page, member }) => {
  await member();
  await fakePush(page.context(), fakeSubscription("blocked"), { permission: "denied" });
  await open(page, PAGE);
  await expect(main(page).getByTestId("push-browser")).toHaveAttribute("data-state", "denied");
  await expect(main(page).getByTestId("push-browser-title")).toHaveText("Notifications are blocked for this site");
  await expect(main(page).getByTestId("push-browser-on")).toHaveCount(0);
});

test("tiers, quiet hours, the summary's time and the lock screen are saved and read back", async ({ page, member }) => {
  const me = await member();
  await open(page, PAGE);
  const tiers = main(page).getByTestId("tiers-card");
  const runFailed = tiers.locator('[data-testid="tier"][data-kind="run_failed"]');
  await expect(runFailed.getByTestId("tier-select")).toHaveValue("default");
  await runFailed.getByTestId("tier-select").selectOption("off");
  await expect(toast(page, "Run failed: Inbox only")).toBeVisible();
  await expect(runFailed).toHaveAttribute("data-chosen", "true");
  await tiers.getByRole("button", { name: /more kinds/ }).click();
  const brief = tiers.locator('[data-testid="tier"][data-kind="curator_brief"]');
  await brief.getByTestId("tier-select").selectOption("interrupt");
  await expect(toast(page, "Morning brief: Push now")).toBeVisible();

  const form = main(page).getByTestId("schedule-form");
  // The browser's zone is the hub's here (Chromium names it Asia/Saigon, the hub Asia/Ho_Chi_Minh): nothing to save yet.
  await expect(form.getByTestId("schedule-time-zone")).toHaveValue("Asia/Ho_Chi_Minh");
  await expect(form.getByTestId("schedule-time-zone-hint")).toHaveText("An IANA name, such as Asia/Ho_Chi_Minh.");
  await expect(form.getByTestId("schedule-save")).toBeDisabled();
  await form.getByRole("switch", { name: "Quiet hours" }).click();
  await form.getByTestId("schedule-end").fill("22:00");
  await form.getByTestId("schedule-save").click();
  await expect(form.getByTestId("schedule-problem")).toHaveText("Quiet hours end at another time than they start.");
  await form.getByTestId("schedule-end").fill("06:30");
  await form.getByTestId("schedule-time-zone").fill("Asia/Tokyo");
  await form.getByTestId("schedule-digest").fill("08:15");
  await form.getByTestId("schedule-save").click();
  await expect(toast(page, "Quiet hours and summary saved")).toBeVisible();

  await main(page).getByTestId("hide-details").getByRole("switch").click();
  await expect(toast(page, "Details hidden on the lock screen")).toBeVisible();

  const saved = await settingsOf(me);
  expect(saved).toMatchObject({ quiet_hours: { start: "22:00", end: "06:30" }, time_zone: "Asia/Tokyo", time_zone_chosen: true, digest_at: "08:15", hide_details: true });
  expect(saved.tiers.find((tier) => tier.kind === "run_failed")).toEqual({ kind: "run_failed", tier: "off", chosen: true });
  expect(saved.tiers.find((tier) => tier.kind === "curator_brief")).toEqual({ kind: "curator_brief", tier: "interrupt", chosen: true });

  await page.reload();
  await expect(main(page).getByTestId("schedule-time-zone")).toHaveValue("Asia/Tokyo");
  await expect(main(page).getByTestId("schedule-start")).toHaveValue("22:00");
  await expect(main(page).getByTestId("hide-details").getByRole("switch")).toBeChecked();
  // A kind of the fold that was chosen opens the fold by itself.
  await expect(main(page).getByTestId("tiers-rest")).toBeVisible();

  // Back to the hub's default for one kind, and quiet hours off.
  await main(page).locator('[data-testid="tier"][data-kind="run_failed"]').getByTestId("tier-select").selectOption("default");
  await expect(toast(page, "Run failed: Default: Push now")).toBeVisible();
  await main(page).getByTestId("schedule-form").getByRole("switch", { name: "Quiet hours" }).click();
  await main(page).getByTestId("schedule-save").click();
  await expect(toast(page, "Quiet hours and summary saved")).toBeVisible();
  const reset = await settingsOf(me);
  expect(reset.quiet_hours).toBeNull();
  expect(reset.tiers.find((tier) => tier.kind === "run_failed")).toEqual({ kind: "run_failed", tier: "interrupt", chosen: false });
});

test.describe("on an iPhone", () => {
  test.use({ viewport: { width: 375, height: 812 }, userAgent: IPHONE_UA, hasTouch: true, isMobile: true });

  test("Safari outside a Home Screen app is shown how to add the hub there, and the page fits the phone", async ({ page, member }) => {
    await member();
    // Safari gives push only to a web app opened from the Home Screen: outside one it has no PushManager.
    await page.context().addInitScript(() => {
      Reflect.deleteProperty(window, "PushManager");
    });
    await open(page, PAGE);
    const card = main(page).getByTestId("push-browser");
    await expect(card).toHaveAttribute("data-state", "home-screen");
    await expect(main(page).getByRole("list", { name: "How to add the hub to the Home Screen" }).getByRole("listitem")).toHaveCount(3);
    await expect(main(page).getByTestId("push-browser-on")).toHaveCount(0);
    // One column, this browser first, then the devices and the quiet hours, the long list of kinds after them.
    const order = ["push-browser-card", "push-devices-card", "schedule-card", "lock-screen-card", "tiers-card", "notices-card"];
    const tops = await Promise.all(order.map(async (id) => (await main(page).getByTestId(id).boundingBox())!.y));
    expect(tops).toEqual([...tops].sort((a, b) => a - b));
    for (const select of await main(page).getByTestId("tier-select").all()) {
      if (await select.isVisible()) expect((await select.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
    await expectNoSeriousViolations(page, "notification settings on an iPhone");
  });
});
