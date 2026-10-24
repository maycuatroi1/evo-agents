import type { Locator } from "@playwright/test";

/**
 * Opens every run's row and every notice of the Inbox's list under `scope`: below what waits for an answer, a run's
 * notifications of one day fold into one row and a notice shows its title alone until opened.
 */
export async function expandInbox(scope: Locator) {
  for (const testId of ["inbox-run-group-toggle", "notification-toggle"]) {
    const closed = scope.locator(`[data-testid="${testId}"][aria-expanded="false"]`);
    while ((await closed.count()) > 0) await closed.first().click();
  }
}
