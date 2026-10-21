import path from "node:path";

import {
  baselinePath,
  canvasesPath,
  canvasPath,
  claimPath,
  editByApi,
  METRIC,
  openBaselineByApi,
  pushConversions,
  seedCanvas,
  signalsPath,
  valueCheck,
} from "./support/canvas";
import { expect, test } from "./support/fixtures";
import { HubAdmin, newAccount } from "./support/hub";
import { open } from "./support/plans";

/**
 * Review screenshots of the Lean Canvas and Signals pages, light and dark, desktop and 375 px, written to
 * E2E_SCREENSHOT_DIR. Skipped unless it is set; it asserts nothing beyond the pages being ready.
 */
const dir = process.env.E2E_SCREENSHOT_DIR;

test.describe("Lean Canvas screenshots", () => {
  test.skip(!dir || Boolean(process.env.PLAYWRIGHT_BASE_URL), "set E2E_SCREENSHOT_DIR to take review screenshots");
  test.setTimeout(120_000);

  for (const scheme of ["light", "dark"] as const) {
    test(`Lean Canvas in ${scheme}`, async ({ page, member }) => {
      const shot = (name: string) => page.screenshot({ path: path.join(dir!, `canvas-${name}-${scheme}.png`), fullPage: true });
      await page.emulateMedia({ colorScheme: scheme });
      await page.setViewportSize({ width: 1440, height: 900 });
      const me = await member([{ role: "writer", maxLevel: "internal" }]);
      const [project] = me.projects;
      const owner = newAccount("canvas-owner");
      const hub = new HubAdmin();
      await hub.grant(project, owner.login, "admin", "internal");
      await seedCanvas(owner, project);
      await editByApi(owner, project, [
        { cell: "customer_segments", text: "Independent shops with fewer than ten staff" },
        { cell: "solution", text: "One-tap checkout that remembers the buyer" },
        { cell: "revenue_streams", text: "A fee of 0.5 % on each order", confidence: "medium" },
        { cell: "cost_structure", text: "Payment processing and two engineers" },
        { cell: "key_metrics", text: "Conversion from cart to order", signal_keys: [METRIC] },
      ]);
      await pushConversions(owner, project);
      await valueCheck(project);
      const number = await openBaselineByApi(owner, project, "CL-2");

      await open(page, canvasesPath(project));
      await expect(page.getByTestId("canvases-table")).toBeVisible();
      await shot("list");
      await open(page, canvasPath(project));
      await expect(page.getByTestId("canvas-grid")).toBeVisible();
      await shot("grid");
      await open(page, claimPath(project, "CL-1"));
      await expect(page.getByTestId("evidence-basis")).toBeVisible();
      await shot("claim");
      await page.getByTestId("claim-edit").click();
      await expect(page.getByTestId("claim-editor")).toBeVisible();
      await shot("claim-editor");
      await open(page, baselinePath(project, number));
      await expect(page.getByTestId("baseline-decide")).toBeVisible();
      await shot("baseline");
      await open(page, signalsPath(project, `?key=${METRIC}`));
      await expect(page.getByTestId("series-chart")).toBeVisible();
      await shot("signals");

      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, canvasPath(project));
      await expect(page.getByTestId("canvas-grid")).toBeVisible();
      await shot("grid-phone");
      await open(page, claimPath(project, "CL-1"));
      await shot("claim-phone");
      await open(page, signalsPath(project, `?key=${METRIC}`));
      await shot("signals-phone");
    });
  }
});
