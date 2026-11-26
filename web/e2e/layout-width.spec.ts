import type { Page } from "@playwright/test";

import { createAgentByApi } from "./support/agents";
import { chatWorker } from "./support/chats";
import { expect, isDeployed, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, uniqueName } from "./support/hub";
import { ACTIVE_PLAN, open, seedPlans } from "./support/plans";
import { planRunUnderway, runPath, say, seedPlanRunPlan, sendEvents } from "./support/runs";

/**
 * The content frame of every page takes the whole width between the sidebar and the window's right edge, less the
 * 24 px gutter on each side (web/DESIGN.md, Spacing and layout): no cap of the shell's, so a 1920 px window is not
 * left with two empty margins. Measured at 1920 by 1080 on Home, the Inbox, a project's Runs, a run, a
 * plan, My memories, Workers and the administration, with the sidebar open and folded, within 1 px; the page's own
 * content reaches across the frame too, so no page caps itself either. Home (`/`) is the exception: no top bar and no
 * sidebar, its rail (288 px) and its main pane take the window between them (web/DESIGN.md, Chat home).
 */
test.skip(isDeployed, "seeds projects, plans and runs through the local stack");

const WINDOW = { width: 1920, height: 1080 };
const GUTTER = 24;

type Measure = { frameLeft: number; frameRight: number; widest: number; sidebarRight: number; clientWidth: number; overflow: number };

async function measure(page: Page): Promise<Measure> {
  return page.evaluate(() => {
    const frame = document.querySelector<HTMLElement>('[data-testid="content-frame"]');
    const sidebar = document.querySelector<HTMLElement>('[data-slot="sidebar-container"]');
    if (!frame || !sidebar) throw new Error("no content frame or sidebar on the page");
    const box = frame.getBoundingClientRect();
    const root = document.documentElement;
    return {
      frameLeft: box.left,
      frameRight: box.right,
      widest: Math.max(...Array.from(frame.children, (child) => child.getBoundingClientRect().width)),
      sidebarRight: sidebar.getBoundingClientRect().right,
      clientWidth: root.clientWidth,
      overflow: root.scrollWidth - root.clientWidth,
    };
  });
}

/** The page at `path` is ready: its heading shown and nothing loading. */
async function visit(page: Page, path: string, ready?: (page: Page) => Promise<void>) {
  await open(page, path);
  await expect(page.locator("#main").getByRole("heading", { level: 1 }).first()).toBeVisible();
  await expect(page.locator("#main").getByTestId("state-loading")).toHaveCount(0);
  if (ready) await ready(page);
}

async function expectFullWidth(page: Page, where: string) {
  const m = await measure(page);
  const left = m.sidebarRight + GUTTER;
  const right = m.clientWidth - GUTTER;
  expect.soft(Math.abs(m.frameLeft - left), `${where}: the frame starts ${GUTTER} px right of the sidebar`).toBeLessThanOrEqual(1);
  expect.soft(Math.abs(m.frameRight - right), `${where}: the frame ends ${GUTTER} px from the window's edge`).toBeLessThanOrEqual(1);
  expect.soft(Math.abs(m.widest - (right - left)), `${where}: the page's content spans the frame`).toBeLessThanOrEqual(1);
  expect.soft(m.overflow, `${where}: the page scrolls sideways`).toBeLessThanOrEqual(0);
}

test("every page of a member takes the full width beside the sidebar at 1920 px, open or folded", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedPlans(me, project);
  await seedPlanRunPlan(me, project);
  // A plan run that waits for the member: the Inbox lists its decision, Workers its worker, Today both.
  const { live, run } = await planRunUnderway(me, project, uniqueName("wide"), { waiting: true });
  await sendEvents(live, run.id, [say("Reading the plan.")]);
  await page.setViewportSize(WINDOW);

  const pages: { path: string; ready?: (page: Page) => Promise<void> }[] = [
    { path: "/today", ready: async (p) => expect(p.locator("#main").getByTestId("needs-you-item").first()).toBeVisible() },
    { path: "/inbox" },
    { path: `/p/${project}/runs`, ready: async (p) => expect(p.locator("#main").getByTestId("runs-table")).toBeVisible() },
    { path: runPath(project, run.id), ready: async (p) => expect(p.locator("#main").getByTestId("trace-message")).toBeVisible() },
    { path: `/p/${project}/plans/${ACTIVE_PLAN}` },
    { path: "/memories" },
    { path: "/workers", ready: async (p) => expect(p.locator("#main").getByTestId("workers-table")).toBeVisible() },
  ];
  for (const { path, ready } of pages) {
    await visit(page, path, ready);
    expect((await measure(page)).sidebarRight, "the sidebar is open, 240 px").toBe(240);
    await expectFullWidth(page, path);
  }

  // Folded to its 56 px of icons, the sidebar gives its width to the content.
  await page.getByTestId("top-bar").getByRole("button", { name: "Show or hide the sidebar" }).click();
  await expect.poll(async () => (await measure(page)).sidebarRight).toBe(56);
  await expectFullWidth(page, "/workers, sidebar folded");
  await visit(page, runPath(project, run.id));
  await expectFullWidth(page, `${runPath(project, run.id)}, sidebar folded`);
});

test("the administration takes the full width beside the sidebar at 1920 px", async ({ page, signInAs }) => {
  await signInAs(ADMIN_ACCOUNT);
  await page.setViewportSize(WINDOW);
  await visit(page, "/admin", async (p) => expect(p.locator("#main").getByTestId("admin-summary")).toBeVisible());
  expect((await measure(page)).sidebarRight).toBe(240);
  await expectFullWidth(page, "/admin");
});

test("Home has no top bar and no sidebar: its rail and its main pane take the window, and the next page keeps the shell", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const handle = uniqueName("analyst");
  await createAgentByApi(me, project, { handle, name: "Analyst" });
  await chatWorker(me, project, uniqueName("chat-box"), 1, handle);
  for (const width of [WINDOW.width, 1440, 1024]) {
    await page.setViewportSize({ width, height: WINDOW.height });
    await open(page, `/?agent=${handle}`);
    await expect(page.getByTestId("home-rail")).toHaveAttribute("data-agent", handle);
    await expect(page.getByTestId("top-bar")).toHaveCount(0);
    await expect(page.locator('[data-slot="sidebar-container"]')).toHaveCount(0);
    await expect(page.getByTestId("content-frame")).toHaveCount(0);
    const m = await page.evaluate(() => {
      const rail = document.querySelector<HTMLElement>('[data-testid="home-rail"]')!.getBoundingClientRect();
      const main = document.querySelector<HTMLElement>('[data-testid="home-main"]')!.getBoundingClientRect();
      const root = document.documentElement;
      return { railLeft: rail.left, mainLeft: main.left, mainRight: main.right, clientWidth: root.clientWidth, overflow: root.scrollWidth - root.clientWidth };
    });
    expect.soft(m.railLeft, `Home at ${width} px: the rail starts at the window's edge`).toBe(0);
    expect.soft(m.mainLeft, `Home at ${width} px: the main pane starts after the 288 px rail`).toBe(288);
    expect.soft(Math.abs(m.mainRight - m.clientWidth), `Home at ${width} px: the main pane reaches the window's edge`).toBeLessThanOrEqual(1);
    expect.soft(m.overflow, `Home at ${width} px scrolls sideways`).toBeLessThanOrEqual(0);
  }

  // The page after Home has its sidebar open, its top bar and its frame, as before.
  await page.setViewportSize(WINDOW);
  await visit(page, "/today");
  await expect(page.getByTestId("top-bar")).toBeVisible();
  expect((await measure(page)).sidebarRight, "the sidebar is open, 240 px").toBe(240);
  await expectFullWidth(page, "/today after Home");
});
