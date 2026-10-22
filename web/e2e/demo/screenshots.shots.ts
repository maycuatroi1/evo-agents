import { DEMO_TIME_ZONE } from "./env"; // first: it sets the ports and the admin that ../support/env reads

import { mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { expect, type Page, test } from "@playwright/test";

import { signIn } from "../support/auth";
import { graphReady, nodePath } from "../support/kg";
import { open } from "../support/plans";
import { runPath } from "../support/runs";

import { type Demo, keepAlive, seedDemo } from "./seed";
import { CHECKOUT_PLAN, MAYA, NOW, PEOPLE, PROJECT } from "./story";
import { SPOOLED_CALL } from "./trace";

/**
 * The images of the README and the product page: the demo of story.ts seeded once, then each page in light and dark,
 * 1440 and 375 px wide, as Maya sees it with the browser's clock standing at NOW. Each image is written to
 * docs/images/<page>-<width>-<scheme>.png at the repo's root; scripts/screenshots.mjs compresses them after.
 */
const IMAGES = fileURLToPath(new URL("../../../docs/images/", import.meta.url));
const WIDTHS = [
  [1440, 900],
  [375, 812],
] as const;
const COLOURS: Record<number, string> = { [PEOPLE[0].id]: "#2f6f5e", [PEOPLE[1].id]: "#9a5b2e", [PEOPLE[2].id]: "#4b55a3" };

/** A GitHub avatar of the demo's people: their initials on a colour of their own. */
function avatar(id: number): string {
  const person = PEOPLE.find((one) => one.id === id);
  const initials = person ? person.name.split(" ").map((word) => word[0]).join("") : "";
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96">` +
    `<rect width="96" height="96" fill="${COLOURS[id] ?? "#6b7280"}"/>` +
    `<text x="48" y="61" font-family="Helvetica, Arial, sans-serif" font-size="36" font-weight="600" fill="#ffffff" text-anchor="middle">${initials}</text>` +
    `</svg>`
  );
}

const main = (page: Page) => page.locator("#main");

/** Open Tomás's run at the test run whose output the spool hook cut, the hook's note in view. */
async function openSpooledOutput(page: Page, demo: Demo): Promise<void> {
  await open(page, runPath(PROJECT, demo.tomasRun));
  const tool = main(page).locator(`[data-testid="trace-tool"][data-tool-call-id="${SPOOLED_CALL}"]`);
  const toggles = main(page).getByTestId("trace-group-toggle");
  // On a phone the run's page opens on a preview of its trace: the full trace is one tap away.
  const preview = main(page).getByTestId("run-open-trace");
  await expect(toggles.first().or(preview)).toBeVisible();
  if (await preview.isVisible()) await preview.click();
  await expect(toggles.first()).toBeVisible();
  for (let index = 0; index < (await toggles.count()) && (await tool.count()) === 0; index += 1) {
    const toggle = toggles.nth(index);
    if ((await toggle.getAttribute("aria-expanded")) !== "false") continue;
    await toggle.click();
    if ((await tool.count()) === 0) await toggle.click();
  }
  await tool.locator("summary").click();
  const output = tool.getByTestId("trace-tool-output");
  await output.getByTestId("trace-output-more").click();
  await output.locator("pre").evaluate((pre) => {
    const lines = (pre.textContent ?? "").split("\n");
    const note = lines.findIndex((line) => line.startsWith("[evo-hub:"));
    pre.scrollTop = Math.max(0, (note - 8) * 18);
  });
  await tool.evaluate((element) => {
    element.scrollIntoView({ block: "start" });
    let box: HTMLElement | null = element.parentElement;
    while (box && !(/(auto|scroll)/.test(getComputedStyle(box).overflowY) && box.scrollHeight > box.clientHeight)) box = box.parentElement;
    (box ?? window).scrollBy(0, -96);
  });
}

type Screen = { name: string; go: (page: Page, demo: Demo) => Promise<void> };

const SCREENS: Screen[] = [
  {
    name: "inbox",
    go: async (page) => {
      await open(page, "/inbox");
      await expect(main(page).getByTestId("inbox-open-count")).toBeVisible();
    },
  },
  {
    name: "plan-graph",
    go: async (page) => {
      await open(page, `/p/${PROJECT}/plans/${CHECKOUT_PLAN}?view=graph`);
      const canvas = main(page).getByTestId("step-graph-canvas");
      await expect(canvas).toHaveAttribute("data-ready", "true");
      // The graph sits below the plan's goal and progress: scroll it up under the header, its toolbar in view.
      await canvas.evaluate((element) => {
        element.scrollIntoView({ block: "start" });
        window.scrollBy(0, -150);
      });
    },
  },
  {
    name: "plan-run",
    go: async (page, demo) => {
      await open(page, runPath(PROJECT, demo.planRun));
      await expect(main(page).getByTestId("run-decision")).toBeVisible();
      await main(page)
        .getByTestId("run-decisions")
        .evaluate((element) => {
          element.scrollIntoView({ block: "start" });
          window.scrollBy(0, -72);
        });
    },
  },
  {
    name: "monitor",
    go: async (page) => {
      await open(page, "/monitor");
      const tiles = main(page).getByTestId("monitor-tile");
      await expect(tiles.first()).toBeVisible();
      // Each tile in view streams its run's trace once it is shown: wait for the lines of those in the window.
      const height = page.viewportSize()?.height ?? 0;
      for (const tile of await tiles.all()) {
        const box = await tile.boundingBox();
        if (box && box.y < height) await expect(tile.getByTestId("tile-line").first()).toBeVisible();
      }
    },
  },
  { name: "run-spool", go: openSpooledOutput },
  {
    name: "kg-search",
    go: async (page) => {
      await open(page, `/p/${PROJECT}/kg?q=checkout`);
      await expect(main(page).getByTestId("kg-results")).toBeVisible();
      // The search sits below the build's figures: bring its form and first results under the header.
      await main(page)
        .getByTestId("kg-search")
        .evaluate((element) => {
          element.scrollIntoView({ block: "start" });
          window.scrollBy(0, -72);
        });
    },
  },
  {
    name: "kg-graph",
    go: async (page) => {
      await open(page, nodePath(PROJECT, "usecase:CHK-02"));
      // A narrow screen gets the table of neighbours in place of the canvas.
      if ((page.viewportSize()?.width ?? 0) < 768) await expect(main(page).getByTestId("kg-neighbours")).toBeVisible();
      else await graphReady(page);
    },
  },
];

test("the README's images", async ({ page }) => {
  test.setTimeout(1_200_000);
  expect(DEMO_TIME_ZONE).toBe("UTC");
  mkdirSync(IMAGES, { recursive: true });
  const demo = await seedDemo();
  await page.route("https://avatars.githubusercontent.com/**", (route) => {
    const id = Number(/\/u\/(\d+)/.exec(new URL(route.request().url()).pathname)?.[1] ?? 0);
    return route.fulfill({ status: 200, contentType: "image/svg+xml", body: avatar(id) });
  });
  await page.clock.setFixedTime(NOW);
  await signIn(page, MAYA);

  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    for (const [width, height] of WIDTHS) {
      await page.setViewportSize({ width, height });
      for (const screen of SCREENS) {
        await keepAlive(demo);
        await screen.go(page, demo);
        await page.evaluate(() => document.fonts.ready);
        await page.screenshot({ path: `${IMAGES}${screen.name}-${width}-${scheme}.png`, animations: "disabled", caret: "hide" });
      }
    }
  }
});
