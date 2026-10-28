import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { CANVAS_SLUG, openBaselineByApi } from "./support/canvas";
import { expect, isDeployed, test } from "./support/fixtures";
import { newAccount, uniqueName } from "./support/hub";
import { decidePath, goalsPath, HELD_PLAN, itemPath, loopPath, PROPOSED, RELEASED_PLAN, seedLoop, seedLoopProject } from "./support/loop";
import { open } from "./support/plans";
import { submitVersion } from "./support/srs";
import { toast } from "./support/toast";

/**
 * The pages of a project's Loop (plan loop-engine, criteria a10 and a8), against the real API with the work items the
 * Loop's tick would leave (e2e/loop_seed.py): the Goals page lists each goal's metric against its target and its open
 * work items, and a goal's page its work items by state; the Loop page shows whether the Loop is on, its breaker, its
 * work items by state and by chain and why each waits, and an admin turns it off and on and resets its breaker; a work
 * item's page shows its chain from the goal to the claim, use case, task, plan, merge request and release; the
 * Decisions page gathers the three gates and what the Loop's chain waits on. A reader reads and turns nothing.
 */
test.skip(isDeployed, "seeds the Loop through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

test("goals show their metric against the target, and a goal's page its work items by state", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedLoopProject(me, project);
  await seedLoop(project);

  await open(page, goalsPath(project));
  await expect(main(page).getByRole("heading", { level: 1, name: "Goals" })).toBeVisible();
  await expect(page.getByTestId("nav-goals")).toHaveAttribute("aria-current", "page");
  const table = main(page).getByTestId("goals-table");
  await expect(table.locator("tbody tr")).toHaveCount(2);
  const first = table.locator("tbody tr").filter({ hasText: "Shops check out in one tap" });
  await expect(first.getByTestId("goal-value")).toHaveText("0.4567");
  await expect(first.getByTestId("goal-reached")).toHaveAttribute("data-reached", "false");
  await expect(first.getByTestId("goal-progress").getByRole("progressbar")).toHaveAttribute("aria-valuenow", "76");
  await expect(first.getByTestId("goal-open-items").getByTestId("item-state")).toHaveText(["1 running", "2 waiting for a person", "1 queued"]);
  await expect(table.locator("tbody tr").filter({ hasText: "Shops reorder every week" })).toContainText("No metric");
  await expectNoSeriousViolations(page, "goals");

  await first.getByTestId("goal-link").click();
  await expect(page).toHaveURL(new RegExp(`${goalsPath(project, "G-1")}$`));
  await expect(main(page).getByTestId("goal-code")).toHaveText("G-1");
  await expect(main(page).getByTestId("goal-status")).toHaveText("Active");
  await expect(main(page).getByTestId("goal-metric").getByTestId("goal-value")).toHaveText("0.4567");
  await expect(main(page).getByTestId("goal-facts")).toContainText("northwind");
  const items = main(page).getByTestId("goal-items-table");
  await expect(items.locator("tbody tr")).toHaveCount(10);
  await main(page).getByTestId("goal-item-states").locator('[data-facet-value="waiting_human"]').click();
  await expect(items.locator("tbody tr")).toHaveCount(2);
  await expect(items.getByTestId("item-waiting")).toHaveText(["Its SRS version waits for review", "1 merge request held for a person"]);
});

test("an admin reads the Loop by state and by chain, opens a chain, resets the breaker and turns the Loop off and on", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedLoopProject(me, project);
  const seeded = await seedLoop(project, { tripped: true });

  await open(page, loopPath(project));
  await expect(main(page).getByRole("heading", { level: 1, name: "Loop" })).toBeVisible();
  await expect(main(page).getByTestId("loop-state")).toHaveAttribute("data-state", "stopped");
  await expect(main(page).getByTestId("loop-breaker-banner")).toContainText("31 work items in an hour");
  const summary = main(page).getByTestId("loop-summary");
  for (const [state, count] of [["running", 1], ["waiting_human", 2], ["queued", 1], ["watching", 0], ["done", 5]] as const) {
    await expect(summary.getByTestId(`summary-${state}`)).toHaveAttribute("data-value", String(count));
  }
  const table = main(page).getByTestId("loop-items-table");
  await expect(table.locator("tbody tr")).toHaveCount(10);
  const queued = table.locator("tbody tr").filter({ has: page.locator(`[data-item-id="${seeded.items.queued}"]`) });
  await expect(queued.getByTestId("item-waiting")).toHaveText(/^Worker northwind-mini: no live worker northwind-mini/);
  await expect(queued.getByTestId("item-state")).toHaveText("Queued");
  await expectNoSeriousViolations(page, "the Loop as an admin");

  // By state, then by chain: the use case of CL-1 with the builds and deliveries that came of it.
  await main(page).getByTestId("loop-states").locator('[data-facet-value="waiting_human"]').click();
  await expect(page).toHaveURL(/state=waiting_human/);
  await expect(table.locator("tbody tr")).toHaveCount(2);
  await main(page).getByTestId("loop-states").locator('[data-facet-value=""]').click();
  await expect(page).not.toHaveURL(/state=/);
  await main(page).getByTestId("loop-view-chain").click();
  await expect(page).toHaveURL(/view=chain/);
  const chains = main(page).getByTestId("loop-chain");
  await expect(chains).toHaveCount(4);
  const claim = main(page).locator(`[data-testid="loop-chain"][data-root="${seeded.items.use_case}"]`);
  await expect(claim.getByTestId("chain-item")).toHaveCount(7);
  await expect(claim.locator(`[data-testid="chain-item"][data-item-id="${seeded.items.deliver}"]`)).toHaveAttribute("data-depth", "2");

  // A chain from the goal to the release; G-1 answers no problem, so the chain's first stage is not reached.
  await claim.locator(`[data-testid="chain-item"][data-item-id="${seeded.items.build}"]`).getByTestId("chain-item-link").click();
  await expect(page).toHaveURL(new RegExp(`${itemPath(project, seeded.items.build)}$`));
  const rail = main(page).getByTestId("chain-rail");
  await expect(rail.getByTestId("chain-stage")).toHaveCount(8);
  await expect(rail.locator('[data-stage="problem"]')).toHaveAttribute("data-reached", "false");
  for (const stage of ["goal", "claim", "use_case", "task", "plan", "merges", "release"]) {
    await expect(rail.locator(`[data-stage="${stage}"]`)).toHaveAttribute("data-reached", "true");
  }
  await expect(rail.locator('[data-stage="claim"]')).toContainText("CL-1");
  await expect(rail.locator('[data-stage="use_case"]')).toContainText("UC ORD-1");
  await expect(rail.locator('[data-stage="plan"]')).toContainText(RELEASED_PLAN);
  await expect(rail.locator('[data-stage="merges"]')).toContainText("api !41");
  await expect(main(page).getByTestId("chain-history").getByTestId("chain-event")).toHaveCount(5);
  await expect(main(page).getByTestId("chain-items").locator('[aria-current="true"]')).toHaveAttribute("data-item-id", String(seeded.items.build));
  await expectNoSeriousViolations(page, "a work item's chain");

  // The held delivery waits for a person, on the Decisions page.
  await open(page, itemPath(project, seeded.items.held));
  await expect(main(page).getByTestId("chain-waiting")).toContainText("1 merge request held for a person");
  await expect(main(page).getByTestId("chain-rail").locator('[data-stage="plan"]')).toContainText(HELD_PLAN);
  await expect(main(page).getByTestId("chain-rail").locator('[data-stage="release"]')).toHaveAttribute("data-reached", "false");

  // The breaker reset, then the Loop turned off (its unfinished work items cancelled) and on again.
  await open(page, loopPath(project));
  await main(page).getByTestId("loop-breaker-reset").click();
  await page.getByTestId("loop-breaker-dialog").getByRole("button", { name: "Reset breaker" }).click();
  await expect(toast(page, "The Loop's breaker is reset")).toBeVisible();
  await expect(main(page).getByTestId("loop-state")).toHaveAttribute("data-state", "on");
  await expect(main(page).getByTestId("loop-breaker-banner")).toHaveCount(0);
  await main(page).getByTestId("loop-turn-off").click();
  const dialog = page.getByTestId("loop-switch-dialog");
  await expect(dialog).toContainText("Its 4 unfinished work items are cancelled.");
  await dialog.getByRole("button", { name: "Turn off" }).click();
  await expect(toast(page, "The Loop is off")).toBeVisible();
  await expect(main(page).getByTestId("loop-state")).toHaveAttribute("data-state", "off");
  await expect(main(page).getByTestId("loop-off")).toBeVisible();
  await expect(summary.getByTestId("summary-running")).toHaveAttribute("data-value", "0");
  await main(page).getByTestId("loop-turn-on").click();
  await page.getByTestId("loop-switch-dialog").getByRole("button", { name: "Turn on" }).click();
  await expect(toast(page, "The Loop is on")).toBeVisible();
  await expect(main(page).getByTestId("loop-state")).toHaveAttribute("data-state", "on");
});

test("the Decisions page gathers the three gates and the Loop's exceptions, and a reader turns nothing", async ({ page, admin, signInAs }) => {
  const project = uniqueName("loop");
  const me = newAccount("loop-owner");
  await admin.registerProject(project);
  await admin.grant(project, me.login, "admin", "internal");
  await seedLoopProject(me, project);
  await openBaselineByApi(me, project, "CL-2");
  await submitVersion(me, project, 1);
  const seeded = await seedLoop(project, { tripped: true });
  const reader = newAccount("loop-reader");
  await admin.grant(project, reader.login, "reader", "internal");
  await signInAs(reader);

  await open(page, decidePath(project));
  await expect(main(page).getByRole("heading", { level: 1, name: "Decisions" })).toBeVisible();
  await expect(page.getByTestId("nav-decide")).toHaveAttribute("aria-current", "page");
  const groups = main(page).getByTestId("board-group");
  await expect(groups).toHaveCount(5);
  const group = (name: string) => main(page).locator(`[data-testid="board-group"][data-group="${name}"]`);
  await expect(group("goal").locator('[data-testid="board-item"][data-kind="goal_overdue"]')).toContainText("G-2 is past its deadline 2026-01-31");
  await expect(group("canvas").locator('[data-testid="board-item"][data-kind="baseline"]')).toHaveCount(1);
  const breaker = group("policy").locator('[data-testid="board-item"][data-kind="loop_breaker"]');
  await expect(breaker.getByTestId("board-item-deciders")).toContainText(me.login);
  await expect(breaker).toHaveAttribute("data-mine", "false");
  const held = group("exception").locator('[data-testid="board-item"][data-kind="merge_held"]');
  await expect(held).toContainText("Merge request !42 of api blocked");
  await expect(held.getByTestId("board-item-chain")).toHaveAttribute("href", itemPath(project, seeded.items.held));
  const review = group("srs").locator('[data-testid="board-item"][data-kind="srs_review"]');
  await expect(review.getByTestId("board-item-title")).toHaveText("SRS demo-shop v1 waits for its review");
  await expect(review.getByTestId("board-item-chain")).toHaveAttribute("href", itemPath(project, seeded.items.review));
  await expect(main(page).getByTestId("board-summary").getByTestId("summary-policy")).toHaveAttribute("data-value", "1");
  await expectNoSeriousViolations(page, "the Decisions page as a reader");

  // Its link opens the Loop, which a reader reads: no switch, no reset.
  await breaker.getByTestId("board-item-open").click();
  await expect(page).toHaveURL(new RegExp(`${loopPath(project)}$`));
  await expect(main(page).getByTestId("loop-read-only")).toBeVisible();
  await expect(main(page).getByTestId("loop-breaker-banner")).toContainText("An admin of the project resets it.");
  await expect(main(page).getByTestId("loop-breaker-reset")).toHaveCount(0);
  await expect(main(page).getByTestId("loop-turn-off")).toHaveCount(0);
  await expect(main(page).getByTestId("loop-items-table").locator("tbody tr")).toHaveCount(10);
});

test("the Decisions page reads each claim proposal by goal and run, opens one on its canvas, and rejects a goal's together", async ({ page, admin, signInAs }) => {
  const project = uniqueName("loop");
  const me = newAccount("loop-owner");
  await admin.registerProject(project);
  await admin.grant(project, me.login, "admin", "internal");
  await seedLoopProject(me, project);
  const seeded = await seedLoop(project, { proposals: true });
  const [first, second] = seeded.proposals ?? [];
  await signInAs(me);

  await open(page, decidePath(project));
  const proposals = main(page).getByTestId("board-proposals");
  await expect(proposals.getByTestId("board-proposals-deciders")).toHaveText(`A writer: ${me.login}`);
  await expect(main(page).locator('[data-testid="board-group"][data-group="canvas"]').getByTestId("board-item-deciders")).toHaveCount(0);
  const group = proposals.locator('[data-testid="proposal-group"][data-goal="G-1"]');
  await expect(group).toContainText("Shops check out in one tap");
  await expect(group.getByTestId("proposal-run")).toHaveAttribute("data-run", String(seeded.run));
  const lines = group.getByTestId("proposal-line");
  await expect(lines.getByTestId("proposal-text")).toHaveText([...PROPOSED]);
  await expect(lines.first().getByTestId("proposal-cell")).toHaveText("Solution");
  await expect(lines.first().getByTestId("proposal-test")).toContainText("checkout.one_tap_share mean over 30 days >= 0.6, by 2026-12-31");
  const opening = lines.first().getByTestId("proposal-open");
  await expect(opening).toHaveAttribute("href", `/p/${project}/x/canvas/canvases/${CANVAS_SLUG}#proposal-${first}`);
  await expectNoSeriousViolations(page, "the Decisions page with claim proposals");

  // Open lands on the proposal on its canvas.
  await opening.click();
  await expect(main(page).locator(`[data-testid="proposal"][data-proposal="${first}"]`)).toHaveAttribute("data-anchored", "true");
  await expect(main(page).locator(`[data-testid="proposal"][data-proposal="${second}"]`)).not.toHaveAttribute("data-anchored", "true");

  // A writer rejects the goal's proposals in one go, with one reason.
  await open(page, decidePath(project));
  await group.getByTestId("proposal-group-reject").click();
  await expect(page.getByTestId("proposals-reject-confirm")).toBeDisabled();
  await page.getByTestId("proposals-reject-reason").fill("G-1 now measures repeat orders");
  await page.getByTestId("proposals-reject-confirm").click();
  await expect(toast(page, "2 proposals rejected")).toBeVisible();
  await expect(main(page).getByTestId("board-proposals")).toHaveCount(0);
});

test.describe("in Vietnamese", () => {
  test.use({ uiLocale: "vi" });

  test("the Loop and a goal read in Vietnamese", async ({ page, member }) => {
    const me = await member([{ role: "admin", maxLevel: "internal" }]);
    const project = me.projects[0];
    await seedLoopProject(me, project);
    await seedLoop(project);
    await open(page, loopPath(project));
    await expect(main(page).getByTestId("loop-state")).toHaveText("Đang bật");
    await expect(main(page).getByTestId("loop-summary").getByTestId("summary-waiting_human")).toContainText("Chờ người");
    await open(page, goalsPath(project, "G-1"));
    await expect(main(page).getByTestId("goal-status")).toHaveText("Đang chạy");
    await expect(main(page).getByTestId("goal-metric")).toContainText("Chưa đạt");
  });
});
