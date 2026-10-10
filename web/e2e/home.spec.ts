import type { Page } from "@playwright/test";

import { expect, isDeployed, test } from "./support/fixtures";
import { newAccount, uniqueName } from "./support/hub";
import { open } from "./support/plans";
import {
  askDecision,
  claimRun,
  decisionOf,
  dispatch,
  liveWorker,
  planRunUnderway,
  reportState,
  runsOf,
  seedPlanRunPlan,
  seedRunPlan,
  startRun,
} from "./support/runs";
import { toast } from "./support/toast";

/**
 * Home against the real API (GET /v1/me/overview and the workers list): the metric strip counts what waits for the
 * member, what runs and what is queued; Needs you lists the decisions only they answer, and one is answered in two
 * clicks from there (Answer, then Send answer with the agent's pick chosen); In flight, Recent with Rerun, the Fleet
 * card and the Projects card follow. A member reads nothing of a project they hold no grant on. The specs play the
 * worker. Pages render in English.
 */
test.skip(isDeployed, "seeds runs and decisions through the local stack");

const QUESTION = "Deploy the plan-runs build to staging now?";
const SECOND = "Drop the old staging bucket after the deploy?";

function main(page: Page) {
  return page.locator("#main");
}

/** A plan run waiting for its owner's decision, a step run at work and a step run queued, all of `owner`'s. */
async function seedWork(owner: Parameters<typeof seedPlanRunPlan>[0], project: string) {
  await seedPlanRunPlan(owner, project);
  const waiting = await planRunUnderway(owner, project, uniqueName("home-plan"), { waiting: true });
  await seedRunPlan(owner, project);
  const live = await liveWorker(owner, project, uniqueName("home-step"));
  const [working, queued] = await dispatch(owner, project, ["2", "4"]);
  await claimRun(live);
  await startRun(live, working.id);
  return { waiting, live, working, queued };
}

test("an admin with an open decision and runs in flight sees them counted and listed", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const { waiting, working, queued } = await seedWork(me, project);

  await open(page, "/");
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Home");
  const strip = main(page).getByTestId("home-metrics");
  await expect(strip.getByTestId("summary-waiting-value")).toHaveText("1");
  await expect(strip.getByTestId("summary-running-value")).toHaveText("1");
  await expect(strip.getByTestId("summary-queued-value")).toHaveText("1");
  await expect(strip.getByTestId("summary-done-value")).toHaveText("0");
  await expect(strip.getByTestId("summary-failed-value")).toHaveText("0");
  await expect(strip.getByTestId("summary-waiting")).toContainText(`#${waiting.run.id}, asked`);
  await expect(strip.getByTestId("summary-running")).toContainText(`#${working.id} on`);
  await expect(strip.getByTestId("summary-queued")).toContainText(`#${queued.id}, queued`);

  // Needs you: the one decision, its run and plan, and when the run parks.
  const needs = main(page).getByRole("region", { name: "Needs you" });
  await expect(needs.getByTestId("needs-you-item")).toHaveCount(1);
  await expect(needs.getByTestId("needs-you-item")).toHaveAttribute("data-decision-id", String(waiting.decision));
  await expect(needs.getByRole("link", { name: QUESTION })).toHaveAttribute("href", `/inbox?decision=${waiting.decision}`);
  await expect(needs.getByTestId("needs-parks")).toContainText("parks in");

  // In flight: the run at work first with its live dot, then the plan run waiting for the member, then the queued one.
  const flight = main(page).getByRole("region", { name: "In flight" });
  const rows = flight.getByTestId("in-flight-item");
  await expect(rows).toHaveCount(3);
  expect(await rows.evaluateAll((items) => items.map((item) => item.getAttribute("data-run-id")))).toEqual(
    [working.id, waiting.run.id, queued.id].map(String),
  );
  await expect(rows.nth(0).locator(".animate-live-ping")).toHaveCount(1);
  await expect(rows.nth(1).getByTestId("run-state")).toHaveText("Waiting for you");
  await expect(rows.nth(1).getByTestId("home-plan-progress")).toHaveAttribute("data-total", "4");
  await expect(rows.nth(1).getByTestId("home-plan-progress")).toContainText("1 of 4 steps done");
  await expect(rows.nth(2).getByTestId("run-state")).toHaveText("Queued");
  await expect(flight.getByRole("link", { name: "Watch on Monitor" })).toHaveAttribute("href", "/monitor");

  // Fleet: both workers of the member's, each busy with the run it holds.
  const fleet = main(page).getByTestId("fleet");
  await expect(fleet.getByTestId("fleet-worker")).toHaveCount(2);
  await expect(fleet.getByTestId("worker-status")).toHaveText(["Busy 1/1", "Busy 1/1"]);
  await expect(fleet.getByTestId("heartbeat-bars")).toHaveCount(2);

  // Projects: the project with its open decision.
  const projects = main(page).getByTestId("home-projects").getByTestId("home-project");
  await expect(projects).toHaveCount(1);
  await expect(projects.getByTestId("home-project-decisions")).toHaveAttribute("data-count", "1");
  await expect(projects.getByTestId("home-project-facts")).toContainText("Admin, 2 active plans, 2 repos");

  // A second decision reaches Needs you within one read, without a reload.
  await askDecision(waiting.live, waiting.run.id, SECOND, "4");
  await expect(needs.getByTestId("needs-you-item")).toHaveCount(2, { timeout: 15_000 });
  await expect(strip.getByTestId("summary-waiting-value")).toHaveText("2");
});

test("answers a decision in two clicks, and it leaves Needs you", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedPlanRunPlan(me, project);
  const { run, decision } = await planRunUnderway(me, project, uniqueName("home-answer"), { waiting: true });

  await open(page, "/");
  const needs = main(page).getByTestId("needs-you");
  const answer = needs.getByRole("button", { name: `Answer decision #${decision}` });

  // One: Answer opens the decision in a sheet over Home, the agent's pick chosen.
  await answer.click();
  const sheet = page.getByTestId("decision-sheet");
  await expect(sheet).toBeVisible();
  await expect(page).toHaveURL(/\/$/);
  await expect(sheet.getByRole("radio", { name: /Deploy to staging now/ })).toBeChecked();
  await expect(sheet.getByTestId("decision-parks")).toContainText("parks in");
  // Two: Send answer.
  await sheet.getByTestId("decision-send").click();

  await expect(toast(page, `Answer sent to run #${run.id}`)).toBeVisible();
  await expect(sheet.getByTestId("decision-answer")).toBeVisible();
  await expect(main(page).getByTestId("needs-you")).toHaveCount(0);
  await expect(main(page).getByTestId("home-metrics").getByTestId("summary-waiting-value")).toHaveText("0");
  const answered = await decisionOf(me, project, decision!);
  expect(answered.state).toBe("answered");
  expect(answered.answer_option).toBe("deploy");

  // Closing the sheet leaves the visitor on Home, the run still in flight.
  await sheet.getByTestId("decision-close").click();
  await expect(sheet).toBeHidden();
  await expect(main(page).getByTestId("in-flight").getByTestId("in-flight-item")).toHaveCount(1);
});

test("reruns a failed run from Recent, where the member writes", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, uniqueName("home-rerun"));
  const [failing] = await dispatch(me, project, ["2"]);
  await claimRun(live);
  await startRun(live, failing.id);
  await reportState(live, failing.id, { state: "failed", error: "verify failed: pnpm test exited 1" });

  await open(page, "/");
  const strip = main(page).getByTestId("home-metrics");
  // Nothing is in flight: the strip gives way to the quiet line, which still counts the week.
  await expect(strip.getByTestId("home-quiet")).toContainText("All quiet.");
  await expect(strip.getByTestId("home-quiet")).toContainText("Last 7 days: 0 done, 1 failed, 0 lost.");
  const recent = main(page).getByRole("region", { name: "Recent" });
  const row = recent.getByTestId("recent-item").filter({ has: page.locator(`[data-run-id="${failing.id}"]`) });
  await expect(row.getByTestId("recent-error")).toHaveText("verify failed: pnpm test exited 1");
  await row.getByRole("button", { name: `Rerun #${failing.id}` }).click();

  // The toast comes once the hub has answered; the new run is then queued.
  await expect(toast(page, /^Run #\d+ dispatched/)).toBeVisible();
  const rerun = (await runsOf(me, project)).find((item) => item.id !== failing.id);
  expect(rerun?.state).toBe("queued");
  await expect(toast(page, `Run #${rerun!.id} dispatched`)).toContainText(`A rerun of #${failing.id}`);
  await expect(main(page).getByTestId("in-flight").locator(`[data-run-id="${rerun!.id}"]`).first()).toBeVisible();
  await expect(strip.getByTestId("summary-queued-value")).toHaveText("1");
});

test("a member reads nothing of a project they hold no grant on", async ({ page, member, admin }) => {
  // Project B: another member's, with a decision waiting and runs in flight.
  const other = newAccount("other");
  const hidden = uniqueName("hidden");
  await admin.registerProject(hidden);
  await admin.grant(hidden, other.login, "writer", "internal");
  await seedWork(other, hidden);

  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  await open(page, "/");
  const home = main(page);
  await expect(home.getByTestId("home-projects").getByTestId("home-project")).toHaveCount(1);
  await expect(home.getByTestId("home-projects").getByTestId("home-project")).toHaveAttribute("data-project", me.projects[0]);
  await expect(home.getByTestId("home-quiet")).toBeVisible();
  await expect(home.getByTestId("needs-you")).toHaveCount(0);
  await expect(home.getByTestId("in-flight")).toHaveCount(0);
  await expect(home.getByTestId("recent-empty")).toBeVisible();
  await expect(home.getByTestId("fleet-empty")).toBeVisible();
  await expect(home).not.toContainText(hidden);
  await expect(home).not.toContainText(QUESTION);

  // The switcher lists only the project of the grant.
  await page.getByTestId("project-switcher").click();
  const menu = page.getByTestId("project-switcher-menu");
  await expect(menu.locator("[data-project]")).toHaveCount(1);
  await expect(menu).not.toContainText(hidden);
});

test("on a phone Home is one column, Needs you first", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  await seedWork(me, me.projects[0]);
  await page.setViewportSize({ width: 375, height: 812 });
  await open(page, "/");
  const order = ["needs-you", "in-flight", "recent", "fleet", "home-projects"];
  for (const id of order) await expect(main(page).getByTestId(id)).toBeVisible();
  const boxes = await Promise.all(order.map((id) => main(page).getByTestId(id).boundingBox()));
  for (const [index, box] of boxes.entries()) {
    expect(box, order[index]).not.toBeNull();
    expect(box!.x, `${order[index]} starts at the gutter`).toBe(boxes[0]!.x);
    expect(box!.width, `${order[index]} takes the column`).toBe(boxes[0]!.width);
    if (index > 0) expect(box!.y, `${order[index]} below ${order[index - 1]}`).toBeGreaterThan(boxes[index - 1]!.y + boxes[index - 1]!.height - 1);
  }
  // Answer is a 44 px touch target.
  expect((await main(page).getByTestId("needs-you-answer").boundingBox())?.height).toBeGreaterThanOrEqual(44);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});
