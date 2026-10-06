import path from "node:path";

import { expect, test } from "./support/fixtures";
import { newAccount, registration, uniqueName } from "./support/hub";

/**
 * Review screenshots of the shell, light and dark, written to E2E_SCREENSHOT_DIR. Skipped unless it is set; it
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
      await expect(page.getByTestId("projects-table")).toBeVisible();
      await page.screenshot({ path: path.join(dir!, `shell-home-${scheme}.png`) });
      await page.goto(`/p/${main}`);
      await expect(page.getByTestId("label-ladder")).toBeVisible();
      await page.screenshot({ path: path.join(dir!, `shell-project-${scheme}.png`), fullPage: true });
      await page.setViewportSize({ width: 375, height: 812 });
      await page.goto("/");
      await expect(page.getByTestId("projects-table")).toBeVisible();
      await page.screenshot({ path: path.join(dir!, `shell-home-mobile-${scheme}.png`) });
      // Leave the app before signing out: a page of the app still polling (the inbox bell, the fleet line) answers
      // its first 401 with window.location.assign("/login"), which would abort a page.goto("/login") under way.
      await page.goto("about:blank");
      await page.context().clearCookies();
      await page.goto("/login");
      await page.setViewportSize({ width: 1440, height: 900 });
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await page.screenshot({ path: path.join(dir!, `login-${scheme}.png`) });
    });
  }
});
