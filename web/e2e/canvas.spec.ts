import type { Page } from "@playwright/test";

import { signOut } from "./support/auth";
import {
  baselinePath,
  CANVAS_NAME,
  CARD,
  canvasesPath,
  canvasPath,
  claimByApi,
  claimPath,
  createCanvasByApi,
  editByApi,
  enableCanvas,
  METRIC,
  openBaselineByApi,
  pushConversions,
  seedCanvas,
  signalsPath,
  TENANT,
  valueCheck,
} from "./support/canvas";
import { expect, isDeployed, test } from "./support/fixtures";
import { HubAdmin, newAccount, uniqueName } from "./support/hub";
import { apiOf } from "./support/memories";
import { revealNavItem } from "./support/nav";
import { toast } from "./support/toast";

/**
 * Criterion a8 of plan living-lean-canvas in the browser, against the real API: a project that enabled the core plugin
 * canvas has Lean Canvas in its menu, a canvas's grid of nine cells with each claim's status, a claim's evidence, its
 * history with what each rev changed and its undo, the 409 of an edit made at an old rev, the proposals that wait to be
 * accepted, a baseline's review from the Inbox, and the Signals page with a key's latest value and series; a reader only
 * reads; a project that has not enabled it has no menu item, and its pages and API answer 404.
 */
test.skip(isDeployed, "enables plugins and seeds canvases through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

function cell(page: Page, name: string) {
  return main(page).locator(`[data-testid="canvas-cell"][data-cell="${name}"]`);
}

test("Lean Canvas shows only where a project enabled it, and a writer starts a canvas there", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  const api = await apiOf(me);
  await page.goto(canvasesPath(project));
  await expect(page.getByTestId("state-not-found")).toBeVisible();
  await page.goto(`/p/${project}`);
  await expect(page.getByTestId("nav-plugin-canvas-canvases")).toHaveCount(0);
  await expect(page.getByTestId("nav-signals")).toBeAttached();
  const off = await api.GET("/v1/x/canvas/{project}/canvases", { params: { path: { project } } });
  expect(off.response.status).toBe(404);

  await enableCanvas(me, project);
  await page.reload();
  await revealNavItem(page, "nav-plugin-canvas-canvases");
  await page.getByTestId("nav-plugin-canvas-canvases").click();
  await expect(page).toHaveURL(canvasesPath(project));
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Lean Canvas");
  await expect(main(page).getByTestId("canvases-empty")).toBeVisible();
  const on = await api.GET("/v1/x/canvas/{project}/canvases", { params: { path: { project } } });
  expect(on.response.status).toBe(200);

  await main(page).getByTestId("canvas-new").first().click();
  const form = page.getByTestId("canvas-new-form");
  await form.getByTestId("canvas-new-create").click();
  await expect(form.getByText("Give the product a name.")).toBeVisible();
  await form.getByTestId("canvas-new-name").fill(CANVAS_NAME);
  await expect(form.getByTestId("canvas-new-slug")).toHaveValue("northwind-checkout");
  await form.getByTestId("canvas-new-create").click();
  await expect(toast(page, `Canvas ${CANVAS_NAME} created`)).toBeVisible();
  await expect(page).toHaveURL(canvasPath(project, "northwind-checkout"));
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText(CANVAS_NAME);
  await expect(main(page).getByTestId("canvas-cell")).toHaveCount(9);
  await expect(cell(page, "problem").getByTestId("cell-empty")).toBeVisible();
});

test("a writer adds a claim to a cell, moves it to testing with its test card, reads what each rev changed and undoes one", async ({
  page,
  member,
}) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  await enableCanvas(me, project);
  await createCanvasByApi(me, project);
  await page.goto(canvasPath(project));

  await cell(page, "problem").getByTestId("cell-add").click();
  const dialog = page.getByTestId("claim-new-form");
  await expect(dialog).toContainText("New claim in Problem");
  await dialog.getByTestId("claim-new-create").click();
  await expect(dialog.getByText("Write the claim.")).toBeVisible();
  await dialog.getByTestId("claim-text").fill("Shop owners lose a sale when checkout takes more than two minutes");
  await dialog.getByTestId("claim-new-create").click();
  await expect(toast(page, "CL-1 added to Problem")).toBeVisible();
  const card = cell(page, "problem").getByTestId("claim-card");
  await expect(card).toHaveAttribute("data-claim", "CL-1");
  await expect(card.getByTestId("claim-status")).toHaveText("Assumption");

  await card.getByTestId("claim-card-text").click();
  await expect(page).toHaveURL(claimPath(project, "CL-1"));
  await expect(main(page).getByTestId("claim-page-code")).toHaveText("CL-1");
  await main(page).getByTestId("claim-edit").click();
  const editor = main(page).getByTestId("claim-editor");
  await editor.getByTestId("claim-status-select").selectOption("testing");
  await editor.getByTestId("claim-save").click();
  await expect(editor.getByText("Testing needs this.")).toHaveCount(3);
  await editor.getByTestId("card-hypothesis").fill(CARD.hypothesis);
  await editor.getByTestId("card-test").fill(CARD.test);
  await editor.getByTestId("card-deadline").fill(CARD.deadline);
  await editor.getByTestId("claim-reason").fill("The pilot starts");
  await editor.getByTestId("claim-save").click();
  await expect(toast(page, "CL-1 saved")).toBeVisible();
  await expect(main(page).getByTestId("claim-editor")).toHaveCount(0);
  await expect(main(page).getByTestId("claim-head").getByTestId("claim-status")).toHaveText("Testing");
  await expect(main(page).getByTestId("claim-test-card")).toContainText(CARD.hypothesis);

  const history = main(page).getByTestId("claim-history");
  await expect(history.getByTestId("claim-rev")).toHaveCount(2);
  const rev2 = history.locator('[data-testid="claim-rev"][data-rev="2"]');
  await expect(rev2).toContainText("The pilot starts");
  await expect(rev2.locator('[data-field="status"]')).toContainText("Testing");
  await expect(rev2.locator('[data-field="test_card.hypothesis"]')).toContainText(CARD.hypothesis);
  await history.getByTestId("claim-compare-toggle").click();
  await expect(history.getByTestId("claim-compare-changes").locator('[data-field="status"]')).toContainText("Assumption");

  await rev2.getByTestId("claim-undo").click();
  await expect(toast(page, "CL-1 rev 2 taken back")).toBeVisible();
  await expect(history.getByTestId("claim-rev")).toHaveCount(3);
  await expect(history.locator('[data-testid="claim-rev"][data-rev="3"]')).toContainText("undo of rev 2");
  await expect(main(page).getByTestId("claim-head").getByTestId("claim-status")).toHaveText("Assumption");
  await main(page).getByTestId("claim-canvas-link").click();
  await expect(cell(page, "problem").getByTestId("claim-status")).toHaveText("Assumption");
});

test("an edit made at an old rev is refused with what changed, then carried over to the newest rev", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  const other = newAccount("canvas-other");
  await new HubAdmin().grant(project, other.login, "writer", "internal");
  await seedCanvas(me, project);

  await page.goto(claimPath(project, "CL-3"));
  await main(page).getByTestId("claim-edit").click();
  const editor = main(page).getByTestId("claim-editor");
  await editor.getByTestId("claim-confidence").selectOption("high");
  // Another writer changes the claim's text while the editor is open.
  await editByApi(other, project, [{ claim: "CL-3", base_rev: 1, text: "Checkout in one tap, on every shop" }]);
  await editor.getByTestId("claim-save").click();
  const conflict = editor.getByTestId("claim-conflict");
  await expect(conflict).toContainText(`CL-3 changed since you opened it at rev 1: ${other.login} wrote rev 2`);
  await expect(conflict.locator('[data-field="text"]')).toContainText("Checkout in one tap, on every shop");
  await conflict.getByTestId("claim-conflict-keep").click();
  await expect(editor).toContainText("Edit CL-3 at rev 2");
  await expect(editor.getByTestId("claim-text")).toHaveValue("Checkout in one tap, on every shop");
  await editor.getByTestId("claim-save").click();
  await expect(toast(page, "CL-3 saved")).toBeVisible();
  const held = await claimByApi(me, project, "CL-3");
  expect(held).toMatchObject({ rev: 3, text: "Checkout in one tap, on every shop", confidence: "high" });
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Checkout in one tap, on every shop");
});

test("signals become evidence through the value check, which proposes; a writer accepts it and reads the series", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  await seedCanvas(me, project);
  expect((await pushConversions(me, project)).accepted).toBe(3);
  expect(await valueCheck(project)).toMatchObject({ evidence: 1, proposals: 1 });

  // The claim's evidence pane: the hub's piece, real, with the check it rests on.
  await page.goto(claimPath(project, "CL-1"));
  const evidence = main(page).getByTestId("claim-evidence").getByTestId("evidence");
  await expect(evidence).toHaveAttribute("data-kind", "signal");
  await expect(evidence.getByTestId("evidence-direction")).toHaveText("Supports");
  await expect(evidence.getByTestId("evidence-real")).toBeVisible();
  const basis = evidence.getByTestId("evidence-basis");
  await expect(basis).toHaveAttribute("data-result", "met");
  await expect(basis).toContainText("from 3 signals over 30 days");

  // The proposal waits on the canvas until a writer accepts it.
  await page.goto(canvasPath(project));
  const proposal = main(page).getByTestId("canvas-proposals").getByTestId("proposal");
  await expect(proposal).toContainText("value check");
  await expect(proposal.locator('[data-field="status"]')).toContainText("Validated");
  await proposal.getByTestId("proposal-accept").click();
  await expect(toast(page, "Proposal accepted as CL-1")).toBeVisible();
  await expect(main(page).getByTestId("canvas-proposals")).toHaveCount(0);
  await expect(cell(page, "problem").getByTestId("claim-status")).toHaveText("Validated");

  // The Signals page: the key's latest value, then its series, of one tenant and of another that sent nothing.
  await page.goto(claimPath(project, "CL-1"));
  await main(page).getByTestId("evidence-basis-signals").click();
  await expect(page).toHaveURL(signalsPath(project, `?key=${METRIC}`));
  await expect(main(page).getByTestId("signal-latest")).toContainText("0.5");
  const series = main(page).getByTestId("signal-series");
  await expect(series.getByTestId("series-row")).toHaveCount(3);
  await expect(series.getByTestId("series-chart")).toBeVisible();
  await series.getByTestId("series-tenant").fill(TENANT);
  await series.getByTestId("series-tenant").press("Enter");
  await expect(page).toHaveURL(signalsPath(project, `?key=${METRIC}&tenant=${TENANT}`));
  await expect(series.getByTestId("series-row")).toHaveCount(3);
  await series.getByTestId("series-tenant").fill("northwind-co");
  await series.getByTestId("series-tenant").press("Enter");
  await expect(series.getByTestId("series-empty")).toBeVisible();
  await series.getByTestId("series-range-7").click();
  await expect(page).toHaveURL(signalsPath(project, `?key=${METRIC}&tenant=northwind-co&days=7`));
});

test("another writer reviews baselines from the Inbox: accepts a change of status and extends an overdue test card", async ({ page, member }) => {
  const reviewer = await member([{ role: "writer", maxLevel: "internal" }]);
  const [project] = reviewer.projects;
  const owner = newAccount("canvas-owner");
  await new HubAdmin().grant(project, owner.login, "admin", "internal");
  await seedCanvas(owner, project);
  const first = await openBaselineByApi(owner, project, "CL-1");
  const second = await openBaselineByApi(owner, project, "CL-2");

  // Whoever opened a baseline, or changed the status, may not decide it.
  const ownerApi = await apiOf(owner);
  const refused = await ownerApi.POST("/v1/x/canvas/{project}/canvases/{slug}/baselines/{number}/items/{ref}/decide", {
    params: { path: { project, slug: "northwind", number: first, ref: "CL-1" } },
    body: { decision: "accepted", reason: "" },
  });
  expect(refused.response.status).toBe(403);

  // The notice in the reviewer's Inbox opens the review.
  await page.goto("/inbox");
  const notice = main(page).locator(`[data-testid="notification"][data-notice-kind="canvas_baseline"]`).filter({ hasText: `Baseline ${first} ` });
  await expect(notice.getByTestId("notice-kind")).toHaveText("A Lean Canvas baseline waits for your review");
  await notice.getByTestId("notification-link").click();
  await expect(page).toHaveURL(baselinePath(project, first));
  await expect(main(page).getByTestId("baseline-head").getByTestId("baseline-status")).toHaveText("Waiting for review");
  const item = main(page).locator('[data-testid="baseline-item"][data-claim="CL-1"]');
  await expect(item).toHaveAttribute("data-kind", "status");
  await expect(item).toContainText(`May not decide it: ${owner.login}`);
  await item.getByTestId("baseline-reason").fill("The pilot is under way");
  await item.getByTestId("baseline-accepted").click();
  await expect(toast(page, "CL-1 accepted")).toBeVisible();
  await expect(main(page).getByTestId("baseline-head").getByTestId("baseline-status")).toHaveText("Approved");
  await expect(item.getByTestId("baseline-decided")).toHaveAttribute("data-decision", "accepted");
  await expect(main(page).getByTestId("baseline-snapshot").locator('li[data-claim="CL-1"]')).toContainText("Testing");

  // The overdue test card: extending it needs a day after today.
  await page.goto(baselinePath(project, second));
  const overdue = main(page).locator('[data-testid="baseline-item"][data-claim="CL-2"]');
  await expect(overdue).toHaveAttribute("data-kind", "overdue");
  await overdue.getByTestId("baseline-extended").click();
  await expect(overdue.getByText("Pick a day after today.")).toBeVisible();
  await overdue.getByTestId("baseline-deadline").fill("2099-06-30");
  await overdue.getByTestId("baseline-extended").click();
  await expect(toast(page, "CL-2 extended")).toBeVisible();
  await expect(main(page).getByTestId("baseline-head").getByTestId("baseline-status")).toHaveText("Approved");
  expect((await claimByApi(owner, project, "CL-2")).test_card?.deadline).toBe("2099-06-30");

  // The canvas lists both baselines, approved, and nothing waits any more.
  await page.goto(canvasPath(project));
  await expect(main(page).getByTestId("canvas-baselines").getByTestId("baseline-line")).toHaveCount(2);
  await expect(main(page).getByTestId("canvas-baseline-waiting")).toHaveCount(0);
});

test("a reader reads a canvas, a claim, a baseline and the signals, with no control that writes", async ({ page, signInAs }) => {
  const hub = new HubAdmin();
  const project = uniqueName("canvas");
  const owner = newAccount("canvas-owner");
  const reader = newAccount("canvas-reader");
  await hub.registerProject(project);
  await hub.grant(project, owner.login, "admin", "internal");
  await hub.grant(project, reader.login, "reader", "internal");
  await seedCanvas(owner, project);
  const number = await openBaselineByApi(owner, project, "CL-1");
  await pushConversions(owner, project);

  await signInAs(reader);
  await page.goto(canvasPath(project));
  await expect(main(page).getByTestId("canvas-read-only")).toBeVisible();
  await expect(main(page).getByTestId("canvas-cell")).toHaveCount(9);
  await expect(main(page).getByTestId("cell-add")).toHaveCount(0);
  await expect(main(page).getByTestId("claim-new")).toHaveCount(0);
  await page.goto(claimPath(project, "CL-1"));
  await expect(main(page).getByTestId("canvas-read-only")).toBeVisible();
  await expect(main(page).getByTestId("claim-edit")).toHaveCount(0);
  await expect(main(page).getByTestId("evidence-add")).toHaveCount(0);
  await expect(main(page).getByTestId("claim-undo")).toHaveCount(0);
  await page.goto(baselinePath(project, number));
  await expect(main(page).getByTestId("baseline-decide")).toHaveCount(0);
  await expect(main(page).getByTestId("baseline-not-yours")).toBeVisible();
  await page.goto(signalsPath(project, `?key=${METRIC}`));
  await expect(main(page).getByTestId("signal-series").getByTestId("series-row")).toHaveCount(3);

  const api = await apiOf(reader);
  const edit = await api.POST("/v1/x/canvas/{project}/canvases/{slug}/edit", {
    params: { path: { project, slug: "northwind" } },
    body: { changes: [{ claim: "CL-3", base_rev: 1, text: "A reader's text" }], reason: "" },
  });
  expect(edit.response.status).toBe(403);
  await signOut(page);
});
