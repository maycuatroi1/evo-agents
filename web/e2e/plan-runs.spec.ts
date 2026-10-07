import type { Page } from "@playwright/test";

import { expect, isDeployed, test } from "./support/fixtures";
import { newAccount, uniqueName } from "./support/hub";
import { open } from "./support/plans";
import {
  askDecision,
  claimRun,
  dispatch,
  HARNESS_REPO,
  MODELS,
  PLAN_RUN_BRANCH,
  PLAN_RUN_PLAN,
  PLAN_RUN_TITLES,
  planRunUnderway,
  planRunWorker,
  REPO,
  reportState,
  reportStep,
  runOf,
  runPath,
  runsOf,
  seedPlanRunPlan,
  seedRunPlan,
  workerHeartbeat,
} from "./support/runs";
import { toast } from "./support/toast";

/**
 * Plan runs from the web against the real API: Run plan shows only for a writer, on the plan's page and on its row of
 * the plans list; its dialog says what the run will do and queues one run of kind plan with the worker, runtime, model
 * and timeout picked; the plan's page then follows the run in a banner (its worker, state, steps done and the decision
 * it waits on), the list marks the plan, and while the run is active neither Run plan nor a step's Run this step can be
 * used. The pages never scroll sideways at 375, 768 and 1024 px. The specs play the worker. Pages render in English.
 */
test.skip(isDeployed, "dispatches plan runs through the local stack");

const PLAN_PATH = (project: string) => `/p/${project}/plans/${PLAN_RUN_PLAN}`;

function main(page: Page) {
  return page.locator("#main");
}

test("a reader follows a plan run but is offered no Run plan", async ({ page, member, admin }) => {
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  const project = me.projects[0];
  const writer = newAccount("writer");
  await admin.grant(project, writer.login, "writer", "internal");
  await seedPlanRunPlan(writer, project);

  await open(page, `/p/${project}/plans`);
  await expect(main(page).getByTestId("plans-table-active")).toContainText(PLAN_RUN_PLAN);
  await expect(main(page).getByTestId("plans-run-plan")).toHaveCount(0);
  await open(page, PLAN_PATH(project));
  await expect(main(page).getByRole("heading", { level: 1 })).toBeVisible();
  await expect(main(page).getByTestId("plan-actions")).toHaveCount(0);
  await expect(main(page).getByRole("button", { name: "Run plan" })).toHaveCount(0);
  await expect(main(page).getByTestId("plans-read-only")).toContainText("Plans change from the CLI with evo harness step");

  // The writer's plan run shows to the reader as it goes, still without a way to start another.
  const { run } = await planRunUnderway(writer, project, uniqueName("writer-box"));
  await open(page, PLAN_PATH(project));
  const banner = main(page).getByTestId("plan-run-banner");
  await expect(banner).toContainText(`Plan run #${run.id}`);
  await expect(banner.getByTestId("plan-run-phase")).toHaveText("Running");
  await expect(banner).toContainText(`dispatched by ${writer.login}`);
  await expect(main(page).getByRole("button", { name: "Run plan" })).toHaveCount(0);
  await open(page, `/p/${project}/plans`);
  await expect(main(page).getByTestId("plan-run-link")).toHaveAttribute("data-run-id", String(run.id));
  await expect(main(page).getByTestId("plans-run-plan")).toHaveCount(0);
});

test("a writer runs a plan from its page, and the page follows the run until it waits for a decision", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedPlanRunPlan(me, project);
  const live = await planRunWorker(me, project, uniqueName("desk"));

  await open(page, PLAN_PATH(project));
  const button = main(page).getByTestId("run-plan");
  await expect(button).toHaveText("Run plan");
  await expect(button).not.toHaveAttribute("aria-disabled", "true");
  await button.click();
  const dialog = page.getByTestId("plan-run-dialog");
  await expect(dialog.getByRole("heading", { name: "Run plan" })).toBeVisible();

  // What the run will do: the three steps not done, the two repos on the plan's branches, the checkpoint, the push.
  const summary = dialog.getByTestId("plan-run-summary");
  await expect(summary.getByTestId("plan-run-steps")).toContainText("3 steps to run");
  await expect(summary.getByTestId(`plan-run-repo-${REPO}`)).toContainText(PLAN_RUN_BRANCH);
  await expect(summary.getByTestId(`plan-run-repo-${REPO}`).getByTestId("plan-run-default-branch")).toHaveCount(0);
  await expect(summary.getByTestId(`plan-run-repo-${HARNESS_REPO}`).getByTestId("plan-run-default-branch")).toHaveText("Default branch");
  await expect(summary.getByTestId("plan-run-checkpoints")).toContainText(PLAN_RUN_TITLES["3"]);
  await expect(summary.getByTestId("plan-run-push")).toContainText(`The plan names the default branch of ${HARNESS_REPO} (main)`);
  await expect(dialog.getByTestId("dispatch-outlook")).toHaveText(`Matching now: ${live.worker.name} (1 free slot).`);

  // A model goes with a runtime; the models the worker lists are offered.
  await dialog.getByTestId("plan-run-model-input").fill(MODELS[0]);
  await expect(dialog.getByTestId("plan-run-model-error")).toContainText("Pick a runtime for this model");
  await expect(dialog.getByTestId("plan-run-submit")).toBeDisabled();
  await dialog.getByRole("radio", { name: /^Claude Code/ }).check();
  await expect(dialog.getByTestId("plan-run-model-error")).toHaveCount(0);
  await expect(dialog.getByTestId("plan-run-model-suggestions").locator("option")).toHaveCount(MODELS.length);
  await dialog.getByTestId("dispatch-target-pin").click();
  await expect(dialog.getByTestId("dispatch-pinned-worker")).toHaveValue(String(live.worker.id));
  await expect(dialog.getByTestId("dispatch-outlook")).toHaveText(`${live.worker.name} can take it now.`);
  await dialog.getByTestId("plan-run-timeout").selectOption("8");
  await dialog.getByTestId("plan-run-submit").click();
  await expect(dialog).toBeHidden();

  // The hub holds one queued run of kind plan, as asked.
  const [run] = await runsOf(me, project);
  expect(run).toMatchObject({
    kind: "plan",
    state: "queued",
    plan_id: PLAN_RUN_PLAN,
    step_key: null,
    requested_runtime: "claude-code",
    model: MODELS[0],
    mode: "headless",
    approval: "auto",
    timeout_min: 480,
    pinned_worker_id: live.worker.id,
    dispatched_by: me.login,
    repos: [
      { repo: REPO, branch: PLAN_RUN_BRANCH },
      { repo: HARNESS_REPO, branch: "main" },
    ],
  });
  await expect(toast(page, `Plan run #${run.id} dispatched`)).toContainText(PLAN_RUN_PLAN);
  const banner = main(page).getByTestId("plan-run-banner");
  await expect(banner).toContainText(`Plan run #${run.id}`);
  await expect(banner.getByTestId("plan-run-phase")).toHaveText("Queued");
  await expect(banner.getByTestId("plan-run-banner-progress")).toHaveText("1 of 4 steps done");
  await expect(button).toHaveAttribute("aria-disabled", "true");
  await expect(main(page).getByTestId("run-plan-lock")).toHaveText(`Plan run #${run.id} is Queued.`);
  await button.click({ force: true }); // a locked button opens nothing
  await expect(dialog).toBeHidden();

  // The worker takes it and starts step 2: the banner follows within one refresh, without a reload.
  expect((await claimRun(live))?.id).toBe(run.id);
  await workerHeartbeat(live, [run.id], { repos: [REPO, HARNESS_REPO], models: MODELS });
  await reportState(live, run.id, { state: "running", session_id: "3f2a9c1e-0000-4000-8000-0000000000bb" });
  await reportStep(live, run.id, "2", "in_progress");
  await expect(banner.getByTestId("plan-run-phase")).toHaveText("Running", { timeout: 12_000 });
  await expect(banner.getByTestId("plan-run-banner-worker")).toContainText(live.worker.name);
  await expect(banner.getByTestId("plan-run-banner-working")).toContainText(`step 2: ${PLAN_RUN_TITLES["2"]}`, { timeout: 12_000 });

  // The agent asks a decision and waits: the banner says so and links to it.
  const decision = await askDecision(live, run.id, "Deploy the plan-runs build to staging now?");
  await reportState(live, run.id, { state: "waiting" });
  await expect(banner.getByTestId("plan-run-phase")).toHaveText("Waiting for your decision", { timeout: 12_000 });
  await expect(banner.getByTestId("plan-run-banner-decision")).toHaveAttribute("href", `/inbox?decision=${decision}`);
  await expect(banner.getByTestId("plan-run-banner-question")).toContainText("Deploy the plan-runs build to staging now?");
  await expect(banner.getByTestId("plan-run-banner-link")).toHaveAttribute("href", runPath(project, run.id));
  expect((await runOf(me, project, run.id)).state).toBe("waiting");

  // The list marks the plan with the run's state, and its Run plan is locked too.
  await open(page, `/p/${project}/plans`);
  const row = main(page).getByTestId("plans-table-active").locator("tbody tr").filter({ hasText: PLAN_RUN_PLAN });
  await expect(row.getByTestId("plan-run-phase")).toHaveText("Waiting for decision");
  await expect(row.getByTestId("plan-run-link")).toHaveAccessibleName(`Plan run #${run.id}: Waiting for your decision`);
  await expect(row.getByTestId("plans-run-plan")).toHaveAttribute("aria-disabled", "true");
  await expect(row.getByTestId("plans-run-plan-lock")).toHaveText(`Plan run #${run.id} is Waiting for decision.`);

  // The run's page says it is a plan run, with its plan's steps, repos and model.
  await open(page, runPath(project, run.id));
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText(`Run #${run.id}`);
  await expect(main(page).getByTestId("run-kind")).toHaveText("Plan run");
  await expect(main(page).getByTestId("run-state").first()).toHaveText("Waiting for decision");
  await expect(main(page).getByTestId("run-model")).toHaveText(MODELS[0]);
  await expect(main(page).getByTestId("run-repos").locator("li")).toHaveText([`${REPO}on ${PLAN_RUN_BRANCH}`, `${HARNESS_REPO}on main`]);
  const steps = main(page).getByTestId("run-plan-step");
  await expect(steps).toHaveCount(4);
  await expect(steps.nth(0)).toHaveAttribute("data-status", "done");
  await expect(steps.nth(1)).toHaveAttribute("data-status", "in_progress");
  await expect(steps.nth(1)).toHaveAttribute("aria-current", "step");
  await expect(main(page).getByTestId("run-plan-steps-count")).toHaveText("1 of 4 done (25%)");
  await expect(main(page).getByTestId("run-decision-link")).toHaveAttribute("href", `/inbox?decision=${decision}`);
  await expect(main(page).getByTestId("run-rerun")).toHaveCount(0);
});

test("Run plan and Run this step are locked while the plan has a plan run", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedPlanRunPlan(me, project);
  const { run } = await planRunUnderway(me, project, uniqueName("mini"));

  await open(page, PLAN_PATH(project));
  await expect(main(page).getByTestId("run-plan")).toHaveAttribute("data-locked", "planRun");
  await expect(main(page).getByTestId("run-plan")).toHaveAttribute("aria-disabled", "true");

  // A step the plan run holds cannot be dispatched on its own: its button is locked and names the run.
  await open(page, `${PLAN_PATH(project)}/steps/2`);
  const section = main(page).getByTestId("step-runs");
  await expect(section.getByTestId("step-run")).toHaveAttribute("aria-disabled", "true");
  await expect(section.getByTestId("step-run")).toHaveAttribute("data-locked", "planRun");
  await expect(section.getByTestId("step-plan-run")).toContainText(`Plan run #${run.id} (Running, dispatched by ${me.login}) holds every step of this plan`);
  await section.getByTestId("step-run").click({ force: true });
  await expect(page.getByTestId("dispatch-dialog")).toHaveCount(0);
  await open(page, `${PLAN_PATH(project)}/steps/4`);
  await expect(section.getByTestId("step-readiness")).toContainText("Not ready to run");
  await expect(section.getByTestId("step-run")).toHaveAttribute("aria-disabled", "true");
});

test("Run plan is locked while a step of the plan has a run, and when no step is pending", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const [stepRun] = await dispatch(me, project, ["2"]);
  await open(page, `/p/${project}/plans/rollout`);
  await expect(main(page).getByTestId("run-plan")).toHaveAttribute("data-locked", "stepRun");
  await expect(main(page).getByTestId("run-plan-lock")).toHaveText(`Run #${stepRun.id} of step 2 is Queued; a plan run waits until it ends.`);

  const done = "finished";
  await seedPlanRunPlan(me, project, done, { allDone: true });
  await open(page, `/p/${project}/plans/${done}`);
  await expect(main(page).getByTestId("run-plan")).toHaveAttribute("data-locked", "noPending");
  await expect(main(page).getByTestId("run-plan-lock")).toHaveText("No step is pending.");
});

test("plan run pages never scroll sideways at 375, 768 and 1024 px", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const LONG = "a-rather-long-unbroken-name-that-never-wraps-in-a-banner";
  await seedPlanRunPlan(me, project);
  const { run } = await planRunUnderway(me, project, `${LONG}-worker`, { waiting: true });
  await seedRunPlan(me, project); // a second plan whose Run plan is free, for the dialog
  for (const width of [375, 768, 1024]) {
    await page.setViewportSize({ width, height: 900 });
    for (const [path, ready] of [
      [`/p/${project}/plans`, "plans-table-active"],
      [PLAN_PATH(project), "plan-run-banner"],
      [`${PLAN_PATH(project)}/steps/2`, "step-plan-run"],
      [runPath(project, run.id), "run-plan-steps"],
    ] as const) {
      await open(page, path);
      await expect(main(page).getByTestId(ready).first()).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect.soft(overflow, `${path} at ${width} px scrolls sideways`).toBeLessThanOrEqual(0);
    }
    await open(page, `/p/${project}/plans/rollout`);
    await main(page).getByTestId("run-plan").click();
    const dialog = page.getByTestId("plan-run-dialog");
    await expect(dialog.getByTestId("plan-run-summary")).toBeVisible();
    const box = await dialog.boundingBox();
    expect.soft(box && box.x >= 0 && box.x + box.width <= width, `the dialog fits ${width} px`).toBe(true);
    const inner = await dialog.getByTestId("plan-run-body").evaluate((element) => element.scrollWidth - element.clientWidth);
    expect.soft(inner, `the dialog's body scrolls sideways at ${width} px`).toBeLessThanOrEqual(0);
    await page.keyboard.press("Escape");
  }
});
