import type { Locator, Page } from "@playwright/test";

/**
 * The toast whose words hold `text` (components/feedback/toast.tsx): the result of a write, bottom right. A success
 * closes after 5 seconds, so a spec checks it right after the action; a failure stays and is an alert.
 */
export function toast(page: Page, text: string | RegExp): Locator {
  return page.getByTestId("toast").filter({ hasText: text });
}
