import type { Page, Request } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { command, endedRuns, gridLayout, monitorPath, rowOf, runningRuns, seedMonitorPlan, tileOf, trace } from "./support/monitor";
import { open } from "./support/plans";
import { planRunUnderway, reportState, runOf, say, seedPlanRunPlan } from "./support/runs";

/**
 * The Monitor (`/monitor`) against the real API: the list of every run in flight of every project of the member's
 * grants; the grid that follows them all without `?runs=`, a new run joining it, or the tiles the URL names (ended runs
 * included) through a reload; checking and unchecking a row, closing a tile, and Follow every run in flight; a tile's
 * pill, "Waiting for you", its last state once the run ended and the tail of its trace, live, read from 200 events
 * before last_seq; its stream closed when the tile closes, when the run ends and when the page is left; the reads of
 * events when the stream fails, and the top bar saying the worst; the grid's columns at 1440 by 900 for 1, 2, 4, 6 and 9
 * tiles, within the window, then four columns that scroll; the empty Monitor; axe in light and dark. The specs play
 * the worker. Pages render in English.
 *
 * The local web serves HTTP/1.1, where Chromium opens at most six connections to the origin, and each tile of a run in
 * flight holds one for its stream (the hub's HTTP/2 does not have this limit): the specs keep at most four runs in
 * flight on a page, and fill the larger grids with runs that ended, whose streams end at once.
 */
test.skip(isDeployed, "seeds runs through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

/** The requests for run `id`'s stream, and those of them that closed (aborted by the page, or ended by the hub). */
function watchStreams(page: Page, project: string, id: number) {
  const path = `/v1/projects/${project}/runs/${id}/stream`;
  const opened: string[] = [];
  const closed: string[] = [];
  const mine = (request: Request) => new URL(request.url()).pathname === path;
  page.on("request", (request) => {
    if (mine(request)) opened.push(request.url());
  });
  page.on("requestfailed", (request) => {
    if (mine(request)) closed.push(request.url());
  });
  page.on("requestfinished", (request) => {
    if (mine(request)) closed.push(request.url());
  });
  return { opened, closed };
}

test("follows every run in flight of every project, lists them, and a run that starts joins the grid", async ({ page, member }) => {
  const me = await member([
    { role: "writer", maxLevel: "internal" },
    { role: "writer", maxLevel: "internal" },
  ]);
  const [first, second] = me.projects;
  await seedMonitorPlan(me, first);
  const { live, runs } = await runningRuns(me, first, ["1", "2"]);
  await seedPlanRunPlan(me, second);
  const waiting = await planRunUnderway(me, second, uniqueName("monitor-plan"), { waiting: true });
  await trace(live, runs[0].id, [say("Reading the queue."), command(1, "pnpm test")]);

  await open(page, "/monitor");
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Monitor");
  await expect(page.getByTestId("nav-monitor")).toHaveAttribute("aria-current", "page");
  await expect(main(page).getByTestId("monitor-mode")).toHaveAttribute("data-mode", "follow");

  // The list: every run in flight of both projects, each with its number, project, title, state, worker and time.
  const list = main(page).getByRole("region", { name: "In flight" });
  await expect(list.getByTestId("flight-item")).toHaveCount(3);
  const plan = rowOf(page, waiting.run.id);
  await expect(plan).toContainText(`#${waiting.run.id}`);
  await expect(plan).toContainText(second);
  await expect(plan).toContainText("Plan runs on the web");
  await expect(plan.getByTestId("run-state")).toHaveText("Waiting for decision");
  await expect(plan.getByTestId("flight-steps")).toHaveAttribute("data-total", "4");
  await expect(plan).toContainText(waiting.live.worker.name);
  const step = rowOf(page, runs[0].id);
  await expect(step).toContainText(first);
  await expect(step).toContainText("Monitor step 1");
  await expect(step.getByTestId("run-state")).toHaveText("Running");
  await expect(step.getByTestId("run-elapsed")).toContainText("so far");
  await expect(step.getByRole("checkbox", { name: `Watch run #${runs[0].id}` })).toBeChecked();

  // The grid: a tile for each, the running one's pill pulsing, the waiting plan run's "Waiting for you", the trace live.
  const grid = main(page).getByTestId("monitor-grid");
  await expect(grid.getByTestId("monitor-tile")).toHaveCount(3);
  const running = tileOf(page, runs[0].id);
  await expect(running.getByRole("link", { name: `Open run #${runs[0].id}` })).toHaveAttribute("href", `/p/${first}/runs/${runs[0].id}`);
  await expect(running.getByTestId("run-state").locator('[data-live="true"]')).toHaveCount(1);
  await expect(running.getByTestId("tile-stream")).toHaveAttribute("data-status", "live");
  await expect(running.getByTestId("tile-line")).toHaveText([/Agent Reading the queue\./, /Bash pnpm test/]);
  const planTile = tileOf(page, waiting.run.id);
  await expect(planTile.getByTestId("tile-waiting-you")).toHaveText("Waiting for you");
  await expect(planTile.getByTestId("run-state").locator('[data-live="true"]')).toHaveCount(0);
  await expect(planTile.getByTestId("tile-steps")).toHaveAttribute("data-done", "1");
  await expect(tileOf(page, runs[1].id).getByTestId("tile-waiting-you")).toHaveCount(0);
  // View only: no Cancel and no message box on a tile.
  await expect(grid.getByRole("button", { name: /cancel/i })).toHaveCount(0);
  await expect(grid.getByRole("textbox")).toHaveCount(0);

  // A new line reaches its tile live.
  await trace(live, runs[0].id, [say("Tests pass.")]);
  await expect(running.getByTestId("tile-line").last()).toHaveText(/Agent Tests pass\./);

  // A run that starts joins the grid and the list within one read, and the sidebar counts the runs at work.
  const {
    runs: [started],
  } = await runningRuns(me, first, ["3"]);
  await expect(tileOf(page, started.id)).toBeVisible({ timeout: 15_000 });
  await expect(list.getByTestId("flight-item")).toHaveCount(4);
  await expect(main(page).getByTestId("monitor-mode")).toHaveAttribute("data-mode", "follow");
  await expect(page.getByTestId("nav-monitor-count")).toHaveText("3", { timeout: 15_000 });
});

test("checking, unchecking and closing write the grid to the URL, which a reload keeps, ended runs included", async ({ page, member, admin }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedMonitorPlan(me, project);
  const [ended] = await endedRuns(me, project, ["5"]);
  const {
    runs: [a, b],
  } = await runningRuns(me, project, ["1", "2"]);
  const hidden = uniqueName("hidden");
  await admin.registerProject(hidden);
  const streamOfB = watchStreams(page, project, b.id);

  await open(page, "/monitor");
  const grid = main(page).getByTestId("monitor-grid");
  await expect(grid.getByTestId("monitor-tile")).toHaveCount(2);
  await expect(tileOf(page, ended.id)).toHaveCount(0); // it ended before the visit: not in flight
  await expect.poll(() => streamOfB.opened.length).toBe(1);

  // Unchecking a row takes its tile off and writes the rest to the URL.
  await rowOf(page, a.id).getByRole("checkbox", { name: `Watch run #${a.id}` }).uncheck();
  await expect(page).toHaveURL(new RegExp(`/monitor\\?runs=${project}:${b.id}$`));
  await expect(grid.getByTestId("monitor-tile")).toHaveCount(1);
  await expect(main(page).getByTestId("monitor-mode")).toHaveAttribute("data-mode", "chosen");
  await expect(main(page).getByTestId("monitor-mode")).toHaveText("Watching 1 run you chose.");
  // Checking it again adds it at the end.
  await rowOf(page, a.id).getByRole("checkbox", { name: `Watch run #${a.id}` }).check();
  await expect(page).toHaveURL(new RegExp(`/monitor\\?runs=${project}:${b.id},${project}:${a.id}$`));
  await expect(grid.getByTestId("monitor-tile")).toHaveCount(2);

  // Closing a tile takes it off the grid and closes its stream.
  await tileOf(page, b.id).getByRole("button", { name: `Stop watching run #${b.id}` }).click();
  await expect(tileOf(page, b.id)).toHaveCount(0);
  await expect(page).toHaveURL(new RegExp(`/monitor\\?runs=${project}:${a.id}$`));
  await expect.poll(() => streamOfB.closed.length).toBe(1);
  await expect(rowOf(page, b.id).getByRole("checkbox")).not.toBeChecked();

  // A link names an ended run, a run of a project without a grant and a run that does not exist: the ended one shows
  // in its last state, the other two are left out and said so, and a reload gives the same grid.
  const link = `${monitorPath([a, ended, { project: hidden, id: ended.id }, { project, id: 999_999 }])}`;
  await open(page, link);
  await expect(grid.getByTestId("monitor-tile")).toHaveCount(2);
  await expect(tileOf(page, ended.id).getByTestId("run-state")).toHaveText("Failed");
  await expect(tileOf(page, ended.id).getByTestId("tile-stream")).toHaveCount(0);
  await expect(tileOf(page, ended.id).getByTestId("tile-error-line")).toHaveText("stopped for the grid");
  await expect(main(page).getByTestId("monitor-hidden")).toHaveText(
    "2 runs of the link are not shown: they do not exist or you cannot read them.",
  );
  await page.reload();
  await expect(grid.getByTestId("monitor-tile")).toHaveCount(2);
  expect(await grid.getByTestId("monitor-tile").evaluateAll((tiles) => tiles.map((tile) => tile.getAttribute("data-run-id")))).toEqual([
    String(a.id),
    String(ended.id),
  ]);

  // Follow every run in flight goes back to the bare address and every run in flight.
  await main(page).getByTestId("monitor-follow-all").click();
  await expect(page).toHaveURL(/\/monitor$/);
  await expect(main(page).getByTestId("monitor-mode")).toHaveAttribute("data-mode", "follow");
  await expect(grid.getByTestId("monitor-tile")).toHaveCount(2);
  await expect(tileOf(page, b.id)).toBeVisible();
  await expect(tileOf(page, ended.id)).toHaveCount(0);

  // Unchecking every row leaves an empty grid that offers to follow every run again.
  await rowOf(page, a.id).getByRole("checkbox").uncheck();
  await rowOf(page, b.id).getByRole("checkbox").uncheck();
  await expect(page).toHaveURL(/\/monitor\?runs=$/);
  await expect(main(page).getByTestId("state-empty")).toContainText("No run on the grid");
  await expect(main(page).getByTestId("monitor-empty-follow")).toBeVisible();
});

test("a run that ends keeps its tile in its last state, and its stream closes for good", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedMonitorPlan(me, project);
  const {
    live,
    runs: [run],
  } = await runningRuns(me, project, ["1"]);
  await trace(live, run.id, [say("Starting.")]);
  const streams = watchStreams(page, project, run.id);

  await open(page, "/monitor");
  const tile = tileOf(page, run.id);
  await expect(tile.getByTestId("tile-stream")).toHaveAttribute("data-status", "live");
  await trace(live, run.id, [command(1, "pnpm test")]);
  await reportState(live, run.id, { state: "failed", error: "pnpm test exited 1" });

  await expect(tile.getByTestId("run-state")).toHaveText("Failed", { timeout: 10_000 });
  await expect(tile.getByTestId("tile-stream")).toHaveCount(0);
  await expect(tile.getByTestId("tile-line")).toHaveText([/Agent Starting\./, /Bash pnpm test/]);
  await expect(tile.getByTestId("tile-error-line")).toHaveText("pnpm test exited 1");
  await expect(tile.getByTestId("tile-said")).toHaveText(`Run #${run.id} is now Failed.`);
  // It left the list of runs in flight, and the tile stays until the member closes it.
  await expect(rowOf(page, run.id)).toHaveCount(0, { timeout: 10_000 });
  await expect(main(page).getByTestId("flight-empty")).toHaveText("No run is in flight.");
  await expect(tile).toBeVisible();
  // The hub ended the stream; the page does not open it again.
  await expect.poll(() => streams.closed.length).toBe(1);
  await page.waitForTimeout(4_000);
  expect(streams.opened).toHaveLength(1);
  await expect(tile).toBeVisible();
});

test("a tile reads only the tail of a long trace, never the whole history", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedMonitorPlan(me, project);
  const {
    live,
    runs: [run],
  } = await runningRuns(me, project, ["1"]);
  await trace(
    live,
    run.id,
    Array.from({ length: 260 }, (_, index) => command(index + 1, `echo step-${String(index + 1).padStart(3, "0")}`)),
  );
  // The hub's own events (the moves between states) count in last_seq too.
  const { last_seq: total } = await runOf(me, project, run.id);
  const reads: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith(`/v1/projects/${project}/runs/${run.id}/`)) reads.push(`${url.pathname.split("/").at(-1)}?${url.searchParams}`);
  });

  await open(page, monitorPath([run]));
  const tile = tileOf(page, run.id);
  await expect(tile.getByTestId("tile-line").last()).toHaveText(/echo step-260/);
  await expect(tile.getByTestId("tile-earlier")).toHaveText("Earlier events are on the run's page.");
  const shown = await tile.getByTestId("tile-line").allTextContents();
  expect(shown.length).toBeLessThanOrEqual(60);
  expect(shown.some((line) => /step-0[0-5]\d/.test(line))).toBe(false);
  // The stream starts 200 events before last_seq; nothing reads the events from the start.
  expect(reads).toContain(`stream?after=${total - 200}`);
  expect(reads.some((read) => /after=0\b/.test(read))).toBe(false);

  // The newest line stays in view unless the member scrolled up; then Jump to the latest brings it back.
  const log = tile.getByRole("log", { name: `Trace of run #${run.id}` });
  await expect(log).toHaveAttribute("data-follow", "true");
  await log.hover();
  await page.mouse.wheel(0, -2_000);
  await expect(log).toHaveAttribute("data-follow", "false");
  await trace(live, run.id, [command(261, "echo step-261")]);
  await expect(tile.getByTestId("tile-line").last()).toHaveText(/echo step-261/);
  await expect(tile.getByTestId("tile-line").last()).not.toBeInViewport();
  await tile.getByTestId("tile-latest").click();
  await expect(log).toHaveAttribute("data-follow", "true");
  await expect(tile.getByTestId("tile-line").last()).toBeInViewport();
});

test("when a tile's stream fails it reads the events instead, and the top bar says the worst connection", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedMonitorPlan(me, project);
  const {
    live,
    runs: [good, bad],
  } = await runningRuns(me, project, ["1", "2"]);
  await page.route(`**/v1/projects/${project}/runs/${bad.id}/stream**`, (route) =>
    route.fulfill({ status: 503, contentType: "application/json", body: '{"error":"unavailable","message":"no stream"}' }),
  );

  await open(page, "/monitor");
  await expect(tileOf(page, good.id).getByTestId("tile-stream")).toHaveAttribute("data-status", "live");
  await expect(tileOf(page, bad.id).getByTestId("tile-stream")).toHaveAttribute("data-status", "polling");
  await expect(page.getByTestId("live-indicator")).toHaveAttribute("data-state", "reconnecting");
  await trace(live, bad.id, [say("Read while the stream is down.")]);
  await expect(tileOf(page, bad.id).getByTestId("tile-line").last()).toHaveText(/Read while the stream is down\./, { timeout: 5_000 });

  // Once no tile streams any more, the list's own reads speak for the page again.
  await page.unroute(`**/v1/projects/${project}/runs/${bad.id}/stream**`);
  await reportState(live, bad.id, { state: "failed", error: "stopped" });
  await reportState(live, good.id, { state: "failed", error: "stopped" });
  await expect(tileOf(page, bad.id).getByTestId("run-state")).toHaveText("Failed", { timeout: 10_000 });
  await expect(tileOf(page, good.id).getByTestId("run-state")).toHaveText("Failed", { timeout: 10_000 });
  await expect(page.getByTestId("live-indicator")).toHaveAttribute("data-state", "live");
  await expect(page.getByTestId("live-indicator")).toHaveAttribute("data-transport", "poll");
});

test("leaving the page closes every stream", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedMonitorPlan(me, project);
  const {
    runs: [a, b],
  } = await runningRuns(me, project, ["1", "2"]);
  const first = watchStreams(page, project, a.id);
  const second = watchStreams(page, project, b.id);
  await open(page, "/monitor");
  await expect(tileOf(page, a.id).getByTestId("tile-stream")).toHaveAttribute("data-status", "live");
  await expect(tileOf(page, b.id).getByTestId("tile-stream")).toHaveAttribute("data-status", "live");
  await page.getByTestId("nav-home").click();
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Home");
  await expect.poll(() => [first.closed.length, second.closed.length]).toEqual([1, 1]);
});

const SHAPES: [count: number, cols: number, rows: number][] = [
  [1, 1, 1],
  [2, 2, 1],
  [4, 2, 2],
  [6, 3, 2],
  [9, 3, 3],
];

test("the grid shares out the window at 1440 by 900 up to nine tiles, then four columns scroll", async ({ page, member }) => {
  test.setTimeout(90_000);
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedMonitorPlan(me, project);
  const runs = await endedRuns(me, project, Array.from({ length: 10 }, (_, index) => String(index + 1)));
  await page.setViewportSize({ width: 1440, height: 900 });

  for (const [count, cols, rows] of SHAPES) {
    await open(page, monitorPath(runs.slice(0, count)));
    await expect(main(page).getByTestId("monitor-tile")).toHaveCount(count);
    await expect(main(page).locator('[data-testid="monitor-tile"][data-state="failed"]')).toHaveCount(count);
    const layout = await gridLayout(page);
    expect.soft([layout.cols, layout.rows], `${count} tiles: columns and rows`).toEqual([cols, rows]);
    expect.soft(layout.pageOverflow, `${count} tiles: the page does not scroll`).toBeLessThanOrEqual(0);
    expect.soft(layout.lowest, `${count} tiles: every tile within the window`).toBeLessThanOrEqual(layout.viewport);
    expect.soft(layout.gridScrolls, `${count} tiles: the grid does not scroll`).toBeLessThanOrEqual(0);
    for (const heights of layout.heights) {
      expect.soft(Math.max(...heights) - Math.min(...heights), `${count} tiles: one height a row`).toBeLessThanOrEqual(1);
    }
    expect.soft(layout.sideways, `${count} tiles: no sideways scroll`).toBeLessThanOrEqual(0);
  }

  // Ten tiles: four columns of tiles of one height, the grid scrolling inside itself, a region a key reaches.
  await open(page, monitorPath(runs));
  await expect(main(page).getByTestId("monitor-tile")).toHaveCount(10);
  const ten = await gridLayout(page);
  expect([ten.cols, ten.rows]).toEqual([4, 3]);
  expect(ten.gridScrolls).toBeGreaterThan(0);
  expect(ten.pageOverflow).toBeLessThanOrEqual(0);
  const grid = main(page).getByRole("list", { name: "Runs you watch" });
  await expect(grid).toHaveAttribute("tabindex", "0");

  // From 768 to 1279 px two columns at most, under 768 px one; the page scrolls instead.
  await open(page, monitorPath(runs.slice(0, 4)));
  await page.setViewportSize({ width: 1024, height: 900 });
  await expect.poll(async () => (await gridLayout(page)).cols).toBe(2);
  await page.setViewportSize({ width: 375, height: 812 });
  await expect.poll(async () => (await gridLayout(page)).cols).toBe(1);
  expect((await gridLayout(page)).sideways).toBeLessThanOrEqual(0);
  // A tile's close button and the list's toggle are 44 px targets on a phone.
  const close = tileOf(page, runs[0].id).getByTestId("tile-close");
  expect((await close.boundingBox())?.height).toBeGreaterThanOrEqual(44);
  expect((await main(page).getByTestId("flight-toggle").boundingBox())?.height).toBeGreaterThanOrEqual(44);
});

test("with no run in flight the Monitor says so", async ({ page, member }) => {
  await member([{ role: "writer", maxLevel: "internal" }]);
  await open(page, "/monitor");
  const empty = main(page).getByTestId("state-empty");
  await expect(empty).toContainText("No run is in flight");
  await expect(empty).toContainText("Monitor shows each run that is queued, at work or waiting for a decision as a live tile");
  await expect(page.getByTestId("nav-monitor-count")).toHaveCount(0);
});

test("the sidebar, Home's In flight and a project's Runs lead to the Monitor", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedMonitorPlan(me, project);
  await runningRuns(me, project, ["1"]);

  // Home's In flight card links to it.
  await open(page, "/");
  await main(page).getByRole("region", { name: "In flight" }).getByRole("link", { name: "Watch on Monitor" }).click();
  await page.waitForURL(/\/monitor$/);
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Monitor");

  // The sidebar's first group: Home, Inbox, then Monitor with the runs at work.
  const links = page.getByRole("navigation", { name: "Main navigation" }).getByRole("link");
  expect((await links.evaluateAll((items) => items.map((item) => item.getAttribute("data-testid")))).slice(0, 3)).toEqual([
    "nav-home",
    "nav-inbox",
    "nav-monitor",
  ]);
  await expect(page.getByTestId("nav-monitor")).toHaveAccessibleName("Monitor, 1 run at work");

  // A project's Runs page.
  await open(page, `/p/${project}/runs`);
  await main(page).getByTestId("runs-monitor").click();
  await page.waitForURL(/\/monitor$/);
});

for (const scheme of ["light", "dark"] as const) {
  test.describe(`${scheme} theme`, () => {
    test.use({ colorScheme: scheme });

    test(`the Monitor with its list, live tiles, an ended tile and a hidden run has no serious axe violation (${scheme})`, async ({ page, member }) => {
      const me = await member([{ role: "writer", maxLevel: "internal" }]);
      const project = me.projects[0];
      await seedMonitorPlan(me, project);
      const [ended] = await endedRuns(me, project, ["5"]);
      const {
        live,
        runs: [run],
      } = await runningRuns(me, project, ["1"]);
      await trace(live, run.id, [say("Reading the plan."), command(1, "pnpm test"), { kind: "system", body: { text: "verify failed", tone: "error" } }]);
      await open(page, monitorPath([run, ended, { project, id: 999_999 }]));
      await expect(tileOf(page, run.id).getByTestId("tile-line")).toHaveCount(3);
      await expect(main(page).getByTestId("monitor-hidden")).toBeVisible();
      await expect(page.locator("html")).toHaveClass(new RegExp(scheme));
      await expectNoSeriousViolations(page, `/monitor (${scheme})`);
    });

    test(`the empty Monitor has no serious axe violation (${scheme})`, async ({ page, member }) => {
      await member([{ role: "writer", maxLevel: "internal" }]);
      await open(page, "/monitor");
      await expect(main(page).getByTestId("state-empty")).toBeVisible();
      await expect(page.locator("html")).toHaveClass(new RegExp(scheme));
      await expectNoSeriousViolations(page, `/monitor, empty (${scheme})`);
    });
  });
}
