import path from "node:path";

import { expect, test } from "./support/fixtures";
import { ACTIVE_PLAN, EVIDENCE_STEP, open, seedPlans } from "./support/plans";

/**
 * Review screenshots of the plan pages, light and dark, desktop and 375 px, written to E2E_SCREENSHOT_DIR. Skipped
 * unless it is set; like screenshots.spec.ts it asserts nothing beyond the page being ready.
 */
const dir = process.env.E2E_SCREENSHOT_DIR;

test.describe("plan screenshots", () => {
  test.skip(!dir || Boolean(process.env.PLAYWRIGHT_BASE_URL), "set E2E_SCREENSHOT_DIR to take review screenshots");
  test.setTimeout(120_000);

  for (const scheme of ["light", "dark"] as const) {
    test(`plan pages in ${scheme}`, async ({ page, member }) => {
      await page.emulateMedia({ colorScheme: scheme });
      const me = await member([{ role: "writer", maxLevel: "internal" }]);
      const project = me.projects[0];
      await seedPlans(me, project);
      const shots: [string, string, () => Promise<void>][] = [
        ["plans-list", `/p/${project}/plans`, () => expect(page.getByTestId("plans-table-active")).toBeVisible()],
        ["plan-board", `/p/${project}/plans/${ACTIVE_PLAN}`, () => expect(page.getByTestId("step-board")).toBeVisible()],
        [
          "plan-step",
          `/p/${project}/plans/${ACTIVE_PLAN}/steps/${EVIDENCE_STEP}`,
          () => expect(page.getByTestId("step-evidence")).toBeVisible(),
        ],
        [
          "plan-diff",
          `/p/${project}/plans/${ACTIVE_PLAN}/revisions?from=1&to=2`,
          () => expect(page.getByTestId("diff-line").first()).toBeVisible(),
        ],
      ];
      for (const [width, height, suffix] of [
        [1440, 900, ""],
        [375, 812, "-375"],
      ] as const) {
        await page.setViewportSize({ width, height });
        for (const [name, url, ready] of shots) {
          await open(page, url);
          await ready();
          await page.screenshot({ path: path.join(dir!, `${name}${suffix}-${scheme}.png`), fullPage: true });
        }
      }
      await page.setViewportSize({ width: 1440, height: 900 });
      await open(page, `/p/${project}/plans/${ACTIVE_PLAN}/revisions?from=1&to=2`);
      await page.getByTestId("diff-mode-split").click();
      await expect(page.getByTestId("diff-mode-split")).toHaveAttribute("aria-pressed", "true");
      await page.screenshot({ path: path.join(dir!, `plan-diff-split-${scheme}.png`), fullPage: true });
      await open(page, `/p/${project}/plans/${ACTIVE_PLAN}`);
      await page.getByTestId("steps-view-list").click();
      await expect(page.getByTestId("steps-table")).toBeVisible();
      await page.screenshot({ path: path.join(dir!, `plan-list-view-${scheme}.png`), fullPage: true });
    });
  }
});
