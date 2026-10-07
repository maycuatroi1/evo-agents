import type { Page } from "@playwright/test";

import { expect, type Member, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, machineToken } from "./support/hub";
import { seedInsights } from "./support/insights";
import { grantOn, kgPath, nodePath, SHARED_NODE, sharedKg } from "./support/kg";
import { apiOf, memoryFile, putMemory } from "./support/memories";
import { ACTIVE_PLAN, open, seedPlans } from "./support/plans";
import {
  askDecision,
  claimRun,
  dispatch,
  leaseCredentials,
  liveWorker,
  planRunUnderway,
  reportState,
  RUN_PLAN,
  runPath,
  runToReview,
  say,
  seedPlanRunPlan,
  seedRunPlan,
  sendEvents,
  startRun,
  uploadDiff,
} from "./support/runs";
import { putSecretByApi, secretValue } from "./support/secrets";
import { packSkill, publishSkill } from "./support/skills";
import { heartbeat, registerWorker, RUNTIMES } from "./support/workers";

/**
 * A table wider than the page scrolls inside its own region; the document itself never scrolls sideways. Step 25
 * found a wide table widening the shell's <main> at 768 and 1024 px, where the open sidebar leaves the content its
 * narrowest for the breakpoint; the shell's inset is min-w-0 since. The pages with the widest tables of each area
 * (admin, plans, memories, skills, knowledge graph, workers, secrets, runs), Insights with its charts and a wide table,
 * and Home, seeded with long unbroken names, at 375, 768 and 1024 px.
 */
const WIDTHS = [375, 768, 1024];
const LONG = "a-rather-long-unbroken-name-that-never-wraps-in-a-table-cell";
/** LONG as an environment variable a secret may set. */
const LONG_VAR = LONG.toUpperCase().replaceAll("-", "_");

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

test("the secrets page never scrolls sideways at 375, 768 and 1024 px", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const worker = await registerWorker(me, { name: `${LONG}-worker`, projects: [project] });
  await putSecretByApi(me, LONG, {
    kind: "git",
    url_prefix: `https://gitlab.example.org/${LONG}/${LONG}`,
    username: LONG,
    projects: [project],
    workers: [worker.name],
    expires_at: new Date(Date.now() + 30 * 86_400_000).toISOString(),
    value: secretValue("wide"),
  });
  await putSecretByApi(me, `${LONG}-env`, { kind: "env", env_var: LONG_VAR, projects: [project], value: secretValue("wide") });
  await noSidewaysScroll(page, [{ path: "/secrets", ready: shown("secrets-table") }]);
});

test("runs pages never scroll sideways at 375, 768 and 1024 px", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, `${LONG}-worker`);
  const [run] = await dispatch(me, project, ["2", "4"]);
  await claimRun(live);
  await startRun(live, run.id, `${LONG}-session`);
  await putSecretByApi(me, LONG, { kind: "env", env_var: LONG_VAR, projects: [project], value: secretValue("wide") });
  await leaseCredentials(live, run.id);
  await sendEvents(live, run.id, [say(`${LONG} ${LONG}`), { kind: "tool_call", body: { title: "Read", rawInput: { file_path: `/${LONG}/${LONG}/${LONG}.ts` } } }]);
  await runToReview(live, run.id);
  await uploadDiff(live, run.id, `diff --git a/${LONG}/${LONG}.ts b/${LONG}/${LONG}.ts\n--- a/${LONG}/${LONG}.ts\n+++ b/${LONG}/${LONG}.ts\n@@ -1 +1 @@\n-${LONG}${LONG}\n+${LONG}${LONG}${LONG}\n`);
  await noSidewaysScroll(page, [
    { path: `/p/${project}/runs`, ready: shown("runs-table") },
    { path: `/p/${project}/plans/${RUN_PLAN}/steps/2`, ready: shown("step-runs-table") },
    { path: `/workers/${live.worker.id}`, ready: shown("worker-runs-table") },
    {
      path: runPath(project, run.id),
      ready: async (page) => {
        await shown("trace-item")(page);
        await shown("run-lease-item")(page);
      },
    },
    { path: `${runPath(project, run.id)}?view=log`, ready: shown("log-line") },
    { path: `${runPath(project, run.id)}/diff`, ready: shown("diff-file") },
  ]);
});

test("home never scrolls sideways at 375, 768 and 1024 px", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  // Long unbroken names in every card: a worker, a decision's question and a failure.
  await seedPlanRunPlan(me, project);
  const { live, run } = await planRunUnderway(me, project, `${LONG}-plan`, { waiting: true });
  await askDecision(live, run.id, `${LONG}${LONG}?`, "4");
  await seedRunPlan(me, project);
  const step = await liveWorker(me, project, `${LONG}-step`);
  const [failing] = await dispatch(me, project, ["2"]);
  await claimRun(step);
  await reportState(step, failing.id, { state: "failed", error: `${LONG}${LONG}${LONG}` });
  await noSidewaysScroll(page, [{ path: "/", ready: shown("needs-you-item") }]);
});

test("insights never scroll sideways at 375, 768 and 1024 px, charts or tables", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedInsights(me, project);
  await noSidewaysScroll(page, [
    {
      path: `/p/${project}/insights`,
      ready: async (page) => {
        for (const chart of ["outcomes", "failure", "duration", "tokens"]) {
          await expect(page.locator("#main").getByTestId(`insights-${chart}-chart`).locator("svg.recharts-surface")).toBeVisible();
        }
      },
    },
    {
      path: `/p/${project}/insights?days=90`,
      ready: async (page) => {
        // The widest table: tokens by type, six columns of figures.
        await page.locator("#main").getByTestId("insights-tokens-view-table").click();
        await shown("insights-tokens-table-region")(page);
      },
    },
  ]);
});
