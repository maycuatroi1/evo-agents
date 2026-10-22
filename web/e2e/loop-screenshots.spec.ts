import path from "node:path";

import { expect, test } from "./support/fixtures";
import { decidePath, goalsPath, itemPath, loopPath, seedLoop, seedLoopProject } from "./support/loop";
import { open } from "./support/plans";

/**
 * Review screenshots of the Goals, Loop, chain and Decisions pages, light and dark, desktop and 375 px, written to
 * E2E_SCREENSHOT_DIR. Skipped unless it is set; it asserts nothing beyond the pages being ready.
 */
const dir = process.env.E2E_SCREENSHOT_DIR;

test.describe("Loop screenshots", () => {
  test.skip(!dir || Boolean(process.env.PLAYWRIGHT_BASE_URL), "set E2E_SCREENSHOT_DIR to take review screenshots");
  test.setTimeout(120_000);

  for (const scheme of ["light", "dark"] as const) {
    test(`Loop pages in ${scheme}`, async ({ page, member }) => {
      const me = await member([{ role: "admin", maxLevel: "internal" }]);
      const [project] = me.projects;
      await seedLoopProject(me, project);
      const seeded = await seedLoop(project, { tripped: true });
      await page.emulateMedia({ colorScheme: scheme });
      for (const [width, height, size] of [
        [1440, 900, "desktop"],
        [375, 812, "phone"],
      ] as const) {
        await page.setViewportSize({ width, height });
        const shot = (name: string) => page.screenshot({ path: path.join(dir!, `loop-${name}-${size}-${scheme}.png`), fullPage: true });
        await open(page, goalsPath(project));
        await expect(page.locator("#main").getByTestId("goals-table")).toBeVisible();
        await shot("goals");
        await open(page, goalsPath(project, "G-1"));
        await expect(page.locator("#main").getByTestId("goal-items-table")).toBeVisible();
        await shot("goal");
        await open(page, loopPath(project));
        await expect(page.locator("#main").getByTestId("loop-items-table")).toBeVisible();
        await shot("loop");
        await open(page, loopPath(project, "?view=chain"));
        await expect(page.locator("#main").getByTestId("loop-chains")).toBeVisible();
        await shot("loop-chains");
        await open(page, itemPath(project, seeded.items.build));
        await expect(page.locator("#main").getByTestId("chain-rail")).toBeVisible();
        await shot("chain");
        await open(page, itemPath(project, seeded.items.held));
        await expect(page.locator("#main").getByTestId("chain-waiting")).toBeVisible();
        await shot("chain-held");
        await open(page, decidePath(project));
        await expect(page.locator("#main").getByTestId("board-summary")).toBeVisible();
        await shot("decide");
      }
    });
  }
});
