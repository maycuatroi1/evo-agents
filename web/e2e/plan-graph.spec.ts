import type { Page } from "@playwright/test";

import { call } from "../src/lib/api/client";

import { expect, test } from "./support/fixtures";
import { type Account, bearerClient, machineToken } from "./support/hub";
import { open } from "./support/plans";

/**
 * The Graph view of a plan's steps against the real API: the choice of view lives in the URL (`view=`) and in
 * localStorage, an unknown `view` falls back to Board, the graph of a plan shaped like context-economy at revision 19
 * puts steps 1, 2 and 4 on its top row, draws a dashed line from a step not done and marks the step whose
 * dependencies are all done "Ready", and Board and List never fetch the chunks of React Flow. A selected step is
 * in the URL (`step=`) and opens the inspector: beside the canvas from 1024 px, a bottom sheet below; it lists every
 * dependency, drawn or not, quotes the plan's criteria the step names and runs nothing. The keyboard moves between
 * the steps, the search and the repo filter dim the steps they leave out, and under reduced motion the canvas moves
 * to a step at once.
 */
test.skip(Boolean(process.env.PLAYWRIGHT_BASE_URL), "seeds a plan through the local stack");

const PLAN = "graph-shape";

// context-economy at revision 19: steps 1 to 10 done, 11 and 12 pending (src/lib/plan-graph/layout.test.ts).
const DEPENDS: Record<number, number[]> = { 3: [2], 5: [1], 6: [1, 2, 3, 4, 5], 7: [6], 8: [7], 9: [8], 10: [7, 9], 11: [10], 12: [11] };

/** Step 10's evidence: five lines, of which the inspector shows three until Show all. */
const EVIDENCE = ["web@1a2b3c4: pnpm test 412 passed", "pnpm lint clean", "pnpm typecheck clean", "playwright 88 passed", "bundle within budget"];

const ACCEPTANCE = [
  { id: "a4", criterion: "Clicking a step opens the inspector.", verify: "pnpm exec playwright test" },
  { id: "a5", criterion: "No page scrolls sideways.", verify: "pnpm exec playwright test" },
];

async function seedGraphPlan(writer: Account, project: string): Promise<void> {
  const steps = Array.from({ length: 12 }, (_, i) => {
    const id = i + 1;
    const step: Record<string, unknown> = {
      id,
      title: `Step number ${id}`,
      what: `Do part ${id}.`,
      repo: id % 2 ? "api" : "web",
      verify: `pnpm test part-${id}`,
      status: id <= 10 ? "done" : "pending",
      depends_on: DEPENDS[id] ?? [],
    };
    if (id === 10) Object.assign(step, { evidence: EVIDENCE.join("\n"), done_at: "2026-10-12T09:30:00+07:00", note: "Took two tries." });
    if (id === 11) Object.assign(step, { acceptance: ["a4", "The board still opens first."], blocking: true });
    return step;
  });
  const api = bearerClient(await machineToken(writer));
  await call(
    api.PUT("/v1/projects/{project}/plans/{plan_id}", {
      params: { path: { project, plan_id: PLAN } },
      body: { body: { id: PLAN, goal: "A plan shaped like context-economy.", acceptance: ACCEPTANCE, steps }, area: "active" },
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

const step = (page: Page, key: string) => page.locator(`[data-testid="graph-step"][data-step="${key}"]`);
/** The box React Flow focuses for a step: the node around our own markup. */
const stepBox = (page: Page, key: string) => page.locator(".react-flow__node").filter({ has: step(page, key) });
const inspector = (page: Page) => page.getByTestId("graph-inspector");

/** The key of the step whose box has the focus, or null. */
async function focusedStep(page: Page): Promise<string | null> {
  return page.evaluate(() => document.activeElement?.querySelector('[data-testid="graph-step"]')?.getAttribute("data-step") ?? null);
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
  test("a selected step opens the inspector beside the canvas and lives in the URL", async ({ page, member }) => {
    const url = await graphPage(page, member);
    await page.setViewportSize({ width: 1440, height: 900 });
    await open(page, `${url}?view=graph`);
    await graphReady(page);
    await expect(inspector(page)).toHaveAttribute("data-layout", "panel");
    await expect(inspector(page).getByTestId("inspector-empty")).toBeVisible();

    // Step 3: what it waits for (2) and what waits for it (6 to 12) stay; 1, 4 and 5 dim.
    await step(page, "3").click();
    await expect(page).toHaveURL(new RegExp(`${PLAN}\\?view=graph&step=3$`));
    await expect(step(page, "3")).toHaveAttribute("data-selected", "true");
    for (const key of ["1", "4", "5"]) await expect(step(page, key)).toHaveAttribute("data-dimmed", "true");
    for (const key of ["2", "6", "12"]) await expect(step(page, key)).toHaveAttribute("data-dimmed", "false");
    await expect(page.locator('[data-testid="graph-edge"][data-from="3"][data-to="6"]')).toHaveAttribute("data-lit", "true");

    // Step 6 lists all five dependencies, the two not drawn (1 and 2, implied through 3 and 5) included.
    await step(page, "6").click();
    const body = inspector(page).getByTestId("inspector-body");
    await expect(body).toHaveAttribute("data-step", "6");
    await expect(body.getByTestId("inspector-key")).toHaveText("6");
    await expect(body.getByTestId("inspector-status")).toBeVisible();
    await expect(body.getByTestId("inspector-title")).toHaveText("Step number 6");
    const deps = body.getByTestId("inspector-depends-on").getByTestId("inspector-dependency");
    await expect(deps).toHaveCount(5);
    await expect(deps.evaluateAll((all) => all.map((one) => one.getAttribute("data-step")))).resolves.toEqual(["1", "2", "3", "4", "5"]);
    await expect(body.getByTestId("inspector-depends-on").locator('[data-kind="implied"]')).toHaveCount(2);
    await expect(body.getByTestId("inspector-dependents").getByTestId("inspector-dependent")).toHaveAttribute("data-step", "7");
    await expect(body.getByTestId("inspector-what")).toContainText("Do part 6.");
    await expect(body.getByTestId("inspector-verify")).toContainText("pnpm test part-6");
    await expect(body.getByTestId("inspector-facts")).toContainText("web");
    await expect(body.getByTestId("inspector-open-page")).toHaveAttribute("href", new RegExp(`/plans/${PLAN}/steps/6$`));
    // Read only: nothing runs from here.
    await expect(inspector(page).getByRole("button", { name: /run/i })).toHaveCount(0);

    // A dependency is a button that selects that step.
    await deps.first().click();
    await expect(page).toHaveURL(/step=1$/);
    await expect(body).toHaveAttribute("data-step", "1");

    // Step 10: Done at, the note, and three lines of evidence until Show all.
    await page.getByTestId("inspector-next").click(); // 2
    await expect(body).toHaveAttribute("data-step", "2");
    await page.getByTestId("inspector-previous").click();
    await expect(body).toHaveAttribute("data-step", "1");
    await expect(page.getByTestId("inspector-previous")).toBeDisabled();
    await step(page, "10").click();
    await expect(body.getByTestId("inspector-facts")).toContainText("2026");
    await expect(body.getByTestId("inspector-note")).toContainText("Took two tries.");
    const evidence = body.getByTestId("inspector-evidence-text");
    await expect(evidence).toHaveText(EVIDENCE.slice(0, 3).join("\n"));
    await body.getByTestId("inspector-evidence-toggle").click();
    await expect(evidence).toHaveText(EVIDENCE.join("\n"));

    // Step 11: Ready, blocking, and its criteria: the plan's a4 quoted, then a sentence of its own.
    await page.getByTestId("inspector-next").click();
    await expect(body).toHaveAttribute("data-step", "11");
    await expect(body.getByTestId("inspector-ready")).toBeVisible();
    await expect(body.getByTestId("inspector-facts").locator('[data-blocking="true"]')).toBeVisible();
    const criteria = body.getByTestId("inspector-criteria").getByRole("listitem");
    await expect(criteria).toHaveCount(2);
    await expect(criteria.first()).toHaveText(/a4\s*Clicking a step opens the inspector\./);
    await expect(criteria.nth(1)).toHaveText("The board still opens first.");

    // Step 12 waits on 11; the last step has no next.
    await page.getByTestId("inspector-next").click();
    await expect(body.getByTestId("inspector-waits-on")).toContainText("11");
    await expect(page.getByTestId("inspector-next")).toBeDisabled();

    // The close button clears the selection and the URL, and gives the focus back to the step.
    await page.getByTestId("inspector-close").click();
    await expect(page).toHaveURL(new RegExp(`${PLAN}\\?view=graph$`));
    await expect(inspector(page).getByTestId("inspector-empty")).toBeVisible();
    expect(await focusedStep(page)).toBe("12");
    for (const key of ["1", "12"]) await expect(step(page, key)).toHaveAttribute("data-dimmed", "false");

    // A link to a step opens the graph with it selected, with or without view=.
    for (const link of [`${url}?view=graph&step=11`, `${url}?step=11`]) {
      await open(page, link);
      await graphReady(page);
      await expect(step(page, "11")).toHaveAttribute("data-selected", "true");
      await expect(inspector(page).getByTestId("inspector-body")).toHaveAttribute("data-step", "11");
    }
    // Leaving the graph drops the step.
    await page.getByTestId("steps-view-board").click();
    await expect(page).toHaveURL(new RegExp(`${PLAN}\\?view=board$`));
  });

  test("the keyboard reaches the steps, moves between them, opens and closes the inspector", async ({ page, member }) => {
    const url = await graphPage(page, member);
    await page.setViewportSize({ width: 1440, height: 900 });
    await open(page, `${url}?view=graph`);
    await graphReady(page);

    // Tab from the view switch reaches a step.
    await page.getByTestId("steps-view-graph").focus();
    let reached: string | null = null;
    for (let i = 0; i < 12 && reached === null; i += 1) {
      await page.keyboard.press("Tab");
      reached = await focusedStep(page);
    }
    expect(reached).not.toBeNull();

    // Along the top row 1, 2, 4, and down and up between 11 and 12.
    await stepBox(page, "1").focus();
    await page.keyboard.press("ArrowRight");
    expect(await focusedStep(page)).toBe("2");
    await page.keyboard.press("ArrowRight");
    expect(await focusedStep(page)).toBe("4");
    await page.keyboard.press("ArrowRight");
    expect(await focusedStep(page)).toBe("4");
    await page.keyboard.press("ArrowLeft");
    expect(await focusedStep(page)).toBe("2");
    await stepBox(page, "11").focus();
    await page.keyboard.press("ArrowDown");
    expect(await focusedStep(page)).toBe("12");
    await page.keyboard.press("ArrowUp");
    expect(await focusedStep(page)).toBe("11");

    // Enter opens the inspector on the focused step; Esc closes it and the focus stays on the step.
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/step=11$/);
    await expect(inspector(page).getByTestId("inspector-body")).toHaveAttribute("data-step", "11");
    await page.keyboard.press("Escape");
    await expect(page).not.toHaveURL(/step=/);
    await expect(inspector(page).getByTestId("inspector-empty")).toBeVisible();
    expect(await focusedStep(page)).toBe("11");

    // Esc from inside the inspector too.
    await page.keyboard.press("Enter");
    await inspector(page).getByTestId("inspector-next").focus();
    await page.keyboard.press("Escape");
    await expect(page).not.toHaveURL(/step=/);
    expect(await focusedStep(page)).toBe("11");
  });

  test("the search and the repo filter dim the steps they leave out and say how many match", async ({ page, member }) => {
    const url = await graphPage(page, member);
    await open(page, `${url}?view=graph`);
    await graphReady(page);
    const live = page.locator('[aria-live="polite"]').filter({ hasText: /Showing/ });

    await page.getByTestId("steps-repo").selectOption("web");
    await expect(page.getByTestId("graph-step")).toHaveCount(12);
    await expect(page.locator('[data-testid="graph-step"][data-dimmed="true"]')).toHaveCount(6);
    await expect(step(page, "1")).toHaveAttribute("data-dimmed", "true");
    await expect(step(page, "2")).toHaveAttribute("data-dimmed", "false");
    await expect(live).toHaveText("Showing 6 of 12 steps");

    await page.getByTestId("steps-repo").selectOption("");
    await page.getByTestId("steps-search").fill("number 1");
    await expect(page.locator('[data-testid="graph-step"][data-dimmed="false"]')).toHaveCount(4); // 1, 10, 11, 12
    await expect(live).toHaveText("Showing 4 of 12 steps");
  });

  test("below 1024 px the inspector is a bottom sheet that takes the focus and gives it back", async ({ page, member }) => {
    const url = await graphPage(page, member);
    await page.setViewportSize({ width: 900, height: 900 });
    await open(page, `${url}?view=graph`);
    await graphReady(page);
    await expect(inspector(page)).toHaveCount(0);

    await step(page, "11").click();
    const sheet = page.getByRole("dialog");
    await expect(sheet).toHaveAttribute("data-layout", "sheet");
    await expect(sheet.getByTestId("inspector-body")).toHaveAttribute("data-step", "11");
    expect(await sheet.evaluate((element) => element.contains(document.activeElement))).toBe(true);

    // A dependency selects that step in the sheet and keeps the focus in it.
    await sheet.getByTestId("inspector-dependency").first().click();
    await expect(sheet.getByTestId("inspector-body")).toHaveAttribute("data-step", "10");
    expect(await sheet.evaluate((element) => element.contains(document.activeElement))).toBe(true);

    await sheet.getByTestId("inspector-close").click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page).not.toHaveURL(/step=/);
    expect(await focusedStep(page)).toBe("10");

    // Esc closes it too.
    await step(page, "12").click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(await focusedStep(page)).toBe("12");
  });

  test("under reduced motion the canvas moves to a step at once", async ({ page, member }) => {
    const url = await graphPage(page, member);
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await open(page, `${url}?view=graph&step=11`);
    await graphReady(page);
    // Zoomed in on the middle of the plan, its last step is out of view.
    for (let i = 0; i < 4; i += 1) await page.getByRole("button", { name: "Zoom in" }).click();
    const canvas = (await page.getByTestId("step-graph-canvas").boundingBox())!;
    const inView = async (key: string) => {
      const box = (await step(page, key).boundingBox())!;
      return (
        box.y >= canvas.y && box.y + box.height <= canvas.y + canvas.height && box.x >= canvas.x && box.x + box.width <= canvas.x + canvas.width
      );
    };
    expect(await inView("12")).toBe(false);

    // Next brings step 12 into view in the same frame, and nothing moves after.
    const transform = () => page.locator(".react-flow__viewport").evaluate((element) => getComputedStyle(element).transform);
    await page.getByTestId("inspector-next").click();
    await expect(page).toHaveURL(/step=12$/);
    const moved = await transform();
    expect(await inView("12")).toBe(true);
    await page.waitForTimeout(400);
    expect(await transform()).toBe(moved);
    expect(await page.evaluate(() => document.getAnimations().filter((a) => a.playState === "running").length)).toBe(0);
  });
});
