import path from "node:path";

import { expect, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { askDecision, planRunUnderway, reportState, runPath, say, seedPlanRunPlan, sendEvents, tool } from "./support/runs";

/**
 * Review screenshots of the kit's DecisionCard: in the Inbox's sheet (open, its context unfolded, answered), at 375 px
 * the kit's MobileDecision screen instead (also with what the agent did so far unfolded, scrolled to its foot), and in
 * the side column of a plan run's page, light and dark, desktop and 375 px, written to E2E_SCREENSHOT_DIR. Skipped
 * unless it is set; like screenshots.spec.ts it asserts nothing beyond the page being ready.
 */
const dir = process.env.E2E_SCREENSHOT_DIR;

const CONTEXT = [
  "Tests and the build passed on **binhna-macbook-m4** (hub 48 s, worker 31 s); the wheel and the sdist are in `dist/`.",
  "Publishing cannot be undone: PyPI keeps a version number forever, even after a yank.",
  "Step 14 still changes the worker's daemon, so a fix found there would need 0.4.1.",
  "The changelog lists plan runs, decisions and the Inbox.",
  "The deploy of the hub waits for the package either way.",
].join("\n\n");

test.describe("decision screenshots", () => {
  test.skip(!dir || Boolean(process.env.PLAYWRIGHT_BASE_URL), "set E2E_SCREENSHOT_DIR to take review screenshots");
  test.setTimeout(120_000);

  for (const scheme of ["light", "dark"] as const) {
    test(`decision card in ${scheme}`, async ({ page, member }) => {
      await page.emulateMedia({ colorScheme: scheme });
      const me = await member([{ role: "writer", maxLevel: "internal" }]);
      const project = me.projects[0];
      await seedPlanRunPlan(me, project);
      const { live, run } = await planRunUnderway(me, project, uniqueName("studio"));
      await sendEvents(live, run.id, [say("Bumped the version to **0.4.0** and ran the tests."), tool("Bash", "uv build")]);
      const decision = await askDecision(live, run.id, "Publish evo-agents 0.4.0 to PyPI now?", "3", {
        context: CONTEXT,
        options: [
          { key: "publish", label: "Publish now", description: "Upload 0.4.0, tag v0.4.0, then go on with step 14." },
          { key: "hold", label: "Hold until step 14 is done", description: "Keep the build, skip the upload, go on with step 14." },
          { key: "stop", label: "Stop the plan run", description: "Leave the branch as it is. You publish by hand." },
        ],
        recommended: "publish",
      });
      await reportState(live, run.id, { state: "waiting" });
      const sheet = page.getByTestId("decision-sheet");

      for (const [width, height, suffix] of [
        [1440, 900, ""],
        [375, 812, "-375"],
      ] as const) {
        await page.setViewportSize({ width, height });
        await open(page, `/inbox?decision=${decision}`);
        await expect(sheet.getByTestId("decision-parks")).toBeVisible();
        await expect(sheet.getByTestId("decision-context-toggle")).toBeVisible();
        await page.screenshot({ path: path.join(dir!, `decision-sheet${suffix}-${scheme}.png`) });
        await sheet.getByTestId("decision-context-toggle").click();
        await page.screenshot({ path: path.join(dir!, `decision-sheet-context${suffix}-${scheme}.png`) });
        if (width === 375) {
          const soFar = sheet.getByTestId("decision-so-far");
          await soFar.locator("summary").click();
          await expect(soFar.getByTestId("decision-so-far-item").first()).toBeVisible();
          await sheet.getByTestId("decision-panel").evaluate((element) => element.scrollTo({ top: element.scrollHeight }));
          await page.screenshot({ path: path.join(dir!, `decision-screen-so-far-375-${scheme}.png`) });
        }
        await open(page, runPath(project, run.id));
        await expect(page.locator("#main").getByTestId("run-decisions").getByTestId("decision-form")).toBeVisible();
        await page.screenshot({ path: path.join(dir!, `decision-run-page${suffix}-${scheme}.png`), fullPage: true });
      }

      await page.setViewportSize({ width: 1440, height: 900 });
      await open(page, `/inbox?decision=${decision}`);
      await sheet.getByTestId("decision-text").fill("Publish, then note the version in the plan's evidence.");
      await sheet.getByTestId("decision-send").click();
      await expect(sheet.getByTestId("decision-answer")).toBeVisible();
      await page.screenshot({ path: path.join(dir!, `decision-answered-${scheme}.png`) });
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, `/inbox?decision=${decision}`);
      await expect(sheet.getByTestId("decision-answer")).toBeVisible();
      await page.screenshot({ path: path.join(dir!, `decision-answered-375-${scheme}.png`) });
    });
  }
});
