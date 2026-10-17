import type { Page } from "@playwright/test";

import { expect, isDeployed, test } from "./support/fixtures";
import { signIn } from "./support/auth";
import { BASE_URL } from "./support/env";
import { ADMIN_ACCOUNT, newAccount, uniqueName } from "./support/hub";
import {
  enablePlugin,
  installedSample,
  installPlugin,
  pluginPagePath,
  projectConfigOf,
  projectPluginOf,
  publishSample,
  SAMPLE_PAGE,
} from "./support/plugins";
import { toast } from "./support/toast";

/**
 * Criteria a10 and a22 of plan hub-agent-plugins in the browser, against the real API (web/DESIGN.md, Plugins): a hub
 * admin installs, upgrades and removes a plugin of the store and reads the publishers the operator trusts; an admin
 * of a project enables one through its consent and fills the form drawn from its config schema, a secret never shown
 * again, while a reader only reads; the page an isolated plugin adds opens from the project's menu in a sandboxed
 * frame that reads what the project granted through the bridge, with the viewer's session, and neither the hub's
 * cookies, the page around it, a fetch of its own nor a method the project did not grant; and the native page of the
 * core plugin notes, which the end-to-end build carries, exists only in a project that enabled it, its notes kept
 * while it is off. Each test publishes a plugin of its own from the sample of tests/fixtures/plugins/sample.
 */
test.skip(isDeployed, "publishes and installs plugins through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

function frameOf(page: Page) {
  return page.frameLocator('[data-testid="plugin-frame"]');
}

test("a hub admin installs, upgrades and removes a plugin, and reads the publishers the hub trusts", async ({ page, signInAs }) => {
  const id = uniqueName("store");
  await publishSample(id, "1.0.0");
  await signInAs(ADMIN_ACCOUNT);
  await page.goto("/admin/plugins");
  await expect(page.getByTestId("admin-nav").getByRole("link", { name: "Plugins" })).toHaveAttribute("aria-current", "page");
  const publishers = main(page).getByTestId("plugin-publishers");
  await expect(publishers.locator('[data-publisher="acme"]')).toContainText(/ed25519:/);

  const row = main(page).getByTestId("plugins-table").locator("tr", { has: page.locator(`[data-plugin-link="${id}"]`) });
  await expect(row.getByTestId("plugin-state")).toHaveText("Available");
  await expect(row).toContainText("Latest 1.0.0");
  await row.getByTestId("plugin-install").click();
  await expect(toast(page, `Installed Sample 1.0.0`)).toBeVisible();
  await expect(row.getByTestId("plugin-state")).toHaveText("Installed");

  // A newer version in the store is an upgrade waiting, on the row and on the plugin's page.
  await publishSample(id, "1.1.0");
  await page.reload();
  await expect(row.getByTestId("plugin-upgrade")).toHaveText("1.1.0 available");
  await row.locator(`[data-plugin-link="${id}"]`).click();
  await expect(page).toHaveURL(`/admin/plugins/${id}`);
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Sample");
  await expect(main(page).getByTestId("plugin-permissions")).toContainText("Read the project's runs, their events and their outcomes");
  await expect(main(page).getByTestId("plugin-contributes")).toContainText("1 page");
  await expect(main(page).getByTestId("plugin-versions-table").locator("tbody tr")).toHaveCount(2);
  await main(page).getByTestId("plugin-upgrade-open").click();
  const upgrade = page.getByTestId("plugin-upgrade-dialog");
  await expect(upgrade).toContainText("Projects that enabled it keep what they granted");
  await upgrade.getByTestId("plugin-upgrade-confirm").click();
  await expect(toast(page, `Upgraded Sample to 1.1.0`)).toBeVisible();
  await expect(main(page).getByTestId("plugin-upgrade-open")).toHaveCount(0);

  // The hub's own layer of the config: a field from the schema, saved as version 1.
  const form = main(page).getByTestId("instance-config-form");
  await form.getByTestId("config-field-greeting").fill("hello from the hub");
  await form.getByTestId("instance-config-form-save").click();
  await expect(toast(page, "Saved version 1 of the config")).toBeVisible();

  await main(page).getByTestId("plugin-remove-open").click();
  await page.getByTestId("plugin-remove-confirm").click();
  await expect(toast(page, "Removed Sample")).toBeVisible();
  await expect(main(page).getByTestId("plugin-state")).toHaveText("Available");
});

test("a project's admin enables a plugin through its consent and configures it, a secret never shown again", async ({
  page,
  member,
  admin,
  browser,
}) => {
  const id = await installedSample("consent");
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const reader = newAccount("plugin-reader");
  await admin.grant(project, reader.login, "reader", "internal");

  await page.goto(`/p/${project}`);
  await page.getByTestId("nav-plugins").click();
  await expect(page).toHaveURL(`/p/${project}/plugins`);
  const row = main(page).getByTestId("project-plugins-table").locator("tr", { has: page.locator(`[data-plugin-link="${id}"]`) });
  await expect(row.getByTestId("plugin-project-state")).toHaveText("Not enabled");
  await expect(page.getByTestId(`nav-plugin-${id}-${SAMPLE_PAGE.name}`)).toHaveCount(0);

  await row.getByTestId("plugin-enable").click();
  const consent = page.getByTestId("plugin-consent");
  await expect(consent.getByRole("heading", { name: `Enable Sample in ${project}?` })).toBeVisible();
  await expect(consent.getByTestId("consent-permissions").locator("[data-permission]")).toHaveCount(1);
  await expect(consent.getByTestId("consent-permissions")).toContainText("Read the project's runs, their events and their outcomes");
  await expect(consent).toContainText(SAMPLE_PAGE.title);
  await consent.getByTestId("plugin-consent-grant").click();
  await expect(toast(page, `Enabled Sample in ${project}`)).toBeVisible();
  await expect(row.getByTestId("plugin-project-state")).toHaveText("Enabled");
  expect((await projectPluginOf(me, project, id)).granted).toEqual(["runs:read"]);
  // Its page joins the project's menu, before the project's Plugins.
  await expect(page.getByTestId(`nav-plugin-${id}-${SAMPLE_PAGE.name}`)).toHaveText(SAMPLE_PAGE.title);

  // The form the schema draws: a text field with its default as the hint, and a secret given once.
  await row.locator(`[data-plugin-link="${id}"]`).click();
  const form = main(page).getByTestId("project-config-form");
  await expect(form.getByTestId("config-field-greeting")).toHaveAttribute("placeholder", "Default: hello");
  await form.getByTestId("config-field-greeting").fill("hi");
  await form.getByTestId("config-secret-api_key-replace").click();
  const secret = `key-${uniqueName("secret")}`;
  await form.getByTestId("config-field-api_key").fill(secret);
  await form.getByTestId("project-config-form-save").click();
  await expect(toast(page, "Saved version 1 of the config")).toBeVisible();
  await expect(form.getByTestId("config-secret-api_key-state")).toHaveText("Secret set");
  await expect(form.getByTestId("config-field-api_key")).toHaveCount(0);
  await expect(main(page).getByTestId("plugin-effective-config")).toContainText("Secret, set");
  expect(await page.content()).not.toContain(secret);
  const layer = await projectConfigOf(me, project, id);
  expect([layer.version, layer.values, layer.secrets]).toEqual([1, { greeting: "hi" }, ["api_key"]]);

  // A field the schema bounds is refused before anything goes out.
  await form.getByTestId("config-field-greeting").fill("x".repeat(41));
  await form.getByTestId("project-config-form-save").click();
  await expect(form.getByTestId("config-problem-greeting")).toHaveText("At most 40 characters.");
  expect((await projectConfigOf(me, project, id)).version).toBe(1);

  // A reader reads what the layer sets, and changes nothing.
  const context = await browser.newContext({ baseURL: BASE_URL, reducedMotion: "reduce" });
  const readerPage = await context.newPage();
  await signIn(readerPage, reader);
  await readerPage.goto(`/p/${project}/plugins/${id}`);
  const shown = main(readerPage).getByTestId("project-config-form");
  await expect(shown).toContainText("hi");
  await expect(shown).toContainText("Secret set");
  await expect(shown.getByRole("textbox")).toHaveCount(0);
  await expect(main(readerPage).getByTestId("plugin-disable")).toHaveCount(0);
  await expect(main(readerPage).getByTestId("project-config-history-rollback")).toHaveCount(0);
  expect(await readerPage.content()).not.toContain(secret);
  await context.close();
});

test("a plugin's page opens in a sandbox from the project's menu and reaches the hub only through the bridge", async ({ page, member }) => {
  const id = await installedSample("frame");
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await enablePlugin(me, project, id);

  await page.goto(`/p/${project}`);
  await page.getByTestId(`nav-plugin-${id}-${SAMPLE_PAGE.name}`).click();
  await expect(page).toHaveURL(pluginPagePath(project, id));
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText(SAMPLE_PAGE.title);
  await expect(main(page).getByTestId("plugin-sandboxed")).toBeVisible();
  const frame = main(page).getByTestId("plugin-frame");
  await expect(frame).toHaveAttribute("sandbox", "allow-scripts allow-forms");
  await expect(frame).toHaveAttribute("src", new RegExp(`^/v1/plugin-ui/${id}/${project}/[0-9]+\\.[0-9a-f]{32}/index\\.html$`));

  const inside = frameOf(page);
  await expect(inside.locator("#context")).toHaveText(`${id} in ${project} for ${me.login}`);
  await expect(inside.locator("#methods")).toHaveText("context, resize, route, runs.list, runs.get");
  // runs:read was granted: the page reads the project's runs with the viewer's session.
  await expect(inside.locator("#runs")).toHaveText("allowed: 0 runs");
  // plans:read was not: the bridge refuses before the hub is asked.
  await expect(inside.locator("#plans")).toHaveText("refused: not_granted");
  // The opaque origin of the sandbox: no cookie of the hub, no reach into the page around it, no fetch of its own.
  await expect(inside.locator("#cookie")).toHaveText("blocked: SecurityError");
  await expect(inside.locator("#parent")).toHaveText("blocked: SecurityError");
  await expect(inside.locator("#fetch")).toHaveText("blocked: TypeError");
  expect((await page.context().cookies()).some((cookie) => cookie.name === "evo_hub_session")).toBe(true);
  // Loaded whole by the server too, the page's first requests wait for the bridge rather than go unanswered.
  await page.reload();
  await expect(frameOf(page).locator("#plans")).toHaveText("refused: not_granted");
  await expect(frameOf(page).locator("#runs")).toHaveText("allowed: 0 runs");

  // The page's files carry a CSP of their own that keeps the sandbox when the URL is opened on its own.
  const src = (await frame.getAttribute("src")) ?? "";
  const direct = await page.request.get(src);
  expect(direct.status()).toBe(200);
  expect(direct.headers()["content-security-policy"]).toContain("sandbox allow-scripts allow-forms");
  expect(direct.headers()["content-security-policy"]).toContain("connect-src 'none'");

  // Disabled, the plugin leaves the menu and its page is a 404.
  await page.goto(`/p/${project}/plugins/${id}`);
  await main(page).getByTestId("plugin-disable").click();
  await page.getByTestId("plugin-disable-confirm-confirm").click();
  await expect(toast(page, `Disabled Sample in ${project}`)).toBeVisible();
  await expect(page.getByTestId(`nav-plugin-${id}-${SAMPLE_PAGE.name}`)).toHaveCount(0);
  expect((await page.request.get(src)).status()).toBe(404);
  await page.goto(pluginPagePath(project, id));
  await expect(page.getByTestId("state-not-found")).toBeVisible();
});

test("the core plugin notes adds a native page only where a project enabled it, and keeps its notes while off", async ({
  page,
  member,
}) => {
  const me = await member([
    { role: "admin", maxLevel: "internal" },
    { role: "admin", maxLevel: "internal" },
  ]);
  const [project, other] = me.projects;
  const notesPath = `/p/${project}/x/notes/entries`;

  await page.goto(notesPath);
  await expect(page.getByTestId("state-not-found")).toBeVisible();
  await page.goto(`/p/${project}/plugins`);
  const row = main(page).getByTestId("project-plugins-table").locator("tr", { has: page.locator('[data-plugin-link="notes"]') });
  await expect(row.getByTestId("plugin-tier")).toHaveText(/^Core/);
  await row.getByTestId("plugin-enable").click();
  await expect(page.getByTestId("consent-permissions")).toContainText("Read the project's plans");
  await page.getByTestId("plugin-consent-grant").click();
  await expect(toast(page, `Enabled Notes in ${project}`)).toBeVisible();

  await page.getByTestId("nav-plugin-notes-entries").click();
  await expect(page).toHaveURL(notesPath);
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Notes");
  await expect(main(page).getByTestId("notes-empty")).toBeVisible();
  const note = `Ship on ${uniqueName("day")}`;
  await main(page).getByTestId("notes-form").getByRole("textbox").fill(note);
  await main(page).getByTestId("notes-add").click();
  await expect(main(page).getByTestId("notes-list")).toContainText(note);

  // Another project of the same member has not enabled it: no menu item, and the page is a 404.
  await page.goto(`/p/${other}`);
  await expect(page.getByTestId("nav-plugin-notes-entries")).toHaveCount(0);
  await page.goto(`/p/${other}/x/notes/entries`);
  await expect(page.getByTestId("state-not-found")).toBeVisible();

  // Off, the page is a 404 and the menu forgets it; on again, the note is still there.
  await page.goto(`/p/${project}/plugins/notes`);
  await main(page).getByTestId("plugin-disable").click();
  await page.getByTestId("plugin-disable-confirm-confirm").click();
  await expect(toast(page, `Disabled Notes in ${project}`)).toBeVisible();
  await expect(page.getByTestId("nav-plugin-notes-entries")).toHaveCount(0);
  await page.goto(notesPath);
  await expect(page.getByTestId("state-not-found")).toBeVisible();
  await enablePlugin(me, project, "notes", ["plans:read"]);
  await page.goto(notesPath);
  await expect(main(page).getByTestId("notes-list")).toContainText(note);
});

test("a reader of a project sees the plugins installed on the hub, read only, and none that is only published", async ({
  page,
  member,
}) => {
  const id = uniqueName("hidden");
  await publishSample(id);
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  await page.goto(`/p/${me.projects[0]}/plugins`);
  await expect(main(page).getByTestId("plugins-read-only")).toBeVisible();
  await expect(main(page).locator(`[data-plugin-link="${id}"]`)).toHaveCount(0);
  await installPlugin(id);
  await page.reload();
  const row = main(page).getByTestId("project-plugins-table").locator("tr", { has: page.locator(`[data-plugin-link="${id}"]`) });
  await expect(row.getByTestId("plugin-project-state")).toHaveText("Not enabled");
  await expect(row.getByTestId("plugin-enable")).toHaveCount(0); // a reader enables nothing
});
