import type { Locator, Page } from "@playwright/test";

import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { EXPECTED, seedInsights, utcDay } from "./support/insights";
import { open } from "./support/plans";

/**
 * A project's Insights against runs that ended on the last three UTC days (e2e/support/insights.ts): the columns of
 * the outcome and token charts carry the seeded counts, each chart's table says the same figures (failure rate and
 * durations included), the range lives in the URL, and a chart's tooltip follows the arrow keys.
 */
test.skip(isDeployed, "seeds runs through the local stack");

const CHARTS = ["outcomes", "failure", "duration", "tokens"] as const;

function main(page: Page): Locator {
  return page.locator("#main");
}

function card(page: Page, chart: (typeof CHARTS)[number]): Locator {
  return main(page).getByTestId(`insights-${chart}`);
}

function bar(page: Page, chart: "outcomes" | "tokens", series: string, daysAgo: number): Locator {
  return card(page, chart).locator(`[data-testid="insights-${chart}-bar"][data-series="${series}"][data-day="${utcDay(daysAgo)}"]`);
}

/** The cells of a table row after its day, as the page writes them. */
async function cells(row: Locator): Promise<string[]> {
  return (await row.locator("td").allTextContents()).map((text) => text.trim());
}

/** "Oct 5": an end of the range as the page writes it. */
function shortDay(daysAgo: number): string {
  return new Intl.DateTimeFormat("en", { month: "short", day: "numeric", timeZone: "UTC" }).format(new Date(`${utcDay(daysAgo)}T00:00:00Z`));
}

/** "Mon, Oct 5": the day as the page writes it in English, in UTC. */
function dayText(daysAgo: number): string {
  return new Intl.DateTimeFormat("en", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" }).format(
    new Date(`${utcDay(daysAgo)}T00:00:00Z`),
  );
}

test("the charts and their tables show the runs of each day, and the range lives in the URL", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedInsights(me, project);

  await open(page, `/p/${project}/insights`);
  await expect(main(page).getByRole("heading", { level: 1, name: "Insights" })).toBeVisible();
  await expect(page.getByTestId("nav-insights")).toHaveAttribute("aria-current", "page");
  await expect(page.getByTestId("insights-range-30")).toHaveAttribute("aria-pressed", "true");
  await expect(card(page, "outcomes").getByTestId("insights-outcomes-headline")).toHaveText(
    "7 runs ended: 3 done, 2 failed, 1 lost, 1 cancelled",
  );
  await expect(card(page, "failure").getByTestId("insights-failure-headline")).toHaveText(
    "50% of 6 runs failed or were lost; cancelled runs are left out",
  );
  await expect(card(page, "duration").getByTestId("insights-duration-headline")).toHaveText(
    "Median 2m 0s, 90th percentile 7m 0s, from start to end",
  );
  await expect(card(page, "tokens").getByTestId("insights-tokens-headline")).toHaveText("23,300 tokens from 3 runs that reported usage");

  // The columns carry the seeded counts: one segment per outcome of the day, the stack's order bottom up.
  await expect(card(page, "outcomes").getByTestId("insights-outcomes-chart").locator("svg.recharts-surface")).toBeVisible();
  await expect(bar(page, "outcomes", "done", 2)).toHaveAttribute("data-value", "2");
  await expect(bar(page, "outcomes", "failed", 2)).toHaveAttribute("data-value", "1");
  await expect(bar(page, "outcomes", "stopped", 2)).toHaveCount(0);
  await expect(bar(page, "outcomes", "done", 1)).toHaveAttribute("data-value", "1");
  await expect(bar(page, "outcomes", "stopped", 1)).toHaveAttribute("data-value", "1");
  await expect(bar(page, "outcomes", "failed", 0)).toHaveAttribute("data-value", "1");
  await expect(bar(page, "outcomes", "stopped", 0)).toHaveAttribute("data-value", "1");
  await expect(card(page, "outcomes").getByTestId("insights-outcomes-bar")).toHaveCount(6);
  // Heights follow the numbers: two done runs stand twice as tall as one, both at the baseline.
  const twoDone = await bar(page, "outcomes", "done", 2).boundingBox();
  const oneDone = await bar(page, "outcomes", "done", 1).boundingBox();
  expect(twoDone && oneDone ? twoDone.height / oneDone.height : 0).toBeCloseTo(2, 1);
  expect(twoDone && oneDone ? Math.abs(twoDone.y + twoDone.height - (oneDone.y + oneDone.height)) : 1).toBeLessThan(1);
  // Tokens: the day with usage stacks its four kinds; the other days draw nothing.
  await expect(bar(page, "tokens", "cacheRead", 2)).toHaveAttribute("data-value", "17000");
  await expect(bar(page, "tokens", "input", 2)).toHaveAttribute("data-value", "4400");
  await expect(bar(page, "tokens", "output", 2)).toHaveAttribute("data-value", "1500");
  await expect(bar(page, "tokens", "reasoning", 2)).toHaveAttribute("data-value", "400");
  await expect(card(page, "tokens").getByTestId("insights-tokens-bar")).toHaveCount(4);

  // Each chart's table says the same figures, the newest day first, the range in its foot.
  for (const chart of CHARTS) {
    const box = card(page, chart);
    await box.getByTestId(`insights-${chart}-view-table`).click();
    await expect(box).toHaveAttribute("data-view", "table");
    const region = box.getByTestId(`insights-${chart}-table-region`);
    await expect(region).toBeVisible();
    const rows = region.getByTestId(`insights-${chart}-row`);
    await expect(rows).toHaveCount(30);
    await expect(rows.first()).toHaveAttribute("data-day", utcDay(0));
    for (const daysAgo of [0, 1, 2] as const) {
      const row = region.locator(`[data-testid="insights-${chart}-row"][data-day="${utcDay(daysAgo)}"]`);
      await expect(row.locator("th")).toHaveText(dayText(daysAgo));
      expect(await cells(row), `${chart}, ${daysAgo} days ago`).toEqual([...EXPECTED[daysAgo][chart]]);
    }
    expect(await cells(region.getByTestId(`insights-${chart}-total`)), `${chart}, all days`).toEqual([...EXPECTED.total[chart]]);
  }
  // A day without a run has no failure rate and no duration.
  const quiet = card(page, "duration").locator(`[data-testid="insights-duration-row"][data-day="${utcDay(5)}"]`);
  expect(await cells(quiet)).toEqual(["None", "None"]);

  // The range: 7 and 90 days in the URL, 30 the plain address; the tables follow.
  const outcomes = card(page, "outcomes").getByTestId("insights-outcomes-row");
  await page.getByTestId("insights-range-7").click();
  await expect(page).toHaveURL(new RegExp(`/p/${project}/insights\\?days=7$`));
  await expect(page.getByTestId("insights-range-7")).toHaveAttribute("aria-pressed", "true");
  await expect(outcomes).toHaveCount(7);
  await expect(main(page).getByTestId("insights-span")).toHaveText(`${shortDay(6)} to ${shortDay(0)}, UTC days`);
  await page.getByTestId("insights-range-90").click();
  await expect(page).toHaveURL(new RegExp(`/p/${project}/insights\\?days=90$`));
  await expect(outcomes).toHaveCount(90);
  await page.getByTestId("insights-range-30").click();
  await expect(page).toHaveURL(new RegExp(`/p/${project}/insights$`));
  await expect(outcomes).toHaveCount(30);

  // The server renders the range the URL names, its tables complete before any script runs.
  await open(page, `/p/${project}/insights?days=7`);
  await expect(page.getByTestId("insights-range-7")).toHaveAttribute("aria-pressed", "true");
  await expect(card(page, "outcomes").getByTestId("insights-outcomes-row")).toHaveCount(7);
});

test("a chart takes focus with Tab and its tooltip follows the arrow keys", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedInsights(me, project);

  await open(page, `/p/${project}/insights?days=7`);
  const box = card(page, "outcomes");
  const surface = box.getByTestId("insights-outcomes-chart").locator("svg.recharts-surface");
  await expect(surface).toBeVisible();
  await expect(surface).toHaveAttribute("aria-label", "Runs by outcome per day. Use the left and right arrow keys to read each day.");
  await box.getByTestId("insights-outcomes-view-table").focus();
  await page.keyboard.press("Tab");
  await expect(surface).toBeFocused();
  // The kit's focus ring, which shadcn's chart container takes off the surface.
  expect(await surface.evaluate((element) => getComputedStyle(element).outlineStyle)).toBe("solid");

  // Focus shows the first day; four steps right is the day two days ago, with two done and one failed.
  const tooltip = box.getByTestId("insights-tooltip");
  await expect(tooltip).toBeVisible();
  await expect(tooltip.locator("p")).toHaveText(dayText(6));
  for (let step = 0; step < 4; step += 1) await page.keyboard.press("ArrowRight");
  await expect(tooltip.locator("p")).toHaveText(dayText(2));
  await expect(tooltip.locator('[data-line="done"] dd')).toHaveText("2");
  await expect(tooltip.locator('[data-line="failed"] dd')).toHaveText("1");
  await expect(tooltip.locator('[data-line="total"] dd')).toHaveText("3");
  await page.keyboard.press("ArrowRight");
  await expect(tooltip.locator("p")).toHaveText(dayText(1));
  await expect(tooltip.locator('[data-line="cancelled"] dd')).toHaveText("1");

  // The duration chart says both lines of a day, and none where nothing ran.
  const duration = card(page, "duration");
  const durationSurface = duration.getByTestId("insights-duration-chart").locator("svg.recharts-surface");
  await durationSurface.focus();
  const durationTip = duration.getByTestId("insights-tooltip");
  await expect(durationTip.locator("p")).toHaveText(dayText(6));
  await expect(durationTip.locator('[data-line="p50"] dd')).toHaveText("None");
  for (let step = 0; step < 4; step += 1) await page.keyboard.press("ArrowRight");
  await expect(durationTip.locator('[data-line="p50"] dd')).toHaveText("5m 0s");
  await expect(durationTip.locator('[data-line="p90"] dd')).toHaveText("9m 0s");
});

test("someone without a grant on the project finds no insights", async ({ page, admin, member }) => {
  const project = uniqueName("insights");
  await admin.registerProject(project);
  await member([]);
  await open(page, `/p/${project}/insights`);
  await expect(main(page).getByTestId("state-not-found")).toBeVisible();
  await expect(main(page).getByTestId("insights-charts")).toHaveCount(0);
});
