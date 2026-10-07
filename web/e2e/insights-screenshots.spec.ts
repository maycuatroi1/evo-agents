import path from "node:path";

import { expect, test } from "./support/fixtures";
import { seedInsights } from "./support/insights";
import { open } from "./support/plans";

/**
 * Review screenshots of a project's Insights: the four charts, a chart's tooltip reached with the keyboard and the
 * tables, light and dark, at 1440 and 375 px, written to E2E_SCREENSHOT_DIR. Skipped unless it is set; like
 * screenshots.spec.ts it asserts nothing beyond the page being ready.
 */
const dir = process.env.E2E_SCREENSHOT_DIR;
const CHARTS = ["outcomes", "failure", "duration", "tokens"] as const;

test.describe("insights screenshots", () => {
  test.skip(!dir || Boolean(process.env.PLAYWRIGHT_BASE_URL), "set E2E_SCREENSHOT_DIR to take review screenshots");
  test.setTimeout(120_000);

  for (const scheme of ["light", "dark"] as const) {
    test(`insights in ${scheme}`, async ({ page, member }) => {
      await page.emulateMedia({ colorScheme: scheme });
      const me = await member([{ role: "writer", maxLevel: "internal" }]);
      const project = me.projects[0];
      await seedInsights(me, project);
      const main = page.locator("#main");
      const ready = async () => {
        for (const chart of CHARTS) await expect(main.getByTestId(`insights-${chart}-chart`).locator("svg.recharts-surface")).toBeVisible();
      };

      for (const [width, suffix] of [
        [1440, ""],
        [375, "-375"],
      ] as const) {
        await page.setViewportSize({ width, height: 1100 });
        await open(page, `/p/${project}/insights?days=7`);
        await ready();
        await page.screenshot({ path: path.join(dir!, `insights${suffix}-${scheme}.png`), fullPage: true });

        // The keyboard's tooltip on the day with the most runs.
        await main.getByTestId("insights-outcomes-view-table").focus();
        await page.keyboard.press("Tab");
        for (let step = 0; step < 4; step += 1) await page.keyboard.press("ArrowRight");
        await expect(main.getByTestId("insights-outcomes").getByTestId("insights-tooltip")).toBeVisible();
        await main.getByTestId("insights-outcomes").screenshot({ path: path.join(dir!, `insights-tooltip${suffix}-${scheme}.png`) });

        for (const chart of CHARTS) await main.getByTestId(`insights-${chart}-view-table`).click();
        await expect(main.getByTestId("insights-tokens-table-region")).toBeVisible();
        await page.screenshot({ path: path.join(dir!, `insights-tables${suffix}-${scheme}.png`), fullPage: true });
      }

      await page.setViewportSize({ width: 1440, height: 1100 });
      await open(page, `/p/${project}/insights?days=90`);
      await ready();
      await page.screenshot({ path: path.join(dir!, `insights-90-${scheme}.png`), fullPage: true });
    });
  }
});
