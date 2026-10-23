import type { Page } from "@playwright/test";

/**
 * Opens the sidebar fold an item sits in (components/shell/nav.ts, NAV_GROUPS) when it is closed, so a spec can click
 * the item; an item on a row of its own, or in an open fold, is left as it is. The item may come late (a plugin's page
 * once the plugins are read), so it waits for it first.
 */
export async function revealNavItem(page: Page, testId: string): Promise<void> {
  await page.getByTestId(testId).waitFor({ state: "attached" });
  const fold = page.locator(`[data-nav-group]:has([data-testid="${testId}"]) > [data-testid^="nav-group-"]`);
  if ((await fold.count()) > 0 && (await fold.getAttribute("aria-expanded")) === "false") await fold.click();
}
