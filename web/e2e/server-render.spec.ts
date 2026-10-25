import type { APIRequestContext } from "@playwright/test";

import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { dispatch, liveWorker, seedRunPlan } from "./support/runs";

/**
 * A page reads its data on the server and sends it in the HTML, before any script runs, and the browser hydrates that
 * HTML without a mismatch. The shell renders before the
 * page and reads some of the same queries (the sidebar's run count and fleet line, the project switcher's overview), so
 * those entries are already in the cache, empty, when the page's prefetched state reaches it; this checks that they
 * do not hold the page's data back to an effect, which would send the page's skeleton instead.
 */
test.skip(isDeployed, "seeds runs and workers through the local stack");

async function html(request: APIRequestContext, path: string): Promise<string> {
  const response = await request.get(path);
  expect(response.status(), path).toBe(200);
  return response.text();
}

/** Whether the HTML holds `pattern`; a boolean, so a failure names what is missing without printing the whole page. */
function holds(page: string, pattern: string | RegExp): boolean {
  return typeof pattern === "string" ? page.includes(pattern) : pattern.test(page);
}

test("Home, Workers and a project's Runs arrive from the server with their data", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, uniqueName("ssr"));
  const [run] = await dispatch(me, project, ["2"]);

  // Home: the overview (the metric strip and the Projects card) and the workers list (the Fleet card).
  const home = await html(page.request, "/");
  expect.soft(holds(home, new RegExp(`data-testid="home-project"[^>]*data-project="${project}"`)), "Home's Projects card").toBe(true);
  expect.soft(holds(home, new RegExp(`data-testid="fleet-worker"[^>]*data-worker-id="${live.worker.id}"`)), "Home's Fleet card").toBe(true);
  expect.soft(holds(home, 'data-testid="home-metrics"'), "Home's metric strip").toBe(true);

  // Workers: the list the sidebar's fleet line also reads.
  const workers = await html(page.request, "/workers");
  expect.soft(holds(workers, `data-worker-name="${live.worker.name}"`), "the workers list").toBe(true);

  // A project's runs: the summary the sidebar's run count also reads, and the list.
  const runs = await html(page.request, `/p/${project}/runs`);
  expect.soft(holds(runs, `data-run-id="${run.id}"`), "the runs list").toBe(true);
  expect.soft(holds(runs, 'data-testid="runs-summary"'), "the runs summary").toBe(true);

  // The browser's first render matches that HTML: no hydration error on any of them, on a desktop or a phone.
  const problems: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") problems.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => problems.push(`page: ${error.message}`));
  for (const width of [1440, 375]) {
    await page.setViewportSize({ width, height: 900 });
    for (const path of ["/", "/workers", `/p/${project}/runs`]) {
      await page.goto(path);
      await expect(page.getByTestId("palette-trigger-key")).toBeAttached(); // the shell has hydrated
      await expect(page.locator("#main").getByTestId("state-loading")).toHaveCount(0);
    }
  }
  expect(problems).toEqual([]);
});
