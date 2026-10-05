import path from "node:path";

import { expect, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { claimRun, dispatch, liveWorker, reportState, RUN_PLAN, seedRunPlan } from "./support/runs";

/**
 * Review screenshots of the runs pages and the Dispatch dialog, light and dark, desktop and 375 px, written to
 * E2E_SCREENSHOT_DIR. Skipped unless it is set; like screenshots.spec.ts it asserts nothing beyond the page being
 * ready.
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
});
