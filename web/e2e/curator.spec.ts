import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { BLOCKED, seedCurator, TITLES } from "./support/curator";
import { expect, isDeployed, test } from "./support/fixtures";
import { newAccount, uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { liveWorker, REPO } from "./support/runs";
import { toast } from "./support/toast";

/**
 * A project's Curator on the web against the real API (e2e/support/curator.ts plays the hub's job and the worker on
 * duty): an admin writes the charter through the form, which refuses what the hub would, then pauses and resumes the
 * night shift; the Curator's page shows the night, the last review run and the nights before; the proposals list
 * filters by state, tier and lens with counts; a proposal shows its evidence (the run's log, the digest's entry, the
 * line of code), its findings, its draft plan and its ledger, and takes Accept, Defer and Reject; the Inbox lists the
 * tier 2 proposal beside the decisions and answers it in a sheet; Home says where each project's Curator stands; a
 * reader reads everything and changes nothing. axe checks every page and the dialogs in light and dark.
 */
test.skip(isDeployed, "writes charters and plays the night's worker through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

/** The digits of an hour and minute, as a time input takes them. */
function hourAround(offset: number): string {
  return `${String((new Date().getUTCHours() + offset + 24) % 24).padStart(2, "0")}:00`;
}

test("an admin writes the charter from the web, and pauses and resumes the night shift", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  // A worker whose daemon does not say it runs review runs: no pass of curator.collect (other specs run it at any
  // time) queues a review run here, so the night shift stays on duty with nothing in flight.
  const live = await liveWorker(me, project, uniqueName("night"));

  await open(page, `/p/${project}/curator`);
  await expect(main(page).getByRole("heading", { level: 1, name: "Curator" })).toBeVisible();
  await expect(page.getByTestId("nav-curator")).toHaveAttribute("aria-current", "page");
  await expect(main(page).getByTestId("curator-header").getByTestId("curator-state")).toHaveText("Not set up");
  await expect(main(page).getByTestId("state-empty")).toContainText("No charter yet");
  await expectNoSeriousViolations(page, "Curator without a charter");

  await main(page).getByTestId("curator-write-charter").click();
  await expect(page).toHaveURL(new RegExp(`/p/${project}/curator/charter\\?edit=1$`));
  const form = main(page).getByTestId("charter-form");
  await expect(form.getByTestId("charter-worker")).toHaveValue(live.worker.name);

  // The form refuses what the hub would, before anything is sent, and the first wrong field takes focus.
  await form.getByTestId("charter-night-budget").fill("2");
  await form.getByTestId("charter-run-budget").fill("3");
  await form.getByTestId("charter-window-start").fill("23:00");
  await form.getByTestId("charter-window-end").fill("23:00");
  await form.getByTestId("charter-save").click();
  await expect(form.getByTestId("charter-error-windowEnd")).toHaveText(/same time/);
  await expect(form.getByTestId("charter-error-runBudget")).toHaveText(/over the night's budget/);
  await expect(form.getByTestId("charter-window-end")).toBeFocused();
  await expect(form.getByTestId("charter-form-problems")).toHaveText("2 fields need a fix before the charter is saved.");

  await form.getByTestId("charter-add-goal").click();
  await form.getByTestId("charter-goal-id").fill("faster-runs");
  await form.getByTestId("charter-goal-what").fill("Plan runs finish within an hour.");
  await form.getByTestId("charter-window-start").fill(hourAround(-2));
  await form.getByTestId("charter-window-end").fill(hourAround(3));
  await form.getByTestId("charter-timezone").fill("UTC");
  await form.getByTestId("charter-run-budget").fill("0.5");
  await form.getByTestId("charter-max-runs").fill("4");
  await form.getByTestId("charter-protected-paths").fill("deploy/**\n.github/workflows/**");
  await form.getByTestId("charter-hidden-checks").fill("pnpm test -- hidden");
  await form.getByTestId("charter-judge-runtime").selectOption("codex");
  await form.getByTestId("charter-auto-merge").click();
  await expectNoSeriousViolations(page, "the charter's form");
  await form.getByTestId("charter-save").click();

  await expect(toast(page, "Charter saved as revision 1")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/p/${project}/curator/charter$`));
  const view = main(page).getByTestId("charter-view");
  await expect(view).toHaveAttribute("data-revision", "1");
  await expect(view.getByTestId("charter-window")).toHaveText(`${hourAround(-2)} to ${hourAround(3)}, UTC`);
  await expect(view.getByTestId("charter-worker-name")).toHaveText(live.worker.name);
  await expect(view.getByTestId("charter-night-budget-value")).toHaveText("$2.00");
  await expect(view.getByTestId("charter-protected-value")).toHaveText("deploy/**.github/workflows/**");
  await expect(view.getByTestId("charter-hidden-checks-value")).toHaveText("pnpm test -- hidden");
  await expect(view.getByTestId("charter-auto-merge-value")).toHaveText("Tier 0, once CI and the Judge pass");
  await expect(main(page).getByTestId("charter-revision")).toHaveCount(1);
  await expectNoSeriousViolations(page, "the charter");

  // A second save writes revision 2; the first stays readable.
  await main(page).getByTestId("charter-edit").click();
  await main(page).getByTestId("charter-night-budget").fill("3");
  await main(page).getByTestId("charter-save").click();
  await expect(toast(page, "Charter saved as revision 2")).toBeVisible();
  await expect(main(page).getByTestId("charter-revision")).toHaveCount(2);
  await main(page).locator('[data-testid="charter-revision"][data-revision="1"]').click();
  await expect(main(page).getByTestId("charter-old-revision")).toContainText("Revision 1");
  await expect(main(page).getByTestId("charter-night-budget-value")).toHaveText("$2.00");

  // In its window with no run in flight, the night shift is on duty; a pause is confirmed, a resume is not.
  await main(page).getByTestId("curator-tab-overview").click();
  const state = main(page).getByTestId("curator-header").getByTestId("curator-state");
  await expect(state).toHaveText("On duty");
  await expect(main(page).getByTestId("curator-local-time")).toContainText("inside the window");
  await expect(main(page).getByTestId("curator-last-review-none")).toBeVisible();
  await main(page).getByTestId("curator-pause").click();
  const dialog = page.getByTestId("curator-pause-dialog");
  await expect(dialog).toContainText(`Every schedule of ${project} stops at once`);
  await expectNoSeriousViolations(page, "the pause dialog");
  await dialog.getByTestId("curator-pause-dialog-confirm").click();
  await expect(toast(page, "Night shift paused")).toBeVisible();
  await expect(state).toHaveText("Paused");
  await expect(main(page).getByTestId("curator-header")).toContainText(`Paused by ${me.login}`);
  await main(page).getByTestId("curator-resume").click();
  await expect(toast(page, "Night shift resumed")).toBeVisible();
  await expect(state).toHaveText("On duty");
});

test("the night's review shows on the Curator's page, and its proposals filter by tier, state and lens", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const night = await seedCurator(me, project, uniqueName("night"));

  await open(page, `/p/${project}/curator`);
  await expect(main(page).getByTestId("summary-waiting-value")).toHaveText("4");
  await expect(main(page).getByTestId("summary-runs-value")).toHaveText("1");
  await expect(main(page).getByTestId("curator-last-review-run")).toHaveText(`#${night.runId}`);
  await expect(main(page).getByTestId("curator-tab-proposals")).toContainText("4");
  const nights = main(page).getByTestId("curator-nights");
  await expect(nights.locator("tbody tr")).toHaveCount(1);
  await expect(nights.getByTestId("curator-night-proposals")).toHaveText("4");
  await expectNoSeriousViolations(page, "the Curator's page");

  // The review run's own page says what it is and leads back to its proposals.
  await main(page).getByTestId("curator-last-review-run").click();
  await expect(main(page).getByTestId("run-kind")).toHaveText("Review run");
  await expect(main(page).getByTestId("run-proposals-link")).toHaveAttribute("href", `/p/${project}/curator/proposals?run=${night.runId}`);
  await main(page).getByTestId("run-proposals-link").click();

  const table = main(page).getByTestId("proposals-table");
  await expect(main(page).getByTestId("proposals-run-filter")).toHaveText(`Review run #${night.runId}`);
  await expect(table.locator("tbody tr")).toHaveCount(4);
  await main(page).getByTestId("proposals-run-filter").click();
  await expect(page).toHaveURL(new RegExp(`/p/${project}/curator/proposals$`));
  await expect(main(page).getByTestId("proposals-summary")).toHaveText("4 proposals");
  const tiers = main(page).getByTestId("proposals-facet-tier");
  for (const tier of [0, 1, 2, 3]) await expect(tiers.locator(`[data-facet-value="${tier}"]`)).toContainText(`Tier ${tier} 1`);
  await expectNoSeriousViolations(page, "the proposals");

  await tiers.locator('[data-facet-value="2"]').click();
  await expect(page).toHaveURL(/\?tier=2$/);
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table.getByTestId("proposal-link")).toHaveText(TITLES[2]);
  await main(page).getByTestId("proposals-lens").selectOption("security");
  await expect(main(page).getByTestId("state-empty")).toContainText("No proposal matches these filters");
  await main(page).getByTestId("clear-filters").click();
  await expect(table.locator("tbody tr")).toHaveCount(4);
  await main(page).getByTestId("proposals-lens").selectOption("security");
  await expect(page).toHaveURL(/\?lens=security$/);
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table.getByTestId("proposal-link")).toHaveText(TITLES[3]);
  await expect(table.getByTestId("proposal-tier")).toHaveText(/Tier 3/);
});

test("a proposal shows where its evidence leads and its draft plan, and an admin accepts, defers and rejects", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const night = await seedCurator(me, project, uniqueName("night"));
  const wide = night.proposals[2];

  await open(page, `/p/${project}/curator/proposals/${wide.id}`);
  const header = main(page).getByTestId("proposal-header");
  await expect(header.getByRole("heading", { level: 1 })).toHaveText(TITLES[2]);
  await expect(header.getByTestId("proposal-state")).toHaveText("Open");
  await expect(header.getByTestId("proposal-tier")).toHaveText(/Tier 2/);
  await expect(header.getByTestId("proposal-run-link")).toHaveAttribute("href", `/p/${project}/runs/${night.runId}`);
  await expect(main(page).getByTestId("proposal-summary-text")).toContainText("Add wait_for to the worker");
  await expect(main(page).getByTestId("proposal-tier-reasons").locator("li").first()).toBeVisible();
  await expect(main(page).getByTestId("proposal-paths")).toHaveText(`${REPO}:src/wait.py`);

  // Its own evidence: a line of code on the forge; its finding's: the digest's entry and the run's log.
  await expect(main(page).getByTestId("evidence-code-link")).toHaveAttribute("href", "https://github.com/example-org/api/blob/main/src/worker.py#L42");
  const finding = main(page).getByTestId("proposal-finding");
  await expect(finding).toContainText("The harness blocks sleep followed by tail");
  await expect(finding.getByTestId("evidence-run-link")).toHaveAttribute("href", `/p/${project}/runs/${night.runId}?view=log`);
  await finding.getByTestId("evidence-digest-toggle").click();
  await expect(finding.getByTestId("evidence-digest")).toContainText(BLOCKED);
  await expect(finding.getByTestId("evidence-digest")).toContainText(`Pushed by ${me.login}`);

  // The draft plan, as a plan or as the JSON the review run wrote.
  const draft = main(page).getByTestId("proposal-draft");
  await expect(draft.getByTestId("proposal-draft-steps")).toContainText("Wait helper");
  await expect(draft.getByTestId("proposal-draft-steps")).toContainText("pnpm test -- wait");
  await draft.getByTestId("proposal-draft-json").click();
  await expect(draft.getByTestId("proposal-draft-raw")).toContainText('"id": "curator-wait-helper"');
  await expectNoSeriousViolations(page, "a proposal");

  await header.getByTestId("proposal-accept").click();
  const dialog = page.getByTestId("proposal-answer-dialog");
  await expect(dialog).toHaveAttribute("data-action", "accept");
  await dialog.getByTestId("proposal-answer-note").fill("Two\nlines");
  await dialog.getByTestId("proposal-answer-send").click();
  await expect(dialog).toContainText("The note is one line.");
  await expectNoSeriousViolations(page, "the answer dialog");
  await dialog.getByTestId("proposal-answer-note").fill("Ship it after the release.");
  await dialog.getByTestId("proposal-answer-note").press("Control+Enter");
  await expect(toast(page, `Proposal #${wide.id} accepted`)).toBeVisible();
  await expect(header.getByTestId("proposal-state")).toHaveText("Accepted");
  await expect(main(page).getByTestId("proposal-answered-by")).toContainText(`Accepted by ${me.login}`);
  await expect(main(page).getByTestId("proposal-note")).toHaveText("Ship it after the release.");
  await expect(header.getByTestId("proposal-accept")).toHaveCount(0);

  await open(page, `/p/${project}/curator/proposals/${night.proposals[0].id}`);
  await main(page).getByTestId("proposal-defer").click();
  await page.getByTestId("proposal-defer-days").fill("3");
  await page.getByTestId("proposal-answer-send").click();
  await expect(toast(page, `Proposal #${night.proposals[0].id} deferred`)).toBeVisible();
  await expect(main(page).getByTestId("proposal-state")).toHaveText("Deferred");
  await expect(main(page).getByTestId("proposal-deferred-until")).toContainText("It opens again");

  await open(page, `/p/${project}/curator/proposals/${night.proposals[3].id}`);
  await main(page).getByTestId("proposal-reject").click();
  await page.getByTestId("proposal-answer-send").click();
  await expect(toast(page, `Proposal #${night.proposals[3].id} rejected`)).toBeVisible();
  await expect(main(page).getByTestId("proposal-state")).toHaveText("Rejected");

  await open(page, `/p/${project}/curator/proposals?state=open`);
  await expect(main(page).getByTestId("proposals-table").getByTestId("proposal-link")).toHaveText(TITLES[1]);
  const states = main(page).getByTestId("proposals-facet-state");
  await expect(states.locator('[data-facet-value="accepted"]')).toContainText("Accepted 1");
  await expect(states.locator('[data-facet-value="deferred"]')).toContainText("Deferred 1");
  await expect(states.locator('[data-facet-value="rejected"]')).toContainText("Rejected 1");
});

test("a proposal's ledger says what happened to it, line by line, with the figures that set it off", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const night = await seedCurator(me, project, uniqueName("night"));
  const wide = night.proposals[2];

  await open(page, `/p/${project}/curator/proposals/${wide.id}`);
  const ledger = main(page).getByTestId("proposal-ledger");
  const lines = ledger.getByTestId("ledger-line");
  await expect(lines).toHaveCount(1);
  const first = lines.first();
  await expect(first).toHaveAttribute("data-action", "proposed");
  await expect(first.getByTestId("ledger-action")).toHaveText("Proposed");
  await expect(first.getByTestId("ledger-actor")).toContainText("Agent");
  await expect(first.getByTestId("ledger-run")).toHaveAttribute("href", `/p/${project}/runs/${night.runId}`);
  await expect(first.getByTestId("ledger-what")).toHaveText(`Review run #${night.runId} proposed it (feature, tier 2): ${TITLES[2]}`);
  // The night's figures of its lens, and the entry its finding's evidence points at: the blocked command.
  await expect(first.getByTestId("ledger-figure")).toHaveCount(2);
  await expect(first.getByTestId("ledger-figure").first()).toHaveAttribute("data-key", "environment");
  await expect(first.getByTestId("ledger-figure").nth(1)).toContainText("failures from the environment of cause harness_blocked");
  await expect(first.getByTestId("ledger-figure").nth(1)).toContainText("3");
  await expect(ledger.getByTestId("ledger-outcome-due")).toHaveCount(0);
  await expectNoSeriousViolations(page, "a proposal's ledger");

  await main(page).getByTestId("proposal-header").getByTestId("proposal-accept").click();
  const dialog = page.getByTestId("proposal-answer-dialog");
  await dialog.getByTestId("proposal-answer-note").fill("Go.");
  await dialog.getByTestId("proposal-answer-send").click();
  await expect(lines).toHaveCount(2);
  await expect(lines.nth(1)).toHaveAttribute("data-action", "accepted");
  await expect(lines.nth(1).getByTestId("ledger-actor")).toContainText(me.login);
  await expect(lines.nth(1).getByTestId("ledger-what")).toHaveText(`${me.login} accepted it: Go.`);
});

test("the Inbox lists the tier 2 proposal beside the decisions, and an admin answers it in a sheet", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const night = await seedCurator(me, project, uniqueName("night"));
  const wide = night.proposals[2];

  await open(page, "/inbox");
  await expect(main(page).getByTestId("inbox-open-proposals")).toHaveText("1 proposal waits for an answer");
  await expect(page.getByTestId("inbox-bell")).toHaveAccessibleName(/1 proposal waits for an answer$/);
  const item = main(page).getByTestId("inbox-waiting").locator(`[data-proposal-id="${wide.id}"]`);
  await expect(item).toHaveAttribute("data-kind", "proposal");
  await expect(item.getByTestId("proposal-state")).toHaveText("Open");
  await expect(item.getByTestId("proposal-tier")).toHaveText(/Tier 2/);
  await expect(item.getByTestId("notification-open")).toHaveText(`Proposal #${wide.id} (tier 2): ${TITLES[2]}`);
  await expect(item.getByTestId("notification-proposal-link")).toHaveAttribute("href", `/p/${project}/curator/proposals/${wide.id}`);
  await main(page).getByTestId("inbox-facet-kind").locator('[data-facet-value="proposal"]').click();
  await expect(main(page).getByTestId("notification")).toHaveCount(1);

  await item.getByTestId("notification-answer").click();
  await expect(page).toHaveURL(new RegExp(`proposal=${wide.id}`));
  const sheet = page.getByTestId("proposal-sheet");
  await expect(sheet.getByTestId("proposal-sheet-title")).toHaveText(TITLES[2]);
  await expect(sheet.getByTestId("proposal-sheet-title")).toBeFocused();
  await expect(sheet.getByTestId("evidence-code-link")).toBeVisible();
  await expectNoSeriousViolations(page, "a proposal in the Inbox's sheet");
  await sheet.getByTestId("proposal-reject").click();
  const dialog = page.getByTestId("proposal-answer-dialog");
  await dialog.getByTestId("proposal-answer-note").fill("Not this quarter.");
  await dialog.getByTestId("proposal-answer-send").click();
  await expect(toast(page, `Proposal #${wide.id} rejected`)).toBeVisible();
  await expect(sheet.getByTestId("proposal-panel")).toHaveAttribute("data-state", "rejected");
  await sheet.getByTestId("proposal-close").click();
  await expect(sheet).toBeHidden();
  await expect(main(page).getByTestId("inbox-open-proposals")).toHaveCount(0);
  await expect(main(page).locator(`[data-proposal-id="${wide.id}"]`)).toHaveAttribute("data-proposal-state", "rejected");

  // A link that names only the proposal finds its project.
  await open(page, `/inbox?proposal=${wide.id}`);
  await expect(page.getByTestId("proposal-sheet").getByTestId("proposal-panel")).toHaveAttribute("data-state", "rejected");
});

test.describe("dark theme", () => {
  test.use({ colorScheme: "dark" });

  test("Home says where each project's Curator stands, and a reader reads the Curator without changing it", async ({ page, admin, signInAs }) => {
    const owner = newAccount("owner");
    const reader = newAccount("reader");
    const project = uniqueName("curator");
    await admin.registerProject(project);
    await admin.grant(project, owner.login, "admin", "internal");
    await admin.grant(project, reader.login, "reader", "internal");
    const night = await seedCurator(owner, project, uniqueName("night"));
    await signInAs(reader);

    await open(page, "/");
    const row = main(page).locator(`[data-testid="home-project"][data-project="${project}"]`);
    await expect(row.getByTestId("home-project-curator")).toHaveAttribute("data-state", "on_duty");
    await expect(row.getByTestId("home-project-curator-link")).toHaveText("Curator: On duty");
    await expect(row.getByTestId("home-project-proposals")).toHaveText("4 proposals wait");
    await expect(page.locator("html")).toHaveClass(/dark/);
    await expectNoSeriousViolations(page, "Home with a Curator (dark)");

    // G then C opens the Curator of the project shown.
    await open(page, `/p/${project}`);
    await page.keyboard.press("g");
    await page.keyboard.press("c");
    await expect(page).toHaveURL(new RegExp(`/p/${project}/curator$`));
    await expect(main(page).getByTestId("curator-pause")).toHaveCount(0);
    await expectNoSeriousViolations(page, "the Curator's page (dark)");

    await main(page).getByTestId("curator-tab-charter").click();
    await expect(main(page).getByTestId("charter-read-only")).toBeVisible();
    await expect(main(page).getByTestId("charter-edit")).toHaveCount(0);
    await expect(main(page).getByTestId("charter-hidden-checks-value")).toHaveText("Shown to the project's admins only");
    await expectNoSeriousViolations(page, "the charter (dark)");
    await open(page, `/p/${project}/curator/charter?edit=1`);
    await expect(main(page).getByTestId("charter-page")).toHaveAttribute("data-mode", "view");

    await open(page, `/p/${project}/curator/proposals/${night.proposals[2].id}`);
    await expect(main(page).getByTestId("proposal-read-only")).toBeVisible();
    await expect(main(page).getByTestId("proposal-accept")).toHaveCount(0);
    await expectNoSeriousViolations(page, "a proposal (dark)");
  });
});

test.describe("at 375 px", () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test("the nights and the proposals become lists of rows, and no page scrolls sideways", async ({ page, member }) => {
    const me = await member([{ role: "admin", maxLevel: "internal" }]);
    const project = me.projects[0];
    const night = await seedCurator(me, project, uniqueName("night"));
    for (const [path, list] of [
      [`/p/${project}/curator`, "curator-nights"],
      [`/p/${project}/curator/proposals`, "proposals-table"],
    ] as const) {
      await open(page, path);
      await expect(main(page).getByTestId(list)).toHaveAttribute("data-layout", "list");
      expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBe(0);
      await expectNoSeriousViolations(page, `${path} at 375 px`);
    }
    await expect(main(page).locator(`[data-proposal-id="${night.proposals[2].id}"]`)).toBeVisible();
    for (const path of [`/p/${project}/curator/proposals/${night.proposals[2].id}`, `/p/${project}/curator/charter?edit=1`]) {
      await open(page, path);
      await expect(main(page).getByRole("heading", { level: 1 })).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBe(0);
    }
  });
});
