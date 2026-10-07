import path from "node:path";

import { expect, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, idleMachineToken, machineToken, newAccount, uniqueName } from "./support/hub";
import { kgProject, queueBuild } from "./support/kg";

/**
 * Review screenshots of the admin area in light and dark, at desktop width and at 375 px, written to
 * E2E_SCREENSHOT_DIR. Skipped unless it is set; it asserts nothing beyond the pages being ready. The pages are in
 * the default language, English, so the locators below go by test id rather than copy.
 *
 * Locators are scoped to <main>: a screenshot or a viewport change while the next page streams in can make React
 * render that page in the browser instead of hydrating it, which leaves the server's copy behind, hidden, outside
 * <main>.
 */
const dir = process.env.E2E_SCREENSHOT_DIR;

test.describe("admin screenshots", () => {
  test.skip(!dir || Boolean(process.env.PLAYWRIGHT_BASE_URL), "set E2E_SCREENSHOT_DIR to take review screenshots");

  for (const scheme of ["light", "dark"] as const) {
    test(`admin area in ${scheme}`, async ({ page, admin, signInAs }) => {
      test.setTimeout(120_000);
      await page.emulateMedia({ colorScheme: scheme });
      const atlas = uniqueName("atlas");
      const harbor = uniqueName("harbor");
      await admin.registerProject(atlas);
      await admin.registerProject(harbor);
      const alice = newAccount("alice");
      const bob = newAccount("bob");
      await admin.grant(atlas, alice.login, "writer", "customer");
      await admin.grant(harbor, alice.login, "reader", "internal");
      await admin.grant(atlas, bob.login, "admin", "secret");
      for (let i = 0; i < 3; i += 1) await machineToken(alice);
      await machineToken(bob);
      // Something for the overview to list: a token about to expire and a project whose graph build failed.
      await idleMachineToken(bob, 80);
      const failing = await kgProject(admin, "failing");
      await queueBuild(failing.project, failing.writer, true);
      await signInAs(ADMIN_ACCOUNT);
      const main = page.locator("#main");
      const shot = async (name: string, fullPage = false) =>
        page.screenshot({ path: path.join(dir!, `admin-${name}-${scheme}.png`), fullPage });

      for (const [width, suffix] of [
        [1440, ""],
        [375, "-mobile"],
      ] as const) {
        await page.setViewportSize({ width, height: width === 375 ? 812 : 900 });
        await page.goto("/admin");
        await expect(main.getByTestId("admin-overview")).toBeVisible();
        await shot(`overview${suffix}`, true);
        await page.goto("/admin/diagnostics");
        await expect(main.getByTestId("diagnostics-table")).toBeVisible();
        await shot(`diagnostics${suffix}`, true);
        await page.goto("/admin/members");
        await expect(main.getByTestId("members-table")).toBeVisible();
        await shot(`members${suffix}`);
        await page.goto(`/admin/members/${alice.login}`);
        await expect(main.getByTestId("member-tokens")).toBeVisible();
        await shot(`member${suffix}`, true);
        await page.goto("/admin/tokens");
        await expect(main.getByTestId("tokens-table")).toBeVisible();
        await shot(`tokens${suffix}`);
        await page.goto("/admin/audit?limit=25");
        await expect(main.getByTestId("audit-table")).toBeVisible();
        await shot(`audit${suffix}`);
      }

      await page.setViewportSize({ width: 1440, height: 900 });
      await page.goto(`/admin/members?q=${alice.login}`);
      await main.getByTestId(`grant-to-${alice.login}`).click();
      const dialog = page.getByTestId("grant-dialog"); // in a portal, outside <main>
      await dialog.getByTestId("grant-project").selectOption(atlas);
      await expect(dialog.getByTestId("grant-existing")).toBeVisible();
      await shot("grant-form");
      await dialog.getByTestId("grant-max-level").selectOption("internal");
      await dialog.getByTestId("grant-continue").click();
      await expect(page.getByTestId("grant-summary")).toBeVisible(); // in a dialog, outside <main>
      await shot("grant-confirm");
      await page.keyboard.press("Escape");
      await page.goto(`/admin/tokens?login=${alice.login}`);
      await main.getByTestId("tokens-table").locator('[data-testid^="revoke-token-"]').first().click();
      await expect(page.getByTestId("revoke-token-dialog")).toBeVisible();
      await shot("revoke-token");
    });
  }
});
