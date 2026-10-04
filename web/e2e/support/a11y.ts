import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

export const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const BLOCKING = new Set(["serious", "critical"]);

/** Run axe on what the page shows now; fail on any serious or critical violation, listing each one. */
export async function expectNoSeriousViolations(page: Page, label: string): Promise<void> {
  // Scan the settled page: mid fade-in, text is drawn at partial opacity and axe would measure that.
  await page.waitForFunction(() => document.getAnimations().every((animation) => animation.playState !== "running"));
  const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();
  const blocking = results.violations.filter((violation) => BLOCKING.has(violation.impact ?? ""));
  const report = blocking.map(
    (violation) =>
      `${violation.impact} ${violation.id}: ${violation.help}\n` +
      violation.nodes.map((node) => `    ${node.target.join(" ")}: ${node.failureSummary ?? ""}`).join("\n"),
  );
  expect(report, `axe on ${label}`).toEqual([]);
}
