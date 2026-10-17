import type { Page } from "@playwright/test";

import { BASE_URL } from "./support/env";
import { expect, isDeployed, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, uniqueName } from "./support/hub";
import { clearHubSettings, PNG, setFeatures } from "./support/settings";
import { toast } from "./support/toast";

/**
 * Criterion a11 of plan hub-agent-plugins in the browser, against the real API: a hub admin brands the hub (a colour
 * under its contrast is held back; the name, the colours, the logo and the favicon reach the sidebar, the page and the
 * sign-in page; a rollback copies an older version back) and turns features off for a project or for the whole hub
 * (the entry leaves the menu, the page and the API answer 404, and turning it back on or rolling back restores them).
 *
 * The branding and the hub's own features show on every page, so this spec runs alone, before every other Chromium
 * spec (the hub-settings project of playwright.config.ts), its tests one after the other, and leaves the hub with
 * nothing set.
 */
test.skip(isDeployed, "changes the branding and the features of the whole hub");
test.describe.configure({ mode: "serial" });
test.beforeAll(clearHubSettings);
test.afterAll(clearHubSettings);

async function brandVariable(page: Page): Promise<string> {
  return page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--brand").trim());
}

test("a hub admin brands the hub, and the sidebar, the page and the sign-in page follow", async ({ page, signInAs, browser }) => {
  await signInAs(ADMIN_ACCOUNT);
  await page.goto("/admin/branding");
  const main = page.locator("#main");
  const form = main.getByTestId("branding-form");
  await expect(main.getByTestId("branding-version")).toHaveText("Not set");
  expect(await brandVariable(page)).toBe("#2f4bd8");

  // A brand colour too pale for links on white is held back, naming the pairs it fails.
  await form.getByTestId("color-light-brand-input").fill("#9aa5ff");
  await expect(form.getByTestId("check-light-linkOnCard")).toHaveAttribute("data-passes", "false");
  await expect(form.getByTestId("check-light-linkOnCard")).toContainText("2.28:1, under 4.5:1");
  await expect(form.getByTestId("check-dark-linkOnCard")).toHaveAttribute("data-passes", "true");
  await form.getByTestId("branding-save").click();
  await expect(form.getByTestId("branding-blocked")).toContainText("5 pairs fall under the contrast");
  await expect(main.getByTestId("branding-version")).toHaveText("Not set");

  await form.getByTestId("branding-name").fill("Acme Agents");
  await form.getByTestId("color-light-brand-input").fill("#0f766e");
  await form.getByTestId("color-light-brand-hover-input").fill("#115e59");
  await form.getByTestId("color-light-brand-soft-input").fill("#e6f4f1");
  await form.getByTestId("color-dark-brand-input").fill("#5EEAD4");
  await expect(form.locator("[data-passes=false]")).toHaveCount(0);
  await form.getByTestId("branding-save").click();
  await expect(toast(page, "Branding saved")).toBeVisible();
  await expect(main.getByTestId("branding-version")).toHaveText("Version 1");
  await expect(page.getByTestId("brand")).toHaveText("Acme Agents");
  await expect.poll(() => brandVariable(page)).toBe("#0f766e");
  await expect(page).toHaveTitle("Branding | Acme Agents");

  // The logo takes the mark's place; the favicon becomes the page's icon; both are served without a credential.
  await main.getByTestId("branding-logo-file").setInputFiles({ name: "logo.png", mimeType: "image/png", buffer: PNG });
  await expect(toast(page, "Logo saved")).toBeVisible();
  const logo = page.getByTestId("brand").getByTestId("hub-logo");
  await expect(logo).toHaveAttribute("src", /^\/v1\/branding\/logo\?v=[0-9a-f]{16}$/);
  await main.getByTestId("branding-favicon-file").setInputFiles({ name: "icon.png", mimeType: "image/png", buffer: PNG });
  await expect(toast(page, "Favicon saved")).toBeVisible();
  await page.reload();
  await expect(page.locator('link[rel="icon"]')).toHaveAttribute("href", /\/v1\/branding\/favicon\?v=[0-9a-f]{16}$/);
  expect(await brandVariable(page)).toBe("#0f766e");

  const visitor = await browser.newContext({ baseURL: BASE_URL, colorScheme: "dark", reducedMotion: "reduce" });
  try {
    const signIn = await visitor.newPage();
    await signIn.goto("/login");
    await expect(signIn.getByRole("heading", { level: 1, name: "Sign in to Acme Agents" })).toBeVisible();
    await expect(signIn).toHaveTitle("Sign in | Acme Agents");
    await expect.poll(() => brandVariable(signIn)).toBe("#5eead4");
    const image = await signIn.request.get((await logo.getAttribute("src"))!);
    expect(image.status()).toBe(200);
    expect(image.headers()["content-type"]).toBe("image/png");
  } finally {
    await visitor.close();
  }

  // Rolling back to version 1 brings back the name and colours alone: the mark returns in place of the logo.
  const history = main.getByTestId("branding-history");
  await expect(history.getByTestId("branding-history-version")).toHaveCount(3);
  await history.locator('[data-version="1"]').getByTestId("branding-history-rollback").click();
  await expect(toast(page, "Rolled back to version 1")).toBeVisible();
  await expect(main.getByTestId("branding-version")).toHaveText("Version 4");
  await expect(page.getByTestId("brand").getByTestId("hub-logo")).toHaveCount(0);
  await expect(page.getByTestId("brand")).toHaveText("Acme Agents");

  // With nothing set again, the web is its own.
  await clearHubSettings();
  await page.reload();
  await expect(page.getByTestId("brand")).toHaveText("evo-agents hub");
  await expect(page.locator('style[data-testid="hub-branding"]')).toHaveCount(0);
  expect(await brandVariable(page)).toBe("#2f4bd8");
});

test("a hub admin turns a feature off for a project, and its menu entry, page and API go until it is back", async ({
  page,
  admin,
  signInAs,
}) => {
  const project = uniqueName("features");
  await admin.registerProject(project);
  await admin.grant(project, ADMIN_ACCOUNT.login, "reader", "internal"); // a hub admin reads a project with a grant
  await signInAs(ADMIN_ACCOUNT);
  await page.goto("/admin/features");
  const main = page.locator("#main");
  await main.getByTestId("features-project-select").selectOption(project);
  await main.getByTestId(`feature-${project}-kg-off`).click();
  await main.getByTestId(`features-save-${project}`).click();
  await expect(toast(page, `Features of ${project} saved`)).toBeVisible();

  await page.goto(`/p/${project}`);
  await expect(page.getByTestId("nav-overview")).toBeVisible();
  await expect(page.getByTestId("nav-curator")).toBeVisible();
  await expect(page.getByTestId("nav-kg")).toHaveCount(0);
  await page.goto(`/p/${project}/kg`);
  await expect(main.getByTestId("state-not-found")).toBeVisible();
  const refused = await page.request.get(`/v1/kg/${project}/builds`);
  expect(refused.status()).toBe(404);
  expect((await refused.json()).message).toBe(
    `kg is turned off for project ${project} on this hub; a hub admin turns it on in the administration, Features`,
  );

  // Following the hub again brings it back; rolling back to the first version turns it off once more.
  await page.goto("/admin/features");
  await main.getByTestId("features-project-select").selectOption(project);
  await main.getByTestId(`feature-${project}-kg-hub`).click();
  await main.getByTestId(`features-save-${project}`).click();
  await expect(toast(page, `Features of ${project} saved`)).toBeVisible();
  expect((await page.request.get(`/v1/kg/${project}/builds`)).status()).toBe(200);
  const history = main.getByTestId(`features-history-${project}`);
  await expect(history.getByTestId(`features-history-${project}-version`)).toHaveCount(2);
  await history.locator('[data-version="1"]').getByTestId(`features-history-${project}-rollback`).click();
  await expect(toast(page, "Rolled back to version 1")).toBeVisible();
  await expect(main.getByTestId(`feature-${project}-kg`)).toHaveAttribute("data-choice", "off");
  expect((await page.request.get(`/v1/kg/${project}/builds`)).status()).toBe(404);
});

test("a feature turned off for the whole hub leaves every member's menus, and its page and API answer 404", async ({
  page,
  member,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const [project] = me.projects;
  await setFeatures(null, { values: { chats: false, monitor: false, curator: false } });
  await setFeatures(project, { values: { curator: true } });

  await page.goto(`/p/${project}`);
  await expect(page.getByTestId("nav-plans")).toBeVisible();
  for (const gone of ["nav-chats", "nav-monitor"]) await expect(page.getByTestId(gone)).toHaveCount(0);
  await expect(page.getByTestId("nav-curator")).toBeVisible(); // the project turns it back on for itself
  await page.goto("/monitor");
  await expect(page.locator("#main").getByTestId("state-not-found")).toBeVisible();
  const chats = await page.request.get("/v1/chats");
  expect(chats.status()).toBe(404);
  expect((await chats.json()).message).toContain("chats is turned off on this hub");

  await setFeatures(null, { unset: ["chats", "monitor", "curator"] });
  await page.goto(`/p/${project}`);
  for (const back of ["nav-chats", "nav-monitor", "nav-curator", "nav-kg"]) await expect(page.getByTestId(back)).toBeVisible();
  expect((await page.request.get("/v1/chats")).status()).toBe(200);
});
