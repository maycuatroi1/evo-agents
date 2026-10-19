import path from "node:path";

import { expect, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { AGENT_COMMIT, CHECKOUTS, heartbeat, NIGHTLY_COMMIT, registerWorker, RUNTIMES } from "./support/workers";

/**
 * Review screenshots of the workers list and a worker's page with what went wrong with its updates (an update that
 * failed after a long wait, a daemon that cannot update itself) beside workers whose updates go as they should, the
 * Version card and the Lock version switch, light and dark, desktop and 375 px, written to E2E_SCREENSHOT_DIR. Skipped
 * unless it is set; like screenshots.spec.ts it asserts nothing beyond the page being ready.
 */
const dir = process.env.E2E_SCREENSHOT_DIR;

test.describe("workers screenshots", () => {
  test.skip(!dir || Boolean(process.env.PLAYWRIGHT_BASE_URL), "set E2E_SCREENSHOT_DIR to take review screenshots");
  test.setTimeout(90_000);

  for (const scheme of ["light", "dark"] as const) {
    test(`workers pages in ${scheme}`, async ({ page, member }) => {
      await page.emulateMedia({ colorScheme: scheme });
      const me = await member([{ role: "writer", maxLevel: "internal" }]);
      const project = me.projects[0];
      const studio = await registerWorker(me, { name: uniqueName("studio"), projects: [project], slots: 2, labels: ["macos"] });
      await heartbeat(studio.id, {
        runtimes: RUNTIMES,
        checkouts: CHECKOUTS,
        agentCommit: AGENT_COMMIT,
        selfUpdate: {
          state: "waiting",
          nightly_commit: NIGHTLY_COMMIT,
          error: "uv tool install --force 'evo-ak[worker] @ git+https://github.com/maycuatroi1/evo-agents@nightly' exited 2: no matching distribution",
          error_at: new Date(Date.now() - 40 * 60_000).toISOString(),
        },
        selfUpdateHours: 14,
      });
      const laptop = await registerWorker(me, { name: uniqueName("laptop"), projects: [project] });
      await heartbeat(laptop.id, { runtimes: RUNTIMES, selfUpdate: { state: "unsupported", reason: "editable install of ~/github/evo-agents" } });
      const mini = await registerWorker(me, { name: uniqueName("mini"), projects: [project] });
      await heartbeat(mini.id, {
        runtimes: RUNTIMES,
        agentCommit: NIGHTLY_COMMIT,
        selfUpdate: { state: "current", nightly_commit: NIGHTLY_COMMIT, checked_at: new Date().toISOString() },
      });

      const main = page.locator("#main");
      for (const [width, height, suffix] of [
        [1440, 900, ""],
        [375, 812, "-375"],
      ] as const) {
        await page.setViewportSize({ width, height });
        await page.goto("/workers");
        await expect(main.getByTestId("worker-update")).toHaveCount(2);
        await page.screenshot({ path: path.join(dir ?? "", `workers-list${suffix}-${scheme}.png`), fullPage: true });

        await page.goto(`/workers/${studio.id}`);
        await expect(main.getByTestId("worker-update-failed")).toBeVisible();
        await expect(main.getByTestId("heartbeat-strip")).toBeVisible();
        await page.screenshot({ path: path.join(dir ?? "", `worker-updates${suffix}-${scheme}.png`), fullPage: true });
      }

      await page.setViewportSize({ width: 1920, height: 1080 });
      await page.goto(`/workers/${mini.id}`);
      await expect(main.getByTestId("worker-commit-sha")).toBeVisible();
      await page.screenshot({ path: path.join(dir ?? "", `worker-current-1920-${scheme}.png`), fullPage: true });
      await main.getByRole("switch", { name: "Lock version" }).click();
      await expect(main.getByTestId("worker-updates")).toHaveAttribute("data-state", "locked");
      await main.getByTestId("worker-version-lock").scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(dir ?? "", `worker-locked-1920-${scheme}.png`), fullPage: true });
    });
  }
});
