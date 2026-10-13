import type { Page } from "@playwright/test";

import { call } from "../src/lib/api/client";

import { expect, test } from "./support/fixtures";
import { type Account, bearerClient, machineToken } from "./support/hub";
import { open } from "./support/plans";

/**
 * The Graph view of a plan's steps against the real API: the choice of view lives in the URL (`view=`) and in
 * localStorage, an unknown `view` falls back to Board, the graph of a plan shaped like context-economy at revision 19
 * puts steps 1, 2 and 4 on its top row, draws a dashed line from a step not done and marks the step whose
 * dependencies are all done "Ready", and Board and List never fetch the chunks of React Flow.
 */
test.skip(Boolean(process.env.PLAYWRIGHT_BASE_URL), "seeds a plan through the local stack");

const PLAN = "graph-shape";

// context-economy at revision 19: steps 1 to 10 done, 11 and 12 pending (src/lib/plan-graph/layout.test.ts).
const DEPENDS: Record<number, number[]> = { 3: [2], 5: [1], 6: [1, 2, 3, 4, 5], 7: [6], 8: [7], 9: [8], 10: [7, 9], 11: [10], 12: [11] };

async function seedGraphPlan(writer: Account, project: string): Promise<void> {
  const steps = Array.from({ length: 12 }, (_, i) => {
    const id = i + 1;
    return { id, title: `Step number ${id}`, what: `Do part ${id}.`, status: id <= 10 ? "done" : "pending", depends_on: DEPENDS[id] ?? [] };
  });
  const api = bearerClient(await machineToken(writer));
  await call(
    api.PUT("/v1/projects/{project}/plans/{plan_id}", {
      params: { path: { project, plan_id: PLAN } },
      body: { body: { id: PLAN, goal: "A plan shaped like context-economy.", steps }, area: "active" },
    }),
  );
}

async function graphPage(page: Page, member: (grants?: { role: "reader" | "writer" | "admin"; maxLevel: string }[]) => Promise<Account & { projects: string[] }>) {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedGraphPlan(me, project);
  return `/p/${project}/plans/${PLAN}`;
}

/** Wait until React Flow has measured and fitted the graph. */
async function graphReady(page: Page) {
  await expect(page.getByTestId("step-graph-canvas")).toHaveAttribute("data-ready", "true");
  await expect(page.getByTestId("graph-step")).toHaveCount(12);
}

/** The script URLs the page requests from now on. */
function watchScripts(page: Page): Set<string> {
  const scripts = new Set<string>();
  page.on("request", (request) => {
    if (request.resourceType() === "script") scripts.add(request.url());
  });
  return scripts;
}

/** Of `urls`, the scripts that hold React Flow (its pane's class name is in its code). */
async function reactFlowChunks(page: Page, urls: Iterable<string>): Promise<string[]> {
  const found: string[] = [];
  for (const url of urls) {
    const text = await (await page.request.get(url)).text();
    if (text.includes("react-flow__pane")) found.push(new URL(url).pathname);
  }
  return found;
}

test.describe("plan graph", () => {
  test("Graph is kept in the URL and in storage; an unknown view shows Board", async ({ page, member }) => {
    const url = await graphPage(page, member);
    await open(page, url);
    await expect(page.getByTestId("step-board")).toBeVisible();

    await page.getByTestId("steps-view-graph").click();
    await expect(page).toHaveURL(new RegExp(`${PLAN}\\?view=graph$`));
    await graphReady(page);

    await page.reload();
    await graphReady(page);
    await expect(page.getByTestId("step-board")).toHaveCount(0);

    await open(page, url); // no view: the saved choice
    await graphReady(page);

    await open(page, `${url}?view=nope`);
    await expect(page.getByTestId("step-board")).toBeVisible();
    await expect(page.getByTestId("step-graph")).toHaveCount(0);
  });

  test("the graph of a context-economy shaped plan", async ({ page, member }) => {
    const url = await graphPage(page, member);
    await open(page, `${url}?view=graph`);
    await graphReady(page);

    // The top row: the steps that wait for nothing, left to right.
    const boxes = await Promise.all(
      Array.from({ length: 12 }, async (_, i) => {
        const box = await page.locator(`[data-testid="graph-step"][data-step="${i + 1}"]`).boundingBox();
        expect(box).not.toBeNull();
        return { key: String(i + 1), x: box!.x, y: box!.y };
      }),
    );
    const top = Math.min(...boxes.map((b) => b.y));
    const row = boxes.filter((b) => Math.abs(b.y - top) < 2).sort((a, b) => a.x - b.x);
    expect(row.map((b) => b.key)).toEqual(["1", "2", "4"]);

    // A line from a step not done is dashed; one from a done step is solid.
    const notDone = page.locator('[data-testid="graph-edge"][data-from="11"][data-to="12"]');
    await expect(notDone).toHaveAttribute("data-done", "false");
    await expect(notDone.locator("path").first()).toHaveAttribute("stroke-dasharray", "5 4");
    const done = page.locator('[data-testid="graph-edge"][data-from="10"][data-to="11"]');
    await expect(done).toHaveAttribute("data-done", "true");
    await expect(done.locator("path").first()).not.toHaveAttribute("stroke-dasharray", /.+/);

    // Step 11 waits only for done steps: Ready. Step 12 still waits for 11: no label, a dashed border.
    const ready = page.locator('[data-testid="graph-step"][data-step="11"]');
    await expect(ready).toHaveAttribute("data-ready", "true");
    await expect(ready.getByTestId("graph-ready")).toHaveText("Ready");
    const waiting = page.locator('[data-testid="graph-step"][data-step="12"]');
    await expect(waiting).toHaveAttribute("data-waiting", "true");
    await expect(waiting.getByTestId("graph-ready")).toHaveCount(0);
    await expect(page.getByTestId("graph-count-ready")).toContainText("1");
  });

  test("Board and List do not load React Flow", async ({ page, member }) => {
    const url = await graphPage(page, member);

    // Graph fetches React Flow when chosen.
    await open(page, `${url}?view=board`);
    await expect(page.getByTestId("step-board")).toBeVisible();
    const added = watchScripts(page);
    await page.getByTestId("steps-view-graph").click();
    await graphReady(page);
    expect(await reactFlowChunks(page, added)).not.toEqual([]);

    // Board and List, opened fresh and switched between, never do.
    for (const view of ["board", "list"]) {
      const fresh = await page.context().newPage();
      const loaded = watchScripts(fresh);
      await open(fresh, `${url}?view=${view}`);
      await expect(fresh.getByTestId(view === "board" ? "step-board" : "steps-table")).toBeVisible();
      await fresh.getByTestId(view === "board" ? "steps-view-list" : "steps-view-board").click();
      await fresh.waitForLoadState("networkidle");
      expect(await reactFlowChunks(fresh, loaded), `${view} loaded React Flow`).toEqual([]);
      await fresh.close();
    }
  });
});
