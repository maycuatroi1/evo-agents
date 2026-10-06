import path from "node:path";

import { expect, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import {
  claimRun,
  dispatch,
  liveWorker,
  PLAN_RUN_PLAN,
  planRunUnderway,
  reportState,
  RUN_PLAN,
  runPath,
  runToReview,
  say,
  seedRunPlan,
  seedPlanRunPlan,
  sendEvents,
  startRun,
  tool,
  uploadDiff,
  workerHeartbeat,
  type WorkerEvent,
} from "./support/runs";
import { startFakeTerminal } from "./support/terminal";

/**
 * Review screenshots of the runs pages, the Dispatch dialog and a run's page (live, in review, its diff, the Take
 * over dialog and the Terminal tab), and of plan runs (the Run plan dialog, the plan page's banner, the plans list and
 * a plan run's page), light and dark, desktop and 375 px, written to E2E_SCREENSHOT_DIR. Skipped unless it is set; like
 * screenshots.spec.ts it asserts nothing beyond the page being ready.
 */
const dir = process.env.E2E_SCREENSHOT_DIR;

test.describe("runs screenshots", () => {
  test.skip(!dir || Boolean(process.env.PLAYWRIGHT_BASE_URL), "set E2E_SCREENSHOT_DIR to take review screenshots");
  test.setTimeout(120_000);

  for (const scheme of ["light", "dark"] as const) {
    test(`runs pages in ${scheme}`, async ({ page, member }) => {
      await page.emulateMedia({ colorScheme: scheme });
      const me = await member([{ role: "writer", maxLevel: "internal" }]);
      const project = me.projects[0];
      await seedRunPlan(me, project);
      const live = await liveWorker(me, project, uniqueName("laptop"), 2);
      const [failed] = await dispatch(me, project, ["2"]);
      await claimRun(live);
      await reportState(live, failed.id, { state: "failed", error: "verify failed: pnpm test exited 1" });
      await dispatch(me, project, ["2"], { runtime: "claude-code" });
      await claimRun(live);
      await dispatch(me, project, ["4"], { approval: "auto" });

      // The dialog on a fresh plan: ready steps, one picked, the whole form on a tall window.
      const fresh = uniqueName("fresh");
      await seedRunPlan(me, project, fresh);
      await page.setViewportSize({ width: 1440, height: 1700 });
      await open(page, `/p/${project}/runs`);
      await page.locator("#main").getByTestId("runs-dispatch").click();
      const form = page.getByTestId("dispatch-dialog");
      await form.getByTestId("dispatch-plan").selectOption(fresh);
      await form.getByTestId("dispatch-step-2").click();
      await expect(form.getByTestId("dispatch-outlook")).toHaveAttribute("data-kind", "now");
      await page.screenshot({ path: path.join(dir!, `dispatch-ready-${scheme}.png`) });
      await page.keyboard.press("Escape");

      for (const [width, height, suffix] of [
        [1440, 900, ""],
        [375, 812, "-375"],
      ] as const) {
        await page.setViewportSize({ width, height });
        const shot = (name: string) => page.screenshot({ path: path.join(dir!, `${name}${suffix}-${scheme}.png`), fullPage: true });
        await open(page, `/p/${project}/runs`);
        await expect(page.locator("#main").getByTestId("runs-table")).toBeVisible();
        await shot("runs");
        await page.locator("#main").getByTestId("runs-dispatch").click();
        const dialog = page.getByTestId("dispatch-dialog");
        await dialog.getByTestId("dispatch-settled").locator("summary").click();
        await expect(dialog.getByTestId("dispatch-outlook")).toBeVisible();
        await page.screenshot({ path: path.join(dir!, `dispatch${suffix}-${scheme}.png`) });
        await page.keyboard.press("Escape");
        await open(page, `/p/${project}/plans/${RUN_PLAN}/steps/2`);
        await expect(page.locator("#main").getByTestId("step-runs-table")).toBeVisible();
        await shot("run-step");
        await open(page, `/workers/${live.worker.id}`);
        await expect(page.locator("#main").getByTestId("worker-runs-table")).toBeVisible();
        await shot("worker-runs");
      }
    });
  }

  for (const scheme of ["light", "dark"] as const) {
    test(`plan runs in ${scheme}`, async ({ page, member }) => {
      await page.emulateMedia({ colorScheme: scheme });
      const me = await member([{ role: "writer", maxLevel: "internal" }]);
      const project = me.projects[0];
      await seedPlanRunPlan(me, project);
      const { run } = await planRunUnderway(me, project, uniqueName("studio"), { waiting: true });
      await seedPlanRunPlan(me, project, "fresh-plan");
      const main = page.locator("#main");

      // The dialog on a plan nobody runs yet, the whole form on a tall window.
      await page.setViewportSize({ width: 1440, height: 1700 });
      await open(page, `/p/${project}/plans/fresh-plan`);
      await main.getByTestId("run-plan").click();
      const dialog = page.getByTestId("plan-run-dialog");
      await expect(dialog.getByTestId("plan-run-summary")).toBeVisible();
      // The worker's one slot holds the other plan's run: the footer says the new one waits for it.
      await expect(dialog.getByTestId("dispatch-outlook")).toHaveAttribute("data-kind", "later");
      await page.screenshot({ path: path.join(dir!, `plan-run-dialog-${scheme}.png`) });
      await dialog.getByTestId("plan-run-model-input").fill("sonnet");
      await expect(dialog.getByTestId("plan-run-model-error")).toBeVisible();
      await page.screenshot({ path: path.join(dir!, `plan-run-dialog-model-${scheme}.png`) });
      await page.keyboard.press("Escape");

      for (const [width, height, suffix] of [
        [1440, 1000, ""],
        [375, 812, "-375"],
      ] as const) {
        await page.setViewportSize({ width, height });
        const shot = (name: string) => page.screenshot({ path: path.join(dir!, `${name}${suffix}-${scheme}.png`), fullPage: true });
        await open(page, `/p/${project}/plans/${PLAN_RUN_PLAN}`);
        await expect(main.getByTestId("plan-run-banner")).toHaveAttribute("data-phase", "waiting");
        await expect(main.getByTestId("plan-run-banner-decision")).toBeVisible();
        await shot("plan-run-banner");
        await open(page, `/p/${project}/plans`);
        await expect(main.getByTestId("plan-run-link")).toBeVisible();
        await shot("plans-with-plan-run");
        await open(page, runPath(project, run.id));
        await expect(main.getByTestId("run-plan-step")).toHaveCount(4);
        await shot("plan-run-page");
        if (suffix) {
          await open(page, `/p/${project}/plans/fresh-plan`);
          await main.getByTestId("run-plan").click();
          await expect(page.getByTestId("plan-run-dialog").getByTestId("plan-run-summary")).toBeVisible();
          await page.screenshot({ path: path.join(dir!, `plan-run-dialog${suffix}-${scheme}.png`) });
          await page.keyboard.press("Escape");
        }
      }
    });
  }

  /** A log with a line of each kind a worker sends. */
  const SESSION: WorkerEvent[] = [
    { kind: "system", body: { text: "Created worktree ~/.evo/worker/worktrees/demo-12 on branch feat/rollout" } },
    say("Reading plan rollout. Step 2 depends on step 1, which is done."),
    { kind: "agent_thought_chunk", body: { text: "The queue needs a lease column before claim can use SKIP LOCKED." } },
    { kind: "plan", body: { entries: [{ content: "Add the lease column", status: "completed" }, { content: "Write claim", status: "in_progress" }] } },
    tool("Bash", "pytest -q tests/hub/test_queue.py"),
    { kind: "tool_call_update", body: { status: "failed", rawOutput: "FAILED test_lease_expiry_requeues: assert 'queued' == 'leased'" } },
    say("The fake clock never advanced. Moving it forward 301 s."),
    { kind: "tool_call_update", body: { status: "completed", rawOutput: "10 passed in 3.41s" } },
    { kind: "usage_update", body: { input_tokens: 41200, output_tokens: 6800 } },
    { kind: "system", body: { text: "verify: `pnpm test` exited 0 after 1400 ms", exit_code: 0, output: "Tests 57 passed (57)" } },
  ];

  for (const scheme of ["light", "dark"] as const) {
    test(`run page in ${scheme}`, async ({ page, member }) => {
      await page.emulateMedia({ colorScheme: scheme });
      const me = await member([{ role: "writer", maxLevel: "internal" }]);
      const project = me.projects[0];
      await seedRunPlan(me, project);
      const live = await liveWorker(me, project, uniqueName("laptop"), 2);
      const [running, review] = await dispatch(me, project, ["2", "4"]);
      await claimRun(live);
      await claimRun(live);
      await startRun(live, running.id);
      await sendEvents(live, running.id, SESSION);
      await startRun(live, review.id);
      await sendEvents(live, review.id, SESSION);
      await runToReview(live, review.id);
      await uploadDiff(
        live,
        review.id,
        ["diff --git a/src/queue.ts b/src/queue.ts", "--- a/src/queue.ts", "+++ b/src/queue.ts", "@@ -1,3 +1,4 @@ export class Queue {", " import { db } from './db';", "-export function claim() {}", "+export function claim(worker: string) {", "+  return db.lease(worker);", " }", ""].join("\n"),
      );

      for (const [width, height, suffix] of [
        [1440, 1000, ""],
        [375, 812, "-375"],
      ] as const) {
        await page.setViewportSize({ width, height });
        const shot = (name: string) => page.screenshot({ path: path.join(dir!, `${name}${suffix}-${scheme}.png`), fullPage: true });
        await open(page, runPath(project, running.id));
        await expect(page.locator("#main").getByTestId("log-status")).toHaveAttribute("data-status", "live");
        await expect(page.locator("#main").getByTestId("log-line")).toHaveCount(SESSION.length + 2);
        await shot("run-live");
        await page.locator("#main").getByTestId("run-takeover").click();
        await expect(page.getByTestId("takeover-dialog")).toBeVisible();
        await page.screenshot({ path: path.join(dir!, `run-takeover${suffix}-${scheme}.png`) });
        await page.keyboard.press("Escape");
        await open(page, runPath(project, review.id));
        await expect(page.locator("#main").getByTestId("run-verify")).toBeVisible();
        await expect(page.locator("#main").getByTestId("log-line")).toHaveCount(SESSION.length + 4);
        await shot("run-review");
        await open(page, `${runPath(project, review.id)}/diff`);
        await expect(page.locator("#main").getByTestId("diff-file")).toHaveCount(1);
        await shot("run-diff");
      }
    });
  }

  for (const scheme of ["light", "dark"] as const) {
    test(`terminal tab in ${scheme}`, async ({ page, member }) => {
      await page.emulateMedia({ colorScheme: scheme });
      const me = await member([{ role: "writer", maxLevel: "internal" }]);
      const project = me.projects[0];
      await seedRunPlan(me, project);
      const live = await liveWorker(me, project, uniqueName("laptop"), 2, { terminal: true });
      const [headless, interactive] = await dispatch(me, project, ["2", "4"]);
      await claimRun(live);
      await claimRun(live);
      await workerHeartbeat(live, [headless.id, interactive.id]);
      await startRun(live, headless.id);
      await reportState(live, interactive.id, { state: "interactive" });

      for (const [width, height, suffix] of [
        [1440, 1000, ""],
        [375, 812, "-375"],
      ] as const) {
        await page.setViewportSize({ width, height });
        const main = page.locator("#main");
        const shot = (name: string) => page.screenshot({ path: path.join(dir!, `${name}${suffix}-${scheme}.png`), fullPage: true });
        await open(page, runPath(project, headless.id));
        await main.getByRole("tab", { name: "Terminal" }).click();
        await expect(main.getByTestId("terminal-intro")).toBeVisible();
        await shot("run-terminal-idle");

        await open(page, runPath(project, interactive.id));
        await main.getByRole("tab", { name: "Terminal" }).click();
        await main.getByTestId("terminal-connect").click();
        await expect(main.getByTestId("terminal-status")).toHaveAttribute("data-status", "waiting");
        await shot("run-terminal-waiting");
        await startFakeTerminal(interactive.id, live.token);
        await expect(main.getByTestId("terminal-screen")).toContainText("fake worker terminal");
        await page.keyboard.type("ls -la ~/.evo/worker/worktrees");
        await page.keyboard.press("Enter");
        await page.keyboard.type("xin chào tiếng Việt");
        await expect(main.getByTestId("terminal-screen")).toContainText("xin chào tiếng Việt");
        await shot("run-terminal-live");

        const other = await page.context().newPage();
        await other.setViewportSize({ width, height });
        await other.emulateMedia({ colorScheme: scheme });
        await open(other, runPath(project, interactive.id));
        await other.locator("#main").getByRole("tab", { name: "Terminal" }).click();
        await other.locator("#main").getByTestId("terminal-connect").click();
        await expect(other.locator("#main").getByTestId("terminal-message")).toHaveAttribute("data-kind", "busy");
        await other.screenshot({ path: path.join(dir!, `run-terminal-busy${suffix}-${scheme}.png`), fullPage: true });
        await other.close();
        await main.getByTestId("terminal-disconnect").click();
        await expect(main.getByTestId("terminal-status")).toHaveAttribute("data-status", "closed");
      }
    });
  }
});
