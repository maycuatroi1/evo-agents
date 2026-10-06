import type { Page } from "@playwright/test";

import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { REAL_DECISION, REAL_EVENTS, replayWaitingRun, seedRealPlan } from "./support/real-run";
import { runPath } from "./support/runs";

/**
 * The page of a plan run fed what a real one sent: the events of run #8 of the production hub (Claude Code's raw
 * output, its tool calls with nested content, the usage update with its cost and iterations, the hub's step reports
 * and decision) and its wait for decision #1. The spec plays the worker, as e2e/support/real-run.ts says.
 */
test.skip(isDeployed, "replays a run through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

test("the page of a real Claude Code plan run waiting for a decision renders", async ({ page, member }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRealPlan(me, project);
  const { run, decision } = await replayWaitingRun(me, project, uniqueName("real-box"));
  expect(run.state).toBe("waiting");

  await open(page, runPath(project, run.id));
  const detail = main(page).getByTestId("run-detail");
  await expect(detail).toHaveAttribute("data-state", "waiting");
  await expect(main(page).getByTestId("log-status")).toHaveAttribute("data-status", "live");
  await expect(main(page).getByTestId("log-count")).toHaveText(`${REAL_EVENTS.length} lines`);
  await expect(main(page).getByTestId("run-decision")).toContainText(REAL_DECISION.question);
  await expect(main(page).getByTestId("run-decision-link")).toHaveAttribute("href", `/inbox?decision=${decision}`);
  const steps = main(page).getByTestId("run-plan-step");
  await expect(steps).toHaveCount(5);
  for (const [index, status] of ["done", "done", "in_progress", "pending", "pending"].entries()) {
    await expect(steps.nth(index)).toHaveAttribute("data-status", status);
  }
  expect(errors).toEqual([]);
});
