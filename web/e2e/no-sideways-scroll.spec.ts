import type { Page } from "@playwright/test";

import { createAgentByApi } from "./support/agents";
import { ceilingOver } from "./support/autonomy";
import { createChatByApi, openDirectByApi, sendByApi, signedInPerson } from "./support/chats";
import { expect, type Member, test } from "./support/fixtures";
import { CONTACTS, contactPath, contactsPath, marketingPath, seedGrowth } from "./support/growth";
import { ADMIN_ACCOUNT, HubAdmin, machineToken, newAccount, uniqueName } from "./support/hub";
import { seedInsights } from "./support/insights";
import { buildsPath, grantOn, kgPath, nodePath, SHARED_NODE, sharedKg } from "./support/kg";
import { decidePath } from "./support/loop";
import { apiOf, memoryFile, putMemory } from "./support/memories";
import { command, endedRuns, monitorPath, runningRuns, seedMonitorPlan } from "./support/monitor";
import { seatByApi, unitByApi } from "./support/org";
import { ACTIVE_PLAN, open, seedPlans } from "./support/plans";
import {
  askDecision,
  claimRun,
  dispatch,
  leaseCredentials,
  liveWorker,
  openRunDetails,
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
import { pmPath, seedPm } from "./support/pm";
import { putSecretByApi, secretValue } from "./support/secrets";
import { CLEAN_REPORT, doctorReport, HIGH_REPORT, narrowByApi, otherMember, shareByApi } from "./support/sharing";
import { documentPath, enableSrs, seedFindings } from "./support/srs";
import { packSkill, publishSkill } from "./support/skills";
import { heartbeat, registerWorker, RUNTIMES } from "./support/workers";

/**
 * A table wider than the page scrolls inside its own region; the document itself never scrolls sideways. Step 25
 * found a wide table widening the shell's <main> at 768 and 1024 px, where the open sidebar leaves the content its
 * narrowest for the breakpoint; the shell's inset is min-w-0 since. The pages with the widest tables of each area
 * (admin, plans, memories, skills, knowledge graph, workers, secrets and their shares, accounts, runs), the org chart three units deep, Insights with its charts and a wide table,
 * Home, the Inbox and a decision in it, the sign-in page, the product page, the Monitor, chats and an SRS version's review with @ba's
 * findings, seeded with long unbroken names, at 375, 768
 * and 1024 px, and at 1440 px, where the content is as wide as the window leaves beside the sidebar, the run page splits
 * into its two scroll regions and the Monitor puts its list beside the grid.
 */
const WIDTHS = [375, 768, 1024, 1440];
const LONG = "a-rather-long-unbroken-name-that-never-wraps-in-a-table-cell";
/** LONG as an environment variable a secret may set. */
const LONG_VAR = LONG.toUpperCase().replaceAll("-", "_");

type Visit = { path: string; ready: (page: Page) => Promise<void> };

function shown(testId: string) {
  return async (page: Page) => {
    await expect(page.locator("#main").getByTestId(testId).first()).toBeVisible();
  };
}

/** Ready once `testId` shows inside the plugin's page the hub frames in the main column (`frame`, its selector). */
function framed(testId: string, frame = '[data-testid="plugin-frame"]') {
  return async (page: Page) => {
    await expect(page.locator("#main").frameLocator(frame).getByTestId(testId).first()).toBeVisible();
  };
}

/** Whether the page is as narrow as a phone's, where the run page shows its overview and opens the trace on a screen. */
function phone(page: Page): boolean {
  return (page.viewportSize()?.width ?? 1440) < 768;
}

/**
 * Whether any visit scrolls sideways at any of `widths`. A test of several pages runs once per width, each with its own
 * 30 s budget and its own seed, and a failure names its width: the plans, memories and skills pages at all four widths
 * in one test went past that budget in CI (32.5 s, and 32.7 s on its retry). No page and no width left a test.
 */
async function noSidewaysScroll(page: Page, widths: number[], visits: Visit[]) {
  for (const width of widths) {
    await page.setViewportSize({ width, height: 900 });
    for (const visit of visits) {
      await open(page, visit.path);
      await visit.ready(page);
      await expect(page.locator("#main").getByTestId("state-loading")).toHaveCount(0);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect.soft(overflow, `${visit.path} at ${width} px scrolls sideways`).toBeLessThanOrEqual(0);
      // A plugin's page draws inside its frame, so the frame's own document must not scroll sideways either.
      for (const handle of await page.locator('#main [data-testid="plugin-frame"]').elementHandles()) {
        const inner = await handle.contentFrame();
        if (!inner) continue;
        const inside = await inner.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        expect.soft(inside, `the framed page of ${visit.path} at ${width} px scrolls sideways`).toBeLessThanOrEqual(0);
      }
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
    {
      // The graph with a step selected: its inspector beside the canvas from 1024 px, a bottom sheet below.
      path: `/p/${project}/plans/${ACTIVE_PLAN}?view=graph&step=12`,
      ready: async (page) => {
        await expect(page.getByTestId("step-graph-canvas")).toHaveAttribute("data-ready", "true");
        await expect(page.getByTestId("inspector-body")).toHaveAttribute("data-step", "12");
      },
    },
    { path: `/p/${project}/plans/${ACTIVE_PLAN}/revisions?from=1&to=3`, ready: shown("diff-hunk") },
    { path: `/p/${project}/memories`, ready: shown("memories-table") },
    { path: `/p/${project}/memories/${memory.id}`, ready: shown("revision-history") },
    { path: `/p/${project}/skills`, ready: shown("skills-table") },
    { path: `/p/${project}/skills/${LONG}`, ready: shown("skill-versions") },
  ];
}

for (const width of WIDTHS) {
  test(`plans, memories and skills pages never scroll sideways at ${width} px`, async ({ page, member }) => {
    const me = await member([{ role: "writer", maxLevel: "customer" }]);
    await noSidewaysScroll(page, [width], await seedProject(me));
  });
}

for (const width of WIDTHS) {
  test(`PM daily never scrolls sideways at ${width} px`, async ({ page, member }) => {
    const me = await member([{ role: "admin", maxLevel: "internal" }]);
    const project = me.projects[0];
    await seedPm(me, project, { long: LONG });
    await noSidewaysScroll(page, [width], [{ path: pmPath(project), ready: framed("pm-report") }]);
  });
}

for (const width of WIDTHS) {
  test(`Contacts, Marketing and the growth decisions never scroll sideways at ${width} px`, async ({ page, member }) => {
    const me = await member([{ role: "admin", maxLevel: "internal" }]);
    const project = me.projects[0];
    await seedGrowth(me, project, { long: true, spent: 50 });
    await noSidewaysScroll(page, [width], [
      { path: contactsPath(project), ready: framed("crm-contacts") },
      { path: contactPath(project, CONTACTS.longLead), ready: framed("crm-touchpoint") },
      { path: marketingPath(project), ready: framed("marketing-channels") },
      { path: marketingPath(project, "posts"), ready: framed("marketing-group-drafts") },
      { path: marketingPath(project, "weekly"), ready: framed("weekly-report") },
      { path: decidePath(project), ready: framed("decide-growth", '[data-testid="plugin-frame"][data-panel]') },
    ]);
  });
}

for (const width of WIDTHS) {
  test(`knowledge graph pages never scroll sideways at ${width} px`, async ({ page, member }) => {
    const me = await member();
    const kg = await sharedKg();
    await grantOn(kg.project, me.login, "reader", "internal");
    await noSidewaysScroll(page, [width], [
      { path: `${kgPath(kg.project)}?q=runbook`, ready: shown("kg-results") },
      { path: buildsPath(kg.project), ready: shown("kg-latest-build") },
      { path: nodePath(kg.project, SHARED_NODE), ready: shown("kg-neighbours") },
    ]);
  });
}

for (const width of WIDTHS) {
  test(`admin pages never scroll sideways at ${width} px`, async ({ page, signInAs }) => {
    await signInAs(ADMIN_ACCOUNT);
    await machineToken(ADMIN_ACCOUNT); // a machine token row next to the web sessions
    await new HubAdmin().invite({ kind: "register", for_login: newAccount(LONG).login, days: 1 }); // a long login in the list
    await noSidewaysScroll(page, [width], [
      { path: "/admin/audit", ready: shown("audit-table") },
      { path: "/admin/invites", ready: shown("invites-table") },
      { path: "/admin/tokens", ready: shown("tokens-table") },
      { path: "/admin/members", ready: shown("members-table") },
      { path: `/admin/members/${ADMIN_ACCOUNT.login}`, ready: shown("member-tokens") },
    ]);
  });
}

for (const width of WIDTHS) {
  test(`an invite link's page never scrolls sideways at ${width} px, signed out, signed in and accepted`, async ({ page, signInAs }) => {
    const hub = new HubAdmin();
    const project = uniqueName(LONG);
    await hub.registerProject(project);
    const created = await hub.invite({ kind: "join", project, role: "writer", max_level: "internal", days: 7 });
    const path = `/invite/${created.token}`;
    await noSidewaysScroll(page, [width], [{ path, ready: (p) => expect(p.getByTestId("invite-sign-in")).toBeVisible() }]);
    await signInAs(newAccount(LONG));
    await noSidewaysScroll(page, [width], [{ path, ready: (p) => expect(p.getByTestId("invite-accept")).toBeVisible() }]);
    await page.getByTestId("invite-accept").click();
    await expect(page.getByTestId("invite-get-started")).toBeVisible(); // accepted before the page loads again
    await noSidewaysScroll(page, [width], [{ path, ready: shown("invite-get-started") }]);
  });
}

for (const width of WIDTHS) {
  test(`workers pages never scroll sideways at ${width} px`, async ({ page, member }) => {
    const me = await member([{ role: "writer", maxLevel: "internal" }]);
    const worker = await registerWorker(me, { name: `${LONG}-worker`, projects: [me.projects[0]], slots: 8, labels: [LONG.slice(0, 40)] });
    await heartbeat(worker.id, {
      runtimes: { ...RUNTIMES, [`${LONG}-runtime`]: { available: true, version: LONG } },
      checkouts: { [LONG]: { path: `~/github/${LONG}/${LONG}`, branch: LONG } },
    });
    await noSidewaysScroll(page, [width], [
      { path: "/workers", ready: shown("workers-table") },
      { path: `/workers/${worker.id}`, ready: shown("heartbeat-strip") },
    ]);
  });
}

test("the secrets page never scrolls sideways at 375, 768, 1024 and 1440 px", async ({ page, member }) => {
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
  // Shared with a member and a unit of long unbroken names, with every limit set: the list of shares under the table.
  const other = await otherMember(LONG.slice(0, 24));
  const unit = await unitByApi(LONG.slice(0, 40));
  const limits = { level: "A3", budget_usd: 99_999.99, usage_ceiling_pct: 80, max_running: 12, expires_at: "2099-12-31T00:00:00Z" } as const;
  await shareByApi(me, `${LONG}-env`, { grantee_kind: "user", login: other.login, projects: [project], ...limits });
  await shareByApi(me, `${LONG}-env`, { grantee_kind: "unit", unit, projects: [project], ...limits });
  await noSidewaysScroll(page, WIDTHS, [{ path: "/secrets", ready: shown("share-row") }]);
});

test("the shares of an account and a worker's Sharing card never scroll sideways at 375, 768, 1024 and 1440 px", async ({ page, member }) => {
  test.setTimeout(60_000);
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await putSecretByApi(me, `${LONG}-oa`, { kind: "env", env_var: "CLAUDE_CODE_OAUTH_TOKEN", projects: [project], value: secretValue("wide") });
  const other = await otherMember(LONG.slice(0, 24));
  await shareByApi(me, `${LONG}-oa`, { grantee_kind: "user", login: other.login, projects: [project], budget_usd: 99_999.99, terms_ack: true });
  const live = await liveWorker(me, project, `${LONG}-worker`);
  const unit = await unitByApi(LONG.slice(0, 40));
  await doctorReport(live, CLEAN_REPORT);
  await narrowByApi(me, live, unit);
  await doctorReport(live, HIGH_REPORT);
  await noSidewaysScroll(page, WIDTHS, [
    { path: "/accounts", ready: shown("share-row") },
    { path: `/workers/${live.worker.id}`, ready: shown("worker-doctor-high") },
  ]);
});

test("the org chart never scrolls sideways at 375, 768, 1024 and 1440 px", async ({ page, member }) => {
  test.setTimeout(60_000);
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  // Units three deep, the deepest with a long unbroken slug and a seat in it.
  const top = await unitByApi("wide-org");
  const middle = await unitByApi("wide-org", top);
  const deep = await unitByApi(LONG.slice(0, 40), middle);
  await seatByApi({ unit: deep, role: "member", login: me.login, ends_on: "2099-12-31" });
  await noSidewaysScroll(page, WIDTHS, [{ path: "/org", ready: shown("org-tree") }]);
});

test("the autonomy settings never scroll sideways at 375, 768, 1024 and 1440 px", async ({ page, member }) => {
  test.setTimeout(60_000);
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  // A ceiling on a unit with a long unbroken slug, which every dimmed level and class names as its source.
  await ceilingOver(me.login, "A1", LONG.slice(0, 40));
  await noSidewaysScroll(page, WIDTHS, [{ path: "/settings/autonomy", ready: shown("autonomy-classes") }]);
});

for (const width of WIDTHS) {
  test(`runs pages never scroll sideways at ${width} px`, async ({ page, member }) => {
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
    await noSidewaysScroll(page, [width], [
      { path: `/p/${project}/runs`, ready: shown("runs-table") },
      { path: `/p/${project}/plans/${RUN_PLAN}/steps/2`, ready: shown("step-runs-table") },
      { path: `/workers/${live.worker.id}`, ready: shown("worker-runs-table") },
      {
        // The run's page with every detail open: on a phone its overview, the trace's latest rows then the details.
        path: runPath(project, run.id),
        ready: async (page) => {
          await shown(phone(page) ? "run-trace-preview-row" : "trace-item")(page);
          await openRunDetails(page, { all: true });
          await shown("run-lease-item")(page);
        },
      },
      {
        // On a phone the trace is a screen of its own.
        path: runPath(project, run.id),
        ready: async (page) => {
          if (phone(page)) await page.locator("#main").getByTestId("run-open-trace").click();
          await shown("trace-item")(page);
        },
      },
      { path: `${runPath(project, run.id)}?view=log`, ready: shown("log-line") },
      { path: `${runPath(project, run.id)}/diff`, ready: shown("diff-file") },
    ]);
  });
}

test("home never scrolls sideways at 375, 768, 1024 and 1440 px", async ({ page, member }) => {
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
  await noSidewaysScroll(page, WIDTHS, [{ path: "/", ready: shown("needs-you-item") }]);
});

for (const width of WIDTHS) {
  test(`the Inbox and a decision in it never scroll sideways at ${width} px`, async ({ page, member }) => {
    const me = await member([{ role: "writer", maxLevel: "internal" }]);
    const project = me.projects[0];
    // Long unbroken names in the question, its context and an option.
    await seedPlanRunPlan(me, project);
    const { live, run } = await planRunUnderway(me, project, `${LONG}-plan`, { waiting: true });
    const decision = await askDecision(live, run.id, `${LONG}${LONG}?`, "4", {
      context: `Step 4 deploys \`${LONG}${LONG}\` to **staging** from ${LONG}/${LONG}.`,
      options: [
        { key: "deploy", label: `Deploy ${LONG}${LONG}`, description: `${LONG}${LONG}` },
        { key: "wait", label: "Wait until tomorrow" },
      ],
    });
    await noSidewaysScroll(page, [width], [
      { path: "/inbox", ready: shown("notification") },
      { path: `/inbox?decision=${decision}`, ready: (p) => expect(p.getByTestId("decision-sheet").getByTestId("decision-form")).toBeVisible() },
    ]);
  });
}

test("the sign-in page never scrolls sideways at 375, 768, 1024 and 1440 px", async ({ page }) => {
  await noSidewaysScroll(page, WIDTHS, [{ path: "/login", ready: (p) => expect(p.getByRole("heading", { level: 1 })).toBeVisible() }]);
});

test.describe("signed out", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("the product page never scrolls sideways at 375, 768, 1024 and 1440 px", async ({ page }) => {
    test.setTimeout(60_000);
    await noSidewaysScroll(page, WIDTHS, [{ path: "/", ready: (p) => expect(p.getByTestId("product-page")).toBeVisible() }]);
  });
});

for (const width of WIDTHS) {
  test(`the monitor never scrolls sideways at ${width} px`, async ({ page, member }) => {
    const me = await member([{ role: "writer", maxLevel: "internal" }]);
    const project = me.projects[0];
    // Long unbroken names in the list and the tiles: a worker, a line of the trace, a command and a failure.
    await seedMonitorPlan(me, project);
    const {
      live,
      runs: [run],
    } = await runningRuns(me, project, ["1"], `${LONG}-worker`);
    await sendEvents(live, run.id, [say(`${LONG}${LONG}`), command(1, `cat /${LONG}/${LONG}/${LONG}.ts`)]);
    const [ended] = await endedRuns(me, project, ["2"], `${LONG}-ended`);
    await noSidewaysScroll(page, [width], [
      { path: "/monitor", ready: shown("tile-line") },
      { path: monitorPath([run, ended]), ready: shown("tile-error-line") },
    ]);
  });
}

for (const width of WIDTHS) {
  test(`insights never scroll sideways at ${width} px, charts or tables`, async ({ page, member }) => {
    const me = await member([{ role: "writer", maxLevel: "internal" }]);
    const project = me.projects[0];
    await seedInsights(me, project);
    await noSidewaysScroll(page, [width], [
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
}

for (const width of WIDTHS) {
  test(`chats never scroll sideways at ${width} px`, async ({ page, member }) => {
    const me = await member([{ role: "admin", maxLevel: "internal" }]);
    const project = me.projects[0];
    // Agents enough to fill the row at the head of the list, one with a long unbroken name: the row scrolls on its own.
    for (const index of [1, 2, 3, 4, 5, 6]) await createAgentByApi(me, project, { handle: `wide-${index}-${project}`.slice(0, 100), name: `${LONG}-${index}` });
    const direct = await openDirectByApi(me, project);
    await sendByApi(me, direct.id, `${LONG}${LONG}`);
    const mai = await signedInPerson("wide", [project]);
    const chat = await createChatByApi(me, { title: `${LONG}${LONG}`.slice(0, 120), people: [mai.login], agents: [project] });
    const root = await sendByApi(me, chat.id, `${LONG}${LONG} https://github.com/example-org/${LONG}/pull/7\n\n\`\`\`\n${LONG}${LONG}${LONG}\n\`\`\``);
    await sendByApi(mai, chat.id, `${LONG}${LONG}`, root.id);
    await noSidewaysScroll(page, [width], [
      { path: "/chats", ready: shown("chats-rows") },
      { path: `/chats/${chat.id}`, ready: shown("chat-messages") },
      { path: `/chats/${direct.id}`, ready: shown("chat-messages") },
      // Below 1536 px the thread is a sheet, outside the page's main.
      { path: `/chats/${chat.id}?thread=${root.id}`, ready: (page) => expect(page.getByTestId("chat-thread")).toHaveAttribute("data-state", "ready") },
    ]);
  });
}

test("an SRS version's review with @ba's findings never scrolls sideways at 375, 768, 1024 and 1440 px", async ({ page, member }) => {
  test.setTimeout(60_000);
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const author = newAccount("srs-author");
  await new HubAdmin().grant(project, author.login, "writer", "internal");
  await enableSrs(me, project);
  const seeded = await seedFindings(author, project, me, { summary: `${LONG}${LONG}`, note: `${LONG}${LONG}` });
  await noSidewaysScroll(page, WIDTHS, [
    {
      path: documentPath(project, `/versions/${seeded.version}`),
      ready: async (page) => {
        await expect(page.locator("#main").getByTestId("srs-findings")).toHaveAttribute("data-state", "ready");
        await shown("srs-review-lock")(page);
      },
    },
  ]);
});
