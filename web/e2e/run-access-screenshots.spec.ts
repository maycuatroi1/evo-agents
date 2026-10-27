import path from "node:path";

import { expect, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { askDecision, PLAN_RUN_PLAN, planRunUnderway, planRunWorker, reportState, runPath, seedPlanRunPlan } from "./support/runs";

/**
 * Review screenshots of a run that runs as its owner and of a credential request (docs/credentials.md): Run plan with
 * "Run as me on the hub" checked, the page of a plan run that runs as its owner with its Hub access card and Revoke's
 * confirmation, and the run's credential request in the Inbox's sheet on the value and on the account, light and dark,
 * 1440 and 375 px wide, written to E2E_SCREENSHOT_DIR. Skipped unless it is set; like the other screenshot specs it
 * asserts nothing beyond the page being ready.
 */
const dir = process.env.E2E_SCREENSHOT_DIR;

const SIZES = [
  [1440, 900, ""],
  [375, 812, "-375"],
] as const;

test.describe("run access screenshots", () => {
  test.skip(!dir || Boolean(process.env.PLAYWRIGHT_BASE_URL), "set E2E_SCREENSHOT_DIR to take review screenshots");
  test.setTimeout(180_000);

  for (const scheme of ["light", "dark"] as const) {
    test(`run access in ${scheme}`, async ({ page, member }) => {
      await page.emulateMedia({ colorScheme: scheme });
      const me = await member([{ role: "writer", maxLevel: "internal" }]);
      const project = me.projects[0];
      await seedPlanRunPlan(me, project);
      await planRunWorker(me, project, uniqueName("desk"));
      const main = page.locator("#main");

      for (const [width, height, suffix] of SIZES) {
        await page.setViewportSize({ width, height });
        await open(page, `/p/${project}/plans/${PLAN_RUN_PLAN}`);
        await main.getByTestId("run-plan").click();
        const dialog = page.getByTestId("plan-run-dialog");
        await dialog.getByTestId("dispatch-as-me-choice").click();
        await expect(dialog.getByTestId("dispatch-as-me-warning")).toBeVisible();
        await dialog.getByTestId("dispatch-as-me").scrollIntoViewIfNeeded();
        await page.screenshot({ path: path.join(dir!, `run-access-dispatch${suffix}-${scheme}.png`) });
        await page.keyboard.press("Escape");
      }

      const { live, run } = await planRunUnderway(me, project, uniqueName("studio"), { dispatch: { as_me: true } });
      const decision = await askDecision(live, run.id, "The deploy needs the Dokploy API key, and the hub to label the squad's chat. May I have both?", "2", {
        category: "credential",
        context: "The key is not in this worker's environment, and `evo-agents hub chat label` answered 401.",
        options: undefined,
        recommended: undefined,
        credential_request: { env_var: "NORTHWIND_DEPLOY_KEY", hub_account: true },
      });
      await reportState(live, run.id, { state: "waiting" });
      const sheet = page.getByTestId("decision-sheet");

      for (const [width, height, suffix] of SIZES) {
        await page.setViewportSize({ width, height });
        await open(page, runPath(project, run.id));
        const access = main.getByTestId("run-access");
        if (!(await access.isVisible())) await main.getByTestId("run-details-open").click();
        await expect(access).toBeVisible();
        await page.screenshot({ path: path.join(dir!, `run-access-run-page${suffix}-${scheme}.png`), fullPage: true });
        await access.scrollIntoViewIfNeeded();
        await page.screenshot({ path: path.join(dir!, `run-access-card${suffix}-${scheme}.png`) });
        await access.getByTestId("run-access-revoke").click();
        await expect(page.getByTestId("run-access-dialog")).toBeVisible();
        await page.screenshot({ path: path.join(dir!, `run-access-revoke${suffix}-${scheme}.png`) });
        await page.keyboard.press("Escape");

        await open(page, `/inbox?decision=${decision}`);
        await expect(sheet.getByTestId("credential-value")).toBeVisible();
        await sheet.getByTestId("credential-value").fill("nw-demo-value");
        await page.screenshot({ path: path.join(dir!, `credential-value${suffix}-${scheme}.png`) });
        await sheet.getByRole("radio", { name: /^Paste the value of NORTHWIND_DEPLOY_KEY and let the run use my hub account/ }).check();
        await expect(sheet.getByTestId("credential-account-note")).toBeVisible();
        await sheet.getByTestId("credential-account-note").scrollIntoViewIfNeeded();
        await page.screenshot({ path: path.join(dir!, `credential-both${suffix}-${scheme}.png`) });
      }
    });
  }
});
