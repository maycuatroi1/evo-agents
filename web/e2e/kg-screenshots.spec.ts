import path from "node:path";

import { expect, test } from "./support/fixtures";
import { newAccount } from "./support/hub";
import { graphReady, HUB_NODE, kgPath, kgProject, nodePath, open, queueBuild, SHARED_NODE, sharedKg } from "./support/kg";

/**
 * Review screenshots of the knowledge graph pages, light and dark, desktop and 375 px, written to
 * E2E_SCREENSHOT_DIR. Skipped unless it is set; it asserts nothing beyond the pages being ready.
 */
const dir = process.env.E2E_SCREENSHOT_DIR;

test.describe("knowledge graph screenshots", () => {
  test.skip(!dir || Boolean(process.env.PLAYWRIGHT_BASE_URL), "set E2E_SCREENSHOT_DIR to take review screenshots");
  test.setTimeout(120_000);

  for (const scheme of ["light", "dark"] as const) {
    test(`knowledge graph in ${scheme}`, async ({ page, admin, signInAs }) => {
      const shot = (name: string, fullPage = true) =>
        page.screenshot({ path: path.join(dir!, `kg-${name}-${scheme}.png`), fullPage });
      await page.emulateMedia({ colorScheme: scheme });
      await page.setViewportSize({ width: 1440, height: 900 });
      const kg = await sharedKg();
      const status = await kgProject(admin, "kg-status");
      await queueBuild(status.project, status.writer, true);
      await queueBuild(status.project, status.writer, false);
      const reviewer = newAccount("kg-reviewer");
      await admin.grant(kg.project, reviewer.login, "writer", "customer");
      await admin.grant(status.project, reviewer.login, "reader", "internal");
      await signInAs(reviewer);

      await open(page, kgPath(kg.project));
      await expect(page.getByTestId("kg-kinds")).toBeVisible();
      await shot("overview");
      await open(page, `${kgPath(kg.project)}?q=KB-01`);
      await expect(page.getByTestId("kg-results")).toBeVisible();
      await shot("search");
      await open(page, kgPath(status.project));
      await expect(page.getByTestId("queue-job")).toBeVisible();
      await shot("status-queued-failed");
      await open(page, nodePath(kg.project, SHARED_NODE));
      await graphReady(page);
      await shot("node");
      await open(page, nodePath(kg.project, HUB_NODE));
      await graphReady(page);
      await shot("node-truncated", false);

      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, `${kgPath(kg.project)}?q=runbook`);
      await expect(page.getByTestId("kg-results")).toBeVisible();
      await shot("search-mobile");
      await open(page, nodePath(kg.project, SHARED_NODE));
      await expect(page.getByTestId("kg-neighbours")).toBeVisible();
      await shot("node-mobile");
    });
  }
});
