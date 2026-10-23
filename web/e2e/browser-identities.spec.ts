import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { endFakeBrowser, startFakeBrowser } from "./support/browser-view";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { bearerClient, machineToken, uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { claimRun, liveWorker, reportState, runOf, runPath, runsOf, workerHeartbeat } from "./support/runs";
import { call } from "../src/lib/api/client";

/**
 * The Browser identities page and the login session (plan agent-browser, criterion a7), against the real API. The
 * page lists the identities the visitor may use with their owner, domains, worker, version, last saved and last used,
 * and a pill only for needs_login or disabled. New takes a name, domains, a login URL and a worker (the visitor's own
 * first, then shared ones, only with the feature browser). Log in queues a login session and opens its run, whose
 * Browser tab is human from the start and has Save instead of Take control; Move does the same on another worker;
 * Disable, Enable and Delete (confirmed) change the identity.
 */
test.skip(isDeployed, "drives a fake worker end against the local stack");

const started = new Set<number>();

test.afterEach(async () => {
  for (const runId of started) await endFakeBrowser(runId, "close");
  started.clear();
});

function main(page: Page) {
  return page.locator("#main");
}

async function createIdentity(me: Member, project: string, worker: string, name: string) {
  const api = bearerClient(await machineToken(me));
  return call(
    api.POST("/v1/projects/{project}/browser-identities", {
      params: { path: { project } },
      body: { name, domains: ["northwind.example"], worker, owner: "member", login_url: "https://northwind.example/login" },
    }),
  );
}

async function overflow(page: Page): Promise<number> {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
}

function card(page: Page, name: string) {
  return main(page).locator(`[data-testid="browser-identity"][data-identity="${name}"]`);
}

test("the page lists an identity with its facts, says a status only when it needs saying, and New makes one", async ({ page, member }) => {
  test.setTimeout(60_000);
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const laptop = uniqueName("laptop");
  const spare = uniqueName("spare");
  await liveWorker(me, project, laptop, 1, { report: { browser: true } });
  await liveWorker(me, project, spare, 1, { report: {} });
  const seeded = uniqueName("work").toLowerCase();
  await createIdentity(me, project, laptop, seeded);

  await open(page, `/p/${project}/browser`);
  await expect(main(page).getByRole("heading", { level: 1, name: "Browser identities" })).toBeVisible();
  const first = card(page, seeded);
  await expect(first).toBeVisible();
  await expect(first.getByTestId("browser-domains")).toHaveText("northwind.example");
  await expect(first).toContainText(laptop);
  await expect(first).toContainText("Not saved yet");
  await expect(first.getByTestId("browser-status")).toHaveCount(0);

  await main(page).getByTestId("browser-new").click();
  const dialog = page.getByTestId("browser-new-dialog");
  await dialog.getByTestId("browser-create").click();
  await expect(dialog.getByText("Use lower case letters, digits, dots, dashes and underscores, starting with a letter or digit.")).toBeVisible();
  await expect(dialog.getByText("Name at least one domain.")).toBeVisible();
  await expect(dialog.getByText("Pick a worker.")).toBeVisible();
  // Only a worker with the feature browser is offered.
  const options = await dialog.getByTestId("browser-worker").locator("option").allTextContents();
  expect(options.some((text) => text.includes(laptop))).toBe(true);
  expect(options.some((text) => text.includes(spare))).toBe(false);
  const fresh = uniqueName("fresh").toLowerCase();
  await dialog.getByTestId("browser-name").fill(fresh);
  await dialog.getByTestId("browser-domains").fill("lumen.example, northwind.example");
  await dialog.getByTestId("browser-login-url").fill("not a url");
  await dialog.getByTestId("browser-create").click();
  await expect(dialog.getByText("Enter an http or https address.")).toBeVisible();
  await dialog.getByTestId("browser-login-url").fill("https://lumen.example/sign-in");
  await dialog.getByTestId("browser-worker").selectOption(laptop);
  await dialog.getByTestId("browser-create").click();
  await expect(dialog).toHaveCount(0);
  await expect(card(page, fresh).getByTestId("browser-domains")).toHaveText("lumen.example, northwind.example");

  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousViolations(page, `the Browser identities page in ${scheme}`);
  }
  await page.emulateMedia({ colorScheme: "light" });
  for (const width of [375, 768, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await overflow(page), `the page scrolls sideways at ${width} px`).toBeLessThanOrEqual(0);
  }
});

test("Log in opens a login session whose browser is human from the start and Save asks the worker to keep it", async ({ page, member }) => {
  test.setTimeout(60_000);
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const live = await liveWorker(me, project, uniqueName("laptop"), 1, { report: { browser: true } });
  const name = uniqueName("work").toLowerCase();
  const identity = await createIdentity(me, project, live.worker.name, name);

  await open(page, `/p/${project}/browser?login=${identity.id}`);
  await expect(card(page, name)).toHaveAttribute("data-highlighted", "true");
  await card(page, name).getByTestId("browser-login").click();
  await expect(page).toHaveURL(/\/runs\/\d+$/);

  const [session] = (await runsOf(me, project)).filter((run) => run.kind === "browser");
  expect((await claimRun(live))?.id).toBe(session.id);
  await workerHeartbeat(live, [session.id], { browser: true });
  await reportState(live, session.id, { state: "interactive" });
  await startFakeBrowser(session.id, live.token);
  started.add(session.id);

  await open(page, `${runPath(project, session.id)}?view=browser`);
  const tab = main(page).getByRole("tab", { name: "Browser", exact: true });
  await expect(tab).toBeVisible();
  if ((await tab.getAttribute("aria-selected")) !== "true") await tab.click();
  const panel = main(page).getByTestId("browser-panel");
  await expect(panel).toHaveAttribute("data-mode", "human");
  await expect(panel.getByTestId("browser-takeover")).toHaveCount(0);
  await expect(panel.getByTestId("browser-handback")).toHaveCount(0);
  await expectNoSeriousViolations(page, "the login session's Browser tab");
  await panel.getByTestId("browser-save").click();
  await expect.poll(async () => (await runOf(me, project, session.id)).finish_requested_at).not.toBeNull();
  await expect(panel.getByTestId("browser-save")).toHaveAttribute("aria-busy", "true");
});

test("Move starts a login session on another worker; Disable, Enable and Delete ask first where they destroy", async ({ page, member }) => {
  test.setTimeout(60_000);
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const home = await liveWorker(me, project, uniqueName("home"), 1, { report: { browser: true } });
  const away = await liveWorker(me, project, uniqueName("away"), 1, { report: { browser: true } });
  const name = uniqueName("work").toLowerCase();
  await createIdentity(me, project, home.worker.name, name);

  await open(page, `/p/${project}/browser`);
  await card(page, name).getByTestId("browser-move").click();
  const move = page.getByTestId("browser-move-dialog");
  const options = await move.getByTestId("browser-worker").locator("option").allTextContents();
  expect(options.some((text) => text.includes(home.worker.name))).toBe(false);
  await move.getByTestId("browser-move-start").click();
  await expect(move.getByText("Pick a worker.")).toBeVisible();
  await move.getByTestId("browser-worker").selectOption(away.worker.name);
  await move.getByTestId("browser-move-start").click();
  await expect(page).toHaveURL(/\/runs\/\d+$/);
  const [session] = (await runsOf(me, project)).filter((run) => run.kind === "browser");
  expect((await claimRun(away))?.id).toBe(session.id);

  // The worker holds the identity from its claim; cancel the session and the worker ends it, then disable and delete.
  await reportCancel(me, project, session.id);
  await reportState(away, session.id, { state: "cancelled" });
  await open(page, `/p/${project}/browser`);
  await card(page, name).getByTestId("browser-disable").click();
  await page.getByTestId("browser-disable-dialog-confirm").click();
  await expect(card(page, name).getByTestId("browser-status")).toHaveText("Disabled");
  await expect(card(page, name).getByTestId("browser-login")).toBeDisabled();
  await card(page, name).getByTestId("browser-enable").click();
  // Never saved, so enabling it leaves it waiting for a login.
  await expect(card(page, name).getByTestId("browser-status")).toHaveText("Needs login");

  await card(page, name).getByTestId("browser-delete").click();
  const confirm = page.getByTestId("browser-delete-dialog");
  await expect(confirm).toBeVisible();
  await expectNoSeriousViolations(page, "the delete confirmation");
  await confirm.getByTestId("browser-delete-dialog-confirm").click();
  await expect(card(page, name)).toHaveCount(0);
});

async function reportCancel(me: Member, project: string, runId: number) {
  const api = bearerClient(await machineToken(me));
  await call(api.POST("/v1/projects/{project}/runs/{run_id}/cancel", { params: { path: { project, run_id: runId } } }));
}
