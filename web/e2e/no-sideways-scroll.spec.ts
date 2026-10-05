import type { Page } from "@playwright/test";

import { expect, type Member, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, machineToken } from "./support/hub";
import { grantOn, kgPath, nodePath, SHARED_NODE, sharedKg } from "./support/kg";
import { apiOf, memoryFile, putMemory } from "./support/memories";
import { ACTIVE_PLAN, open, seedPlans } from "./support/plans";
import { packSkill, publishSkill } from "./support/skills";
import { heartbeat, registerWorker, RUNTIMES } from "./support/workers";

/**
 * A table wider than the page scrolls inside its own region; the document itself never scrolls sideways. Step 25
 * found a wide table widening the shell's <main> at 768 and 1024 px, where the open sidebar leaves the content its
 * narrowest for the breakpoint; the shell's inset is min-w-0 since. The pages with the widest tables of each area
 * (admin, plans, memories, skills, knowledge graph, workers), seeded with long unbroken names, at 375, 768 and 1024 px.
 */
const WIDTHS = [375, 768, 1024];
const LONG = "a-rather-long-unbroken-name-that-never-wraps-in-a-table-cell";

type Visit = { path: string; ready: (page: Page) => Promise<void> };

function shown(testId: string) {
  return async (page: Page) => {
    await expect(page.locator("#main").getByTestId(testId).first()).toBeVisible();
  };
}

async function noSidewaysScroll(page: Page, visits: Visit[]) {
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 900 });
    for (const visit of visits) {
      await open(page, visit.path);
      await visit.ready(page);
      await expect(page.locator("#main").getByTestId("state-loading")).toHaveCount(0);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect.soft(overflow, `${visit.path} at ${width} px scrolls sideways`).toBeLessThanOrEqual(0);
    }
  }
}

async function seedProject(me: Member): Promise<Visit[]> {
  const project = me.projects[0];
  await seedPlans(me, project);
  const api = await apiOf(me);
  const wide = ["| region | owner | window | key |", "| --- | --- | --- | --- |", `| eu-central | platform | Monday | ${LONG} |`];
  const memory = await putMemory(api, { project, name: `${LONG}.md`, body: memoryFile("Wide", LONG, wide.join("\n")) });
  const bundle = await packSkill(LONG, "Notes.", `Use when ${LONG}`);
  await publishSkill(api, bundle, LONG, project, { repo: `example-org/${LONG}`, commit: "0123abcdef0123abcdef" });
  return [
    { path: `/p/${project}/plans`, ready: shown("plans-table-active") },
    {
      path: `/p/${project}/plans/${ACTIVE_PLAN}`,
      ready: async (page) => {
        await page.getByTestId("steps-view-list").click();
        await shown("steps-table")(page);
      },
    },
    { path: `/p/${project}/plans/${ACTIVE_PLAN}/revisions?from=1&to=3`, ready: shown("diff-hunk") },
    { path: `/p/${project}/memories`, ready: shown("memories-table") },
    { path: `/p/${project}/memories/${memory.id}`, ready: shown("revision-history") },
    { path: `/p/${project}/skills`, ready: shown("skills-table") },
    { path: `/p/${project}/skills/${LONG}`, ready: shown("skill-versions") },
  ];
}

test("plans, memories and skills pages never scroll sideways at 375, 768 and 1024 px", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "customer" }]);
  await noSidewaysScroll(page, await seedProject(me));
});

test("knowledge graph pages never scroll sideways at 375, 768 and 1024 px", async ({ page, member }) => {
  const me = await member();
  const kg = await sharedKg();
  await grantOn(kg.project, me.login, "reader", "internal");
  await noSidewaysScroll(page, [
    { path: `${kgPath(kg.project)}?q=runbook`, ready: shown("kg-results") },
    { path: nodePath(kg.project, SHARED_NODE), ready: shown("kg-neighbours") },
  ]);
});

test("admin pages never scroll sideways at 375, 768 and 1024 px", async ({ page, signInAs }) => {
  await signInAs(ADMIN_ACCOUNT);
  await machineToken(ADMIN_ACCOUNT); // a machine token row next to the web sessions
  await noSidewaysScroll(page, [
    { path: "/admin/audit", ready: shown("audit-table") },
    { path: "/admin/tokens", ready: shown("tokens-table") },
    { path: "/admin/members", ready: shown("members-table") },
    { path: `/admin/members/${ADMIN_ACCOUNT.login}`, ready: shown("member-tokens") },
  ]);
});

test("workers pages never scroll sideways at 375, 768 and 1024 px", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const worker = await registerWorker(me, { name: `${LONG}-worker`, projects: [me.projects[0]], slots: 8, labels: [LONG.slice(0, 40)] });
  await heartbeat(worker.id, {
    runtimes: { ...RUNTIMES, [`${LONG}-runtime`]: { available: true, version: LONG } },
    checkouts: { [LONG]: { path: `~/github/${LONG}/${LONG}`, branch: LONG } },
  });
  await noSidewaysScroll(page, [
    { path: "/workers", ready: shown("workers-table") },
    { path: `/workers/${worker.id}`, ready: shown("heartbeat-strip") },
  ]);
});
