import path from "node:path";

import { signOut } from "./support/auth";
import { expect, test } from "./support/fixtures";
import { newAccount, registration, uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { claimRun, dispatch, liveWorker, planRunUnderway, reportState, seedPlanRunPlan, seedRunPlan, startRun } from "./support/runs";

/**
 * Review screenshots of the shell and Home, light and dark, written to E2E_SCREENSHOT_DIR. Skipped unless it is set; it
 * asserts nothing beyond the page being ready, so it never belongs in a verify command.
 */
const dir = process.env.E2E_SCREENSHOT_DIR;

test.describe("screenshots", () => {
  test.skip(!dir || Boolean(process.env.PLAYWRIGHT_BASE_URL), "set E2E_SCREENSHOT_DIR to take review screenshots");

  for (const scheme of ["light", "dark"] as const) {
    test(`shell in ${scheme}`, async ({ page, admin, signInAs }) => {
      await page.emulateMedia({ colorScheme: scheme });
      await page.setViewportSize({ width: 1440, height: 900 });
      const account = newAccount("reviewer");
      const main = uniqueName("atlas");
      await admin.registerProject(main, {
        repos: [
          { name: "atlas-api", origin: "https://github.com/example-org/atlas-api", default_branch: "main", path: "atlas-api" },
          { name: "atlas-web", origin: "git@github.com:example-org/atlas-web.git", default_branch: "main", path: "atlas-web" },
          { name: "atlas-harness", origin: "https://github.com/example-org/atlas-harness", default_branch: "develop", path: "atlas-harness" },
        ],
      });
      await admin.grant(main, account.login, "writer", "customer");
      for (const [prefix, role, level] of [
        ["harbor", "reader", "internal"],
        ["orbit", "admin", "secret"],
      ] as const) {
        const name = uniqueName(prefix);
        await admin.registerProject(name, registration());
        await admin.grant(name, account.login, role, level);
      }
      await signInAs(account);
      await expect(page.locator("#main").getByTestId("home-projects")).toBeVisible();
      await page.screenshot({ path: path.join(dir!, `shell-home-${scheme}.png`) });
      await page.goto(`/p/${main}`);
      await expect(page.getByTestId("label-ladder")).toBeVisible();
      await page.screenshot({ path: path.join(dir!, `shell-project-${scheme}.png`), fullPage: true });
      await page.setViewportSize({ width: 375, height: 812 });
      await page.goto("/");
      await expect(page.locator("#main").getByTestId("home-projects")).toBeVisible();
      await page.screenshot({ path: path.join(dir!, `shell-home-mobile-${scheme}.png`) });
      await signOut(page);
      await page.goto("/login");
      await page.setViewportSize({ width: 1440, height: 900 });
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await page.screenshot({ path: path.join(dir!, `login-${scheme}.png`) });
    });

    test(`home in ${scheme}`, async ({ page, member }) => {
      await page.emulateMedia({ colorScheme: scheme });
      const me = await member([
        { role: "writer", maxLevel: "internal" },
        { role: "reader", maxLevel: "public" },
      ]);
      const project = me.projects[0];
      // A plan run waiting for the member, a step at work, one queued and one that failed.
      await seedPlanRunPlan(me, project);
      await planRunUnderway(me, project, uniqueName("macbook"), { waiting: true });
      await seedRunPlan(me, project);
      const live = await liveWorker(me, project, uniqueName("mini"));
      const [failing, working] = await dispatch(me, project, ["2", "4"]);
      await claimRun(live);
      await reportState(live, failing.id, { state: "failed", error: "The agent did not write .evo-run/result.json" });
      await claimRun(live);
      await startRun(live, working.id);
      for (const [width, name] of [
        [1440, "home"],
        [1024, "home-1024"],
        [768, "home-768"],
        [375, "home-mobile"],
      ] as const) {
        await page.setViewportSize({ width, height: 900 });
        await open(page, "/");
        await expect(page.locator("#main").getByTestId("needs-you-item")).toHaveCount(1);
        await expect(page.locator("#main").getByTestId("fleet-worker")).toHaveCount(2);
        await expect(page.locator("#main").getByTestId("sparkline-chart")).toBeVisible();
        await page.screenshot({ path: path.join(dir!, `${name}-${scheme}.png`), fullPage: true });
      }
      await page.setViewportSize({ width: 1440, height: 900 });
      await open(page, "/");
      await page.getByTestId("project-switcher").click();
      await expect(page.getByTestId("project-switcher-menu").locator("[data-project]")).toHaveCount(2);
      await page.screenshot({ path: path.join(dir!, `home-switcher-${scheme}.png`) });
      await page.keyboard.press("Escape");
      await page.locator("#main").getByTestId("needs-you-answer").click();
      await expect(page.getByTestId("decision-sheet").getByTestId("decision-form")).toBeVisible();
      await page.screenshot({ path: path.join(dir!, `home-sheet-${scheme}.png`) });
    });
  }
});
