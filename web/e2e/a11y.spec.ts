import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { agentPath, agentsPath, createAgentByApi, PIXEL_PNG, putAgentAccountByApi, putAvatarByApi } from "./support/agents";
import {
  baselinePath,
  canvasesPath,
  canvasPath,
  claimPath,
  editByApi,
  METRIC,
  openBaselineByApi,
  pushConversions,
  seedCanvas,
  signalsPath,
  valueCheck,
} from "./support/canvas";
import {
  agentAnswers,
  chatPageOf,
  chatPath,
  chatSession,
  chatWorker,
  claimChatRun,
  claimOnly,
  createChatByApi,
  openDirectByApi,
  putChatImageByApi,
  reactByApi,
  sendByApi,
  setupNote,
  signedInPerson,
} from "./support/chats";
import { ceilingOver, prefByApi, stopOwnByApi } from "./support/autonomy";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { promotionNight, seedCurator } from "./support/curator";
import { type Account, ADMIN_ACCOUNT, HubAdmin, machineToken, newAccount, uniqueName } from "./support/hub";
import { seedInsights } from "./support/insights";
import { buildsPath, explorerReady, graphReady, grantOn, HUB_NODE, kgPath, nodePath, sharedKg } from "./support/kg";
import { apiOf, markAgentMemory, memoryFile, putMemory } from "./support/memories";
import { command, endedRuns, monitorPath, runningRuns, seedMonitorPlan } from "./support/monitor";
import { dayFromNow, seatByApi, seatOf, unitByApi } from "./support/org";
import { ACTIVE_PLAN, EVIDENCE_STEP, open, seedPlans } from "./support/plans";
import {
  askDecision,
  chooseRunAction,
  claimRun,
  COMMIT,
  dispatch,
  HARNESS_REPO,
  leaseCredentials,
  liveWorker,
  openPlanStepFolds,
  openRunDetails,
  PLAN_RUN_PLAN,
  planRunUnderway,
  planRunWorker,
  reportState,
  RUN_PLAN,
  runPath,
  runToReview,
  say,
  seedPlanRunPlan,
  seedRunPlan,
  sendEvents,
  sendNotice,
  startRun,
  tool,
  uploadDiff,
} from "./support/runs";
import { enablePlugin, installedSample, installPlugin, pluginPagePath, publishSample } from "./support/plugins";
import { decidePath, goalsPath, itemPath as loopItemPath, loopPath, seedLoop, seedLoopProject } from "./support/loop";
import { problemsPath, seedProblems } from "./support/problems";
import { addPushDeviceByApi, fakePush, fakeSubscription } from "./support/push";
import { gitlabRepos, mergeQueuePath, seedMergeQueue, seedPolicy } from "./support/merge-queue";
import { seedRelease } from "./support/releases";
import { providerKey, putProviderByApi } from "./support/providers";
import { putSecretByApi, secretValue } from "./support/secrets";
import { CLEAN_REPORT, doctorReport, HIGH_REPORT, narrowByApi, otherMember, shareByApi } from "./support/sharing";
import {
  documentPath,
  editItem,
  enableSrs,
  enableTasks,
  importSample,
  itemPath,
  RULE,
  SAMPLE_SLUG,
  SAMPLE_SRS,
  seedFindings,
  srsPath,
  submitVersion,
  USE_CASE,
} from "./support/srs";
import { packSkill, publishSkill } from "./support/skills";
import { createSprintByApi, createTaskByApi, SAMPLE_TASKS, taskPath, tasksPath } from "./support/tasks";
import { AGENT_COMMIT, CHECKOUTS, heartbeat, NIGHTLY_COMMIT, registerWorker, RUNTIMES } from "./support/workers";

/**
 * axe (WCAG 2.2 A and AA rules) on every page of the web, in light and dark: no serious or critical violation.
 * Later steps add their pages to PAGES; each entry opens the page in a given state and waits for its content.
 * Pages render in the default language, English, and entries find what they click by role, test id or ARIA
 * state rather than by copy.
 */
type Context = { page: Page; me: Member; hidden: string };
type Entry = { name: string; open: (context: Context) => Promise<void>; deployed?: boolean };

/** Memories in the first project of `me` (a writer up to customer; the project's hub sink clears internal). */
async function seedMemories(me: Member) {
  const api = await apiOf(me);
  const project = me.projects[0];
  const text = "## Steps\n\n1. Tag\n2. Ship\n\n- [x] checked\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n```\nmake\n```";
  const runbook = await putMemory(api, { project, name: "runbook.md", body: memoryFile("Runbook", "Ship", "Draft") });
  await putMemory(api, { project, name: "runbook.md", ifRevision: 1, body: memoryFile("Runbook", "Ship", text) });
  await putMemory(api, {
    project,
    location: "api",
    name: "conventions.md",
    type: "reference",
    level: "public",
    body: memoryFile("Conventions", "Paging", "By cursor.", "reference"),
  });
  await putMemory(api, { project, name: "mine.md", type: "user", body: memoryFile("Mine", "Private", "x", "user") });
  return { runbook };
}

/**
 * Three workers of `me`: idle with what a heartbeat reports (its commit, and an update that failed while it has waited
 * half a day for an idle moment), offline, and draining on a daemon that cannot update itself; returns the idle one.
 */
async function seedWorkers(me: Member) {
  const project = me.projects[0];
  const idle = await registerWorker(me, { name: uniqueName("idle"), projects: [project], slots: 2, labels: ["macos", "gpu"] });
  await heartbeat(idle.id, {
    runtimes: RUNTIMES,
    checkouts: CHECKOUTS,
    secondsAgo: 3,
    agentCommit: AGENT_COMMIT,
    selfUpdate: { state: "waiting", nightly_commit: NIGHTLY_COMMIT, error: "uv tool install exited 2", error_at: new Date().toISOString() },
    selfUpdateHours: 13,
  });
  await registerWorker(me, { name: uniqueName("offline"), projects: [project] });
  const draining = await registerWorker(me, { name: uniqueName("draining"), projects: [project] });
  await heartbeat(draining.id, { selfUpdate: { state: "unsupported", reason: "editable install of a local checkout" } });
  const api = await apiOf(me);
  await api.POST("/v1/workers/{worker_id}/drain", { params: { path: { worker_id: draining.id } } });
  return idle;
}

/** A new project where `me` is an admin, with repos on GitLab and two revisions of its policy. */
async function mergeQueueProject(me: Member): Promise<string> {
  const project = uniqueName("mq");
  const hub = new HubAdmin();
  await hub.registerProject(project);
  await hub.grant(project, me.login, "admin", "internal");
  await gitlabRepos(hub, project);
  await seedPolicy(me, project);
  return project;
}

/** The runs plan in the first project of `me`, a live worker, and two runs: one failed on it, one queued. */
async function seedRuns(me: Member) {
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, uniqueName("a11y"));
  const [failing, waiting] = await dispatch(me, project, ["2", "4"]);
  await claimRun(live);
  await reportState(live, failing.id, { state: "failed", error: "verify failed: pnpm test exited 1" });
  return { project, live, failing, waiting };
}

/** A run of `me` that their worker started, with a few lines in its log. */
async function seedLiveRun(me: Member) {
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, uniqueName("a11y"));
  const [run] = await dispatch(me, project, ["2"]);
  await claimRun(live);
  await startRun(live, run.id);
  // A trace with an item of each kind: thinking, words, a plan, a failed call (open, its output on term-bg), a raw
  // runtime event, a usage report and a verify line.
  await sendEvents(live, run.id, [
    { kind: "agent_thought_chunk", body: { content: { type: "text", text: "Step 2 needs the queue table first." } } },
    say("Reading the plan."),
    { kind: "plan", body: { entries: [{ content: "Read the plan", status: "completed" }, { content: "Run the tests", status: "in_progress" }] } },
    tool("Bash", "pnpm test"),
    { kind: "tool_call_update", body: { toolCallId: "call-9", status: "failed", rawOutput: { exitCode: 1 }, content: [{ type: "content", content: { type: "text", text: "FAIL src/queue.test.ts" } }] } },
    { kind: "output", body: { raw: { type: "rate_limit_event" } } },
    { kind: "usage_update", body: { usage: { input_tokens: 40, cache_read_input_tokens: 9000, output_tokens: 300 }, cost: { amount: 0.05, currency: "USD" } } },
    { kind: "system", body: { text: "verify: `pnpm test` exited 1 after 900 ms", exit_code: 1 } },
  ]);
  return { project, live, run };
}

/** Two secrets of `me` for the first project: a variable, and a git credential for one worker that ends in 30 days. */
async function seedSecrets(me: Member) {
  const project = me.projects[0];
  const worker = await registerWorker(me, { name: uniqueName("mini"), projects: [project] });
  await putSecretByApi(me, "claude-oauth", { kind: "env", env_var: "CLAUDE_CODE_OAUTH_TOKEN", projects: [project], value: secretValue("a11y") });
  await putSecretByApi(me, "gitlab-docs", {
    kind: "git",
    url_prefix: "https://gitlab.example.org/group",
    projects: [project],
    workers: [worker.name],
    expires_at: new Date(Date.now() + 30 * 86_400_000).toISOString(),
    value: secretValue("a11y"),
  });
}

/** Accounts of `me`: a Claude subscription and an API key, two Lark variables for one line, a GitLab token. */
async function seedAccounts(me: Member) {
  const projects = [me.projects[0]];
  const env = (name: string, env_var: string) => putSecretByApi(me, name, { kind: "env", env_var, projects, value: secretValue("a11y") });
  await env("claude-sub", "CLAUDE_CODE_OAUTH_TOKEN");
  await env("anthropic-key", "ANTHROPIC_API_KEY");
  await env("lark-id", "LARK_APP_ID");
  await env("lark-secret", "LARK_APP_SECRET");
  await env("gitlab-token", "GITLAB_TOKEN");
}

/**
 * Agents of the first project of `me`, made its admin: one that limits five axes (credentials to none), with an
 * avatar, instructions, an account of its own and a run under it, which a worker of `me` carrying the label it allows
 * makes possible, and one deleted; returns the first's handle.
 */
async function seedAgents(me: Member) {
  const project = me.projects[0];
  await new HubAdmin().grant(project, me.login, "admin", "customer");
  const handle = uniqueName("reviewer");
  await createAgentByApi(me, project, {
    handle,
    name: "Code reviewer",
    description: "Reads diffs and leaves notes.",
    instructions: "Review only; never push.",
    color: "#d97706",
    permissions: {
      credential: { mode: "allowlist", allow: [] },
      tool: { mode: "allowlist", allow: ["read", "bash"] },
      worker: { mode: "allowlist", allow: ["label:gpu"] },
      model: { mode: "allowlist", allow: ["claude-opus-4-1"] },
      runtime: { mode: "allowlist", allow: ["claude-code"] },
    },
  });
  await putAvatarByApi(me, project, handle);
  const gone = uniqueName("gone");
  await createAgentByApi(me, project, { handle: gone, name: "Old helper", color: "#0891b2" });
  const api = await apiOf(me);
  await api.DELETE("/v1/projects/{project}/agents/{handle}", { params: { path: { project, handle: gone } } });
  const gpu = await registerWorker(me, { name: uniqueName("gpu"), projects: [project], labels: ["gpu"] });
  await putAgentAccountByApi(me, project, handle, "openai-key", { env_var: "OPENAI_API_KEY", value: secretValue("a11y"), workers: [gpu.name] });
  await seedRunPlan(me, project);
  await dispatch(me, project, ["2"], { profile: handle, runtime: "claude-code" });
  return handle;
}

/**
 * A chat of `me` with another person and the default agent of their first project: a message that mentions the agent
 * before `me` has a worker (so the hub answers in its thread why it took no work), a reply and a reaction on it; then,
 * with a worker, a mention the agent takes, its run claimed and its reply in the thread, linking a draft pull request.
 */
async function seedChat(me: Member) {
  const project = me.projects[0];
  const mai = await signedInPerson("a11y-mai", [project]);
  const chat = await createChatByApi(me, { title: "Release talk", people: [mai.login], agents: [project] });
  const root = await sendByApi(me, chat.id, `@${project} please look at **the flaky wait test**`);
  await sendByApi(mai, chat.id, "I can review it after lunch.", root.id);
  await reactByApi(mai, chat.id, root.id, "\u{1F440}");
  const live = await chatWorker(me, project, uniqueName("a11y-chat"));
  const asked = await sendByApi(me, chat.id, `@${project} fix it, please`);
  const runId = asked.mentions[0]?.run_id;
  if (!runId) throw new Error(`the mention made no run: ${JSON.stringify(asked.mentions)}`);
  await claimChatRun(live, runId);
  await agentAnswers(live, runId, "Fixed the wait. Draft PR: https://github.com/example-org/api/pull/7");
  return { chat, root, asked };
}

/**
 * A chat of `me` with the default agent of their first project whose two mentions' runs are on their way: the first
 * claimed by its worker and opening a repo, the second waiting for that worker. The chat.
 */
async function seedChatProgress(me: Member) {
  const project = me.projects[0];
  const live = await chatWorker(me, project, uniqueName("a11y-setup"), 2);
  const chat = await createChatByApi(me, { title: "Setup talk", agents: [project] });
  const asked = await sendByApi(me, chat.id, `@${project} is the wait test flaky?`);
  const runId = asked.mentions[0]?.run_id;
  if (!runId) throw new Error(`the mention made no run: ${JSON.stringify(asked.mentions)}`);
  await claimOnly(live, runId);
  await setupNote(live, runId, 1, "opening", "evo-agents-harness");
  await sendByApi(me, chat.id, `@${project} and the docs, in a session of their own?`);
  return chat;
}

/**
 * The direct chat of `me` with the default agent of their first project: a question the agent answered (its run, a
 * draft pull request and a code block), the member's reply to that answer and a new question, then the agent's answer
 * to the reply, which quotes it since the question came between; the question waits for the worker. The chat.
 */
async function seedDirectChat(me: Member) {
  const project = me.projects[0];
  const chat = await openDirectByApi(me, project);
  const live = await chatWorker(me, project, uniqueName("a11y-direct"), 2);
  const asked = await sendByApi(me, chat.id, "Is the **wait test** flaky?");
  const runId = asked.mentions[0]?.run_id;
  if (!runId) throw new Error(`the message made no run: ${JSON.stringify(asked.mentions)}`);
  await claimChatRun(live, runId);
  await agentAnswers(live, runId, "Not flaky: it waits on the clock. Draft PR: https://github.com/example-org/api/pull/7\n\n```ts\nawait clock.tickAsync(500);\n```");
  const answer = (await chatPageOf(me, chat.id)).messages.find((message) => message.author.kind === "agent");
  const reply = await sendByApi(me, chat.id, "And the `retries`?", answer?.id ?? null);
  await sendByApi(me, chat.id, "Check the docs too?");
  const replyRun = reply.mentions[0]?.run_id;
  if (!replyRun) throw new Error(`the reply made no run: ${JSON.stringify(reply.mentions)}`);
  await claimChatRun(live, replyRun);
  await agentAnswers(live, replyRun, "Retries are bounded too: three at most.");
  return chat;
}

/** A session of the default agent of the first project of `me` in three turns, the last one running: the runs' ids. */
async function seedChatSession(me: Member): Promise<number[]> {
  const project = me.projects[0];
  const live = await chatWorker(me, project, uniqueName("a11y-session"));
  const { runs } = await chatSession(me, project, live, ["please fix the flaky wait test", "one more thing: the retries", "and the docs?"]);
  return runs;
}

async function seedSkill(me: Member) {
  const bundle = await packSkill("team-notes", "Notes.", "Use when writing notes");
  await publishSkill(await apiOf(me), bundle, "team-notes", me.projects[0], { repo: "example-org/skills", commit: "0123abc" });
}

/**
 * Home's every card, in the first project of `me`: a plan run waiting for their decision, a step run at work and one
 * that failed (with Rerun), and their two workers.
 */
async function seedHome(me: Member) {
  const project = me.projects[0];
  await seedPlanRunPlan(me, project);
  await planRunUnderway(me, project, uniqueName("a11y-home"), { waiting: true });
  const { live, waiting } = await seedRuns(me);
  await claimRun(live);
  await startRun(live, waiting.id);
}

/**
 * Tasks in a project of `owner`, an admin of it, for the Tasks pages: srs and tasks enabled, the sample SRS imported,
 * the runs plan and a run of its step 2, the active Sprint 7, and four tasks in it, one per column but the backlog;
 * T-1 links a use case, a business rule, the plan step, the run and a pull request, has `other` as its assignee and a
 * comment; T-4 is cancelled, off the board.
 */
async function seedTasks(owner: Account, project: string, other: string) {
  await enableSrs(owner, project);
  await enableTasks(owner, project);
  await importSample(owner, project);
  await seedRunPlan(owner, project);
  const [run] = await dispatch(owner, project, ["2"]);
  await createSprintByApi(owner, project, "Sprint 7", "active");
  await createTaskByApi(owner, project, {
    title: "Checkout places the order",
    description_md: "Customers place an order from the cart.\n\n- The cart is kept for a day.",
    status: "in_progress",
    priority: "urgent",
    due_on: "2026-10-24",
    sprint: "Sprint 7",
    assignees: [other],
    roles: { ba: [owner.login], qa: [other] },
    links: { use_cases: [USE_CASE.code, RULE.code], steps: [`${RUN_PLAN}#2`], runs: [run.id], pulls: ["https://github.com/example-org/api/pull/7"] },
  });
  await createTaskByApi(owner, project, { title: "Test cases for the cart", status: "todo", priority: "high", sprint: "Sprint 7", assignees: [owner.login, other] });
  await createTaskByApi(owner, project, { title: "Totals round the wrong way", type: "bug", status: "in_review", priority: "low", sprint: "Sprint 7" });
  await createTaskByApi(owner, project, { title: "Dropped idea", status: "cancelled", priority: "low", sprint: "Sprint 7" });
  const api = await apiOf(owner);
  await api.POST("/v1/x/tasks/{project}/tasks/{ref}/comments", { params: { path: { project, ref: "T-1" } }, body: { body: "Is a day **long enough**?" } });
}

const PAGES: Entry[] = [
  {
    name: "home",
    deployed: true,
    open: async ({ page }) => {
      await page.goto("/");
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await expect(page.locator("#main").getByTestId("state-loading")).toHaveCount(0);
    },
  },
  {
    name: "home with a decision, runs in flight, a failed run and the fleet",
    open: async ({ page, me }) => {
      await seedHome(me);
      await open(page, "/");
      const main = page.locator("#main");
      await expect(main.getByTestId("needs-you-item")).toHaveCount(1);
      await expect(main.getByTestId("in-flight-item").first()).toBeVisible();
      await expect(main.getByTestId("recent-rerun")).toBeVisible();
      await expect(main.getByTestId("fleet-worker").first()).toBeVisible();
      await expect(main.getByTestId("summary-done-series")).toHaveCount(1);
    },
  },
  {
    name: "home with the decision sheet open from Needs you",
    open: async ({ page, me }) => {
      await seedHome(me);
      await open(page, "/");
      await page.locator("#main").getByTestId("needs-you-answer").click();
      await expect(page.getByTestId("decision-sheet").getByTestId("decision-form")).toBeVisible();
    },
  },
  {
    name: "home on a small screen",
    open: async ({ page, me }) => {
      await seedHome(me);
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, "/");
      await expect(page.locator("#main").getByTestId("needs-you-item")).toHaveCount(1);
    },
  },
  {
    name: "monitor",
    deployed: true,
    open: async ({ page }) => {
      await page.goto("/monitor");
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await expect(page.locator("#main").getByTestId("state-loading")).toHaveCount(0);
    },
  },
  {
    name: "monitor with its list, a live tile with its trace and an ended tile",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await seedMonitorPlan(me, project);
      const [ended] = await endedRuns(me, project, ["5"]);
      const {
        live,
        runs: [run],
      } = await runningRuns(me, project, ["1"]);
      await sendEvents(live, run.id, [say("Reading the plan."), command(1, "pnpm test")]);
      await open(page, monitorPath([run, ended]));
      const main = page.locator("#main");
      await expect(main.getByTestId("flight-item")).toHaveCount(1);
      await expect(main.getByTestId("monitor-tile")).toHaveCount(2);
      // The live tile's two lines, and the ended tile's one: the hub's note that it guessed the failure's cause.
      await expect(main.getByTestId("tile-line")).toHaveCount(3);
    },
  },
  {
    name: "monitor on a small screen, the list unfolded",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await seedMonitorPlan(me, project);
      const {
        live,
        runs: [run],
      } = await runningRuns(me, project, ["1"]);
      await sendEvents(live, run.id, [say("Reading the plan.")]);
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, "/monitor");
      const main = page.locator("#main");
      await main.getByTestId("flight-toggle").click();
      await expect(main.getByTestId("flight-item")).toHaveCount(1);
      await expect(main.getByTestId("tile-line")).toHaveCount(1);
    },
  },
  {
    name: "project picker open",
    deployed: true,
    open: async ({ page }) => {
      await page.goto("/");
      await page.getByTestId("project-switcher").click();
      await expect(page.getByTestId("project-switcher-menu")).toBeVisible();
    },
  },
  {
    name: "user menu open",
    deployed: true,
    open: async ({ page }) => {
      await page.goto("/");
      await page.getByTestId("user-menu").click();
      await expect(page.getByTestId("user-menu-content")).toBeVisible();
    },
  },
  {
    name: "project overview",
    open: async ({ page, me }) => {
      await page.goto(`/p/${me.projects[0]}`);
      await expect(page.getByTestId("label-ladder")).toBeVisible();
    },
  },
  {
    name: "a project's Curator, its proposals, a proposal, its history, graph health, runtime and charter, read by a writer",
    open: async ({ page, me }) => {
      // The charter is an admin's: another member is the project's admin and owns the night shift's worker.
      const project = me.projects[0];
      const owner = newAccount("owner");
      await new HubAdmin().grant(project, owner.login, "admin", "internal");
      const night = await seedCurator(owner, project, uniqueName("a11y"));
      await page.goto(`/p/${project}/curator`);
      await expect(page.locator("#main").getByTestId("curator-nights")).toBeVisible();
      await expectNoSeriousViolations(page, "the Curator");
      await page.goto(`/p/${project}/curator/proposals`);
      await expect(page.locator("#main").getByTestId("proposals-table")).toBeVisible();
      await expectNoSeriousViolations(page, "the proposals");
      await page.goto(`/p/${project}/curator/history`);
      await expect(page.locator("#main").getByTestId("history-sessions")).toBeVisible();
      await expectNoSeriousViolations(page, "the Curator's history");
      await page.goto(`/p/${project}/curator/graph`);
      await expect(page.locator("#main").getByTestId("graph-page")).toBeVisible();
      await expectNoSeriousViolations(page, "the graph's health");
      await page.goto(`/p/${project}/curator/runtime`);
      await expect(page.locator("#main").getByTestId("runtime-range")).toBeVisible();
      await expectNoSeriousViolations(page, "the runtime");
      await page.goto(`/p/${project}/curator/charter`);
      await expect(page.locator("#main").getByTestId("charter-view")).toBeVisible();
      await expectNoSeriousViolations(page, "the charter");
      await page.goto(`/p/${project}/curator/proposals/${night.proposals[2].id}`);
      await expect(page.locator("#main").getByTestId("proposal-finding")).toBeVisible();
      await expect(page.locator("#main").getByTestId("ledger-figures")).toBeVisible();
      await page.locator("#main").getByTestId("evidence-digest-toggle").first().click();
      await expect(page.locator("#main").getByTestId("evidence-digest")).toBeVisible();
    },
  },
  {
    name: "a project's Releases with a stopped cycle and the cycles before it, read by a writer",
    open: async ({ page, me }) => {
      // The schedule is an admin's: another member is the project's admin and owns it.
      const project = me.projects[0];
      const owner = newAccount("owner");
      await new HubAdmin().grant(project, owner.login, "admin", "internal");
      await seedRelease(owner, project, { stopped: true });
      await page.goto(`/p/${project}/releases`);
      await expect(page.locator("#main").getByTestId("release-stopped")).toBeVisible();
      await expect(page.locator("#main").getByTestId("release-past")).toBeVisible();
    },
  },
  {
    name: "no access (403)",
    open: async ({ page }) => {
      await page.goto("/admin");
      await expect(page.getByTestId("state-forbidden")).toBeVisible();
    },
  },
  {
    name: "project not found",
    open: async ({ page, hidden }) => {
      await page.goto(`/p/${hidden}`);
      await expect(page.getByTestId("state-not-found")).toBeVisible();
    },
  },
  {
    name: "plans list",
    open: async ({ page, me }) => {
      await seedPlans(me, me.projects[0]);
      await open(page, `/p/${me.projects[0]}/plans`);
      await expect(page.getByTestId("plans-table-active")).toBeVisible();
    },
  },
  {
    name: "plan board with its sections open",
    open: async ({ page, me }) => {
      await seedPlans(me, me.projects[0]);
      await open(page, `/p/${me.projects[0]}/plans/${ACTIVE_PLAN}`);
      await expect(page.getByTestId("step-board")).toBeVisible();
      await page.getByTestId("board-column-done").getByRole("button").click();
      for (const summary of await page.locator("details > summary").all()) await summary.click();
    },
  },
  {
    name: "plan steps as a list",
    open: async ({ page, me }) => {
      await seedPlans(me, me.projects[0]);
      await open(page, `/p/${me.projects[0]}/plans/${ACTIVE_PLAN}`);
      await page.getByTestId("steps-view-list").click();
      await expect(page.getByTestId("steps-table")).toBeVisible();
    },
  },
  {
    name: "plan graph with a step selected and its inspector beside the canvas",
    open: async ({ page, me }) => {
      await seedPlans(me, me.projects[0]);
      await page.setViewportSize({ width: 1440, height: 900 });
      await open(page, `/p/${me.projects[0]}/plans/${ACTIVE_PLAN}?view=graph&step=${EVIDENCE_STEP}`);
      await expect(page.getByTestId("step-graph-canvas")).toHaveAttribute("data-ready", "true");
      await expect(page.getByTestId("graph-inspector").getByTestId("inspector-body")).toHaveAttribute("data-step", EVIDENCE_STEP);
    },
  },
  {
    name: "plan graph with a step selected in the bottom sheet",
    open: async ({ page, me }) => {
      await seedPlans(me, me.projects[0]);
      await page.setViewportSize({ width: 800, height: 900 });
      await open(page, `/p/${me.projects[0]}/plans/${ACTIVE_PLAN}?view=graph&step=${EVIDENCE_STEP}`);
      await expect(page.getByTestId("step-graph-canvas")).toHaveAttribute("data-ready", "true");
      await expect(page.getByRole("dialog").getByTestId("inspector-body")).toHaveAttribute("data-step", EVIDENCE_STEP);
    },
  },
  {
    name: "plan step with evidence",
    open: async ({ page, me }) => {
      await seedPlans(me, me.projects[0]);
      await open(page, `/p/${me.projects[0]}/plans/${ACTIVE_PLAN}/steps/${EVIDENCE_STEP}`);
      await expect(page.getByTestId("step-evidence")).toBeVisible();
    },
  },
  {
    name: "plan revisions and diff",
    open: async ({ page, me }) => {
      await seedPlans(me, me.projects[0]);
      await open(page, `/p/${me.projects[0]}/plans/${ACTIVE_PLAN}/revisions?from=1&to=2`);
      await expect(page.getByTestId("diff-line").first()).toBeVisible();
    },
  },
  {
    name: "plan diff side by side",
    open: async ({ page, me }) => {
      await seedPlans(me, me.projects[0]);
      await open(page, `/p/${me.projects[0]}/plans/${ACTIVE_PLAN}/revisions?from=1&to=3`);
      await page.getByTestId("diff-mode-split").click();
      await expect(page.getByTestId("diff-mode-split")).toHaveAttribute("aria-pressed", "true");
    },
  },
  {
    name: "plan not found",
    open: async ({ page, me }) => {
      await open(page, `/p/${me.projects[0]}/plans/no-such-plan`);
      await expect(page.getByTestId("state-not-found")).toBeVisible();
    },
  },
  {
    name: "knowledge graph explorer and search",
    open: async ({ page, me }) => {
      const kg = await sharedKg();
      await grantOn(kg.project, me.login, "reader", "customer");
      await open(page, `${kgPath(kg.project)}?q=runbook`);
      await expect(page.getByTestId("kg-results")).toBeVisible();
      await explorerReady(page, "overview");
    },
  },
  {
    name: "knowledge graph explorer at a node",
    open: async ({ page, me }) => {
      const kg = await sharedKg();
      await grantOn(kg.project, me.login, "reader", "customer");
      await open(page, `${kgPath(kg.project)}?node=${encodeURIComponent("requirement:KB-01")}`);
      await explorerReady(page, "node");
      await expect(page.getByTestId("kg-inspector-relations")).toBeVisible();
    },
  },
  {
    name: "knowledge graph builds",
    open: async ({ page, me }) => {
      const kg = await sharedKg();
      await grantOn(kg.project, me.login, "reader", "customer");
      await open(page, buildsPath(kg.project));
      await expect(page.getByTestId("kg-latest-build")).toBeVisible();
      // the commit each source read: a link out with its own name, a copy button, a badge for docs behind main
      await expect(page.getByTestId("kg-commits").getByTestId("source-commit")).toHaveCount(2);
      await expect(page.getByTestId("kg-commits").getByTestId("source-behind")).toBeVisible();
    },
  },
  {
    name: "knowledge graph node with its neighbourhood",
    open: async ({ page, me }) => {
      const kg = await sharedKg();
      await grantOn(kg.project, me.login, "reader", "customer");
      await open(page, nodePath(kg.project, HUB_NODE));
      await graphReady(page);
      // the group of its 160 notes, cut to the most linked with a way to the rest (kg.spec.ts opens it)
      await expect(page.getByTestId("neighbour-group-more").first()).toBeVisible();
    },
  },
  {
    name: "knowledge graph node on a small screen",
    open: async ({ page, me }) => {
      const kg = await sharedKg();
      await grantOn(kg.project, me.login, "reader", "customer");
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, nodePath(kg.project, "requirement:KB-01", 1));
      await expect(page.getByTestId("kg-neighbours")).toBeVisible();
    },
  },
  {
    name: "knowledge graph node not found",
    open: async ({ page, me }) => {
      const kg = await sharedKg();
      await grantOn(kg.project, me.login, "reader", "internal");
      await open(page, nodePath(kg.project, "deals:doc:acme-contract"));
      await expect(page.getByTestId("state-not-found")).toBeVisible();
    },
  },
  {
    name: "project memories",
    open: async ({ page, me }) => {
      await seedMemories(me);
      await page.goto(`/p/${me.projects[0]}/memories`);
      await expect(page.locator("#main").getByTestId("memories-table")).toBeVisible();
    },
  },
  {
    name: "memory detail with its history",
    open: async ({ page, me }) => {
      const { runbook } = await seedMemories(me);
      await page.goto(`/p/${me.projects[0]}/memories/${runbook.id}`);
      await expect(page.locator("#main").getByTestId("memory-markdown")).toBeVisible();
      await expect(page.locator("#main").getByTestId("revision-history")).toBeVisible();
    },
  },
  {
    name: "memory search without results",
    open: async ({ page, me }) => {
      await page.goto(`/p/${me.projects[0]}/memories?q=nothing-matches-this`);
      await expect(page.locator("#main").getByTestId("state-empty")).toBeVisible();
    },
  },
  {
    name: "personal memories",
    open: async ({ page, me }) => {
      await putMemory(await apiOf(me), { name: "reading-list.md", body: memoryFile("Reading list", "Papers", "- one") });
      await page.goto("/memories");
      await expect(page.locator("#main").getByTestId("memories-table")).toBeVisible();
    },
  },
  {
    name: "project skills",
    open: async ({ page, me }) => {
      await seedSkill(me);
      await page.goto(`/p/${me.projects[0]}/skills`);
      await expect(page.locator("#main").getByTestId("skills-table")).toBeVisible();
    },
  },
  {
    name: "skill detail",
    open: async ({ page, me }) => {
      await seedSkill(me);
      await page.goto(`/p/${me.projects[0]}/skills/team-notes`);
      await expect(page.locator("#main").getByTestId("skill-versions")).toBeVisible();
    },
  },
  {
    name: "shared skills",
    open: async ({ page }) => {
      await page.goto("/skills");
      await expect(page.locator("#main").getByRole("heading", { level: 1 })).toBeVisible();
    },
  },
  {
    name: "workers with no worker yet",
    open: async ({ page }) => {
      await page.goto("/workers");
      await expect(page.locator("#main").getByTestId("state-empty")).toBeVisible();
    },
  },
  {
    name: "workers list with its summary and facets",
    open: async ({ page, me }) => {
      await seedWorkers(me);
      await page.goto("/workers");
      await expect(page.locator("#main").getByTestId("workers-table").locator("tbody tr")).toHaveCount(3);
      await expect(page.locator("#main").getByTestId("workers-summary")).toBeVisible();
      await expect(page.locator("#main").getByTestId("worker-update")).toHaveCount(2); // failed, and can't self-update
    },
  },
  {
    name: "register worker dialog, form with errors, CLI tab and pairing code",
    open: async ({ page }) => {
      await page.goto("/workers");
      await page.getByTestId("workers-register").click();
      const dialog = page.getByTestId("register-dialog");
      await dialog.getByTestId("register-labels").fill("bad/label");
      await dialog.getByTestId("register-create").click(); // shows the field errors
      await expect(dialog.getByTestId("register-name")).toHaveAttribute("aria-invalid", "true");
      await expectNoSeriousViolations(page, "register form with errors");
      await dialog.getByTestId("register-tab-cli").click();
      await expect(dialog.getByTestId("register-cli")).toBeVisible();
      await expectNoSeriousViolations(page, "register, CLI only");
      await dialog.getByTestId("register-tab-pairing").click();
      await dialog.getByTestId("register-name").fill(uniqueName("a11y"));
      await dialog.getByTestId("register-labels").fill("macos");
      await dialog.getByTestId("register-create").click();
      await expect(dialog.getByTestId("pairing-code")).toBeVisible();
    },
  },
  {
    name: "worker detail with its heartbeat strip",
    open: async ({ page, me }) => {
      const idle = await seedWorkers(me);
      await page.goto(`/workers/${idle.id}`);
      await expect(page.locator("#main").getByTestId("heartbeat-strip")).toBeVisible();
      await expect(page.locator("#main").getByTestId("worker-runtimes")).toContainText("2.1.289");
      await expect(page.locator("#main").getByTestId("worker-update-failed")).toBeVisible();
      await expect(page.locator("#main").getByTestId("worker-update-waiting")).toBeVisible();
      await expect(page.locator("#main").getByTestId("worker-commit-sha")).toBeVisible();
      await expect(page.locator("#main").getByRole("switch", { name: "Lock version" })).toBeVisible();
    },
  },
  {
    name: "worker revoke confirmation with a wrong name",
    open: async ({ page, me }) => {
      const idle = await seedWorkers(me);
      await page.goto(`/workers/${idle.id}`);
      await page.locator("#main").getByTestId("worker-revoke").click();
      const dialog = page.getByTestId("revoke-worker-dialog");
      await dialog.getByTestId("revoke-worker-dialog-name").fill("not-the-name");
      await dialog.getByTestId("revoke-worker-dialog-confirm").click();
      await expect(dialog.getByTestId("revoke-worker-dialog-name")).toHaveAttribute("aria-invalid", "true");
    },
  },
  {
    name: "workers on a small screen",
    open: async ({ page, me }) => {
      const idle = await seedWorkers(me);
      await page.setViewportSize({ width: 375, height: 812 });
      await page.goto("/workers");
      await expect(page.locator("#main").getByTestId("workers-table")).toBeVisible();
      await expectNoSeriousViolations(page, "workers at 375 px");
      await page.goto(`/workers/${idle.id}`);
      await expect(page.locator("#main").getByTestId("heartbeat-strip")).toBeVisible();
    },
  },
  {
    name: "secrets with no secret yet",
    open: async ({ page }) => {
      await page.goto("/secrets");
      await expect(page.locator("#main").getByTestId("state-empty")).toBeVisible();
    },
  },
  {
    name: "secrets list, the add form with errors, and the delete confirmation",
    open: async ({ page, me }) => {
      await seedSecrets(me);
      await page.goto("/secrets");
      await expect(page.locator("#main").getByTestId("secrets-table").locator("tbody tr")).toHaveCount(2);
      await expectNoSeriousViolations(page, "secrets list");
      await page.locator("#main").getByTestId("secrets-add").click();
      const dialog = page.getByTestId("secret-dialog");
      await dialog.getByTestId("secret-kind-git").click();
      await dialog.getByTestId("secret-save").click(); // shows the field errors
      await expect(dialog.getByTestId("secret-name")).toHaveAttribute("aria-invalid", "true");
      await expectNoSeriousViolations(page, "add secret with errors");
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      await page.locator("#main").getByTestId("secret-delete").first().click();
      await expect(page.getByTestId("delete-secret-dialog")).toBeVisible();
    },
  },
  {
    name: "secret replace form",
    open: async ({ page, me }) => {
      await seedSecrets(me);
      await page.goto("/secrets");
      await page.locator("#main").getByTestId("secret-replace").last().click();
      await expect(page.getByTestId("secret-dialog")).toHaveAttribute("data-mode", "replace");
      await expect(page.getByTestId("secret-dialog").getByTestId("secret-url-prefix")).toHaveValue("https://gitlab.example.org/group");
    },
  },
  {
    name: "secrets on a small screen",
    open: async ({ page, me }) => {
      await seedSecrets(me);
      await page.setViewportSize({ width: 375, height: 812 });
      await page.goto("/secrets");
      await expect(page.locator("#main").getByTestId("secrets-table")).toBeVisible();
    },
  },
  {
    name: "org chart read by a member, a seat ending soon",
    open: async ({ page, me }) => {
      const slug = await unitByApi("a11y-org");
      await seatByApi({ unit: slug, role: "member", login: me.login, ends_on: dayFromNow(2) });
      await page.goto("/org");
      await expect(seatOf(page, me.login, slug).getByTestId("org-seat-ends-soon")).toBeVisible();
    },
  },
  {
    name: "org chart on a small screen",
    open: async ({ page, me }) => {
      const slug = await unitByApi("a11y-org");
      await seatByApi({ unit: slug, role: "member", login: me.login });
      await page.setViewportSize({ width: 375, height: 812 });
      await page.goto("/org");
      await expect(seatOf(page, me.login, slug)).toBeVisible();
    },
  },
  {
    name: "autonomy settings under a ceiling, with a lowered class and a stop that holds",
    open: async ({ page, me }) => {
      await ceilingOver(me.login, "A2");
      await prefByApi(me, { level: "A4", classes: { K10: "deny" } });
      await stopOwnByApi(me, { reason: "a check" });
      await page.goto("/settings/autonomy");
      await expect(page.locator("#main").getByTestId("autonomy-lowered")).toBeVisible();
      await expect(page.locator("#main").getByTestId("autonomy-stop")).toBeVisible();
    },
  },
  {
    name: "autonomy settings on a small screen",
    open: async ({ page }) => {
      await page.setViewportSize({ width: 375, height: 812 });
      await page.goto("/settings/autonomy");
      await expect(page.locator("#main").getByTestId("autonomy-classes")).toBeVisible();
    },
  },
  {
    name: "notification settings with push on in this browser, a device elsewhere and every kind shown",
    open: async ({ page, me }) => {
      await addPushDeviceByApi(me, "Firefox on Linux");
      await fakePush(page.context(), fakeSubscription("a11y"));
      await page.goto("/settings/notifications");
      const main = page.locator("#main");
      await main.getByTestId("push-browser-on").click();
      await expect(main.getByTestId("push-browser")).toHaveAttribute("data-state", "on");
      await expect(main.getByTestId("push-device")).toHaveCount(2);
      await main.getByTestId("tiers-more").click();
      await main.getByRole("switch", { name: "Quiet hours" }).click();
      await expect(main.getByTestId("schedule-quiet-times")).toBeVisible();
    },
  },
  {
    name: "notification settings on a small screen",
    open: async ({ page }) => {
      await fakePush(page.context(), fakeSubscription("a11y-phone"));
      await page.setViewportSize({ width: 375, height: 812 });
      await page.goto("/settings/notifications");
      await expect(page.locator("#main").getByTestId("push-browser")).toHaveAttribute("data-state", "off");
    },
  },
  {
    name: "accounts with no account yet",
    open: async ({ page }) => {
      await page.goto("/accounts");
      await expect(page.locator("#main").getByTestId("state-empty")).toBeVisible();
    },
  },
  {
    name: "accounts' service credentials tab with a merged line open",
    open: async ({ page, me }) => {
      await seedAccounts(me);
      await page.goto("/accounts?tab=services");
      const services = page.locator("#main").getByTestId("accounts-group-service");
      await services.getByTestId("service-group-toggle").click();
      await expect(services.getByTestId("service-variables")).toBeVisible();
    },
  },
  {
    name: "accounts on a small screen",
    open: async ({ page, me }) => {
      await seedAccounts(me);
      await page.setViewportSize({ width: 375, height: 812 });
      await page.goto("/accounts");
      await expect(page.locator("#main").getByTestId("provider-card").first()).toBeVisible();
    },
  },
  {
    name: "accounts' provider cards with a provider account, and its Models tab",
    open: async ({ page, me }) => {
      await seedAccounts(me);
      await putProviderByApi(me, "glm-a11y", { provider: "glm", value: providerKey(), projects: [me.projects[0]], models: ["fake-large"] });
      await page.goto("/accounts");
      await expect(page.locator("#main").locator('[data-provider-id="glm"]').getByTestId("provider-card-model")).toBeVisible();
      await expectNoSeriousViolations(page, "accounts with a provider account");
      await page.locator("#main").getByTestId("accounts-tab-models").click();
      await expect(page.locator("#main").getByTestId("runnable-model").first()).toBeVisible();
    },
  },
  {
    name: "accounts' provider catalog, then the provider form with its errors",
    open: async ({ page }) => {
      await page.goto("/accounts");
      await page.locator("#main").getByTestId("accounts-add").click();
      const dialog = page.getByTestId("provider-dialog");
      await expect(dialog.getByTestId("provider-catalog-entry").first()).toBeVisible();
      await expectNoSeriousViolations(page, "the provider catalog");
      await dialog.getByTestId("provider-catalog-search").fill("glm");
      await dialog.getByTestId("provider-catalog-entry").and(page.locator('[data-provider-id="glm"]')).click();
      await dialog.getByTestId("provider-save").click();
      await expect(dialog.getByTestId("provider-key-error")).toBeVisible();
    },
  },
  {
    name: "accounts shared with a member and a unit, a share ending soon, the cost by member, and the share dialog with the terms warning",
    open: async ({ page, me }) => {
      await seedAccounts(me);
      const lan = await otherMember("lan-tran");
      const unit = await unitByApi("a11y-share");
      const projects = [me.projects[0]];
      await shareByApi(me, "claude-sub", { grantee_kind: "user", login: lan.login, projects, budget_usd: 40, terms_ack: true });
      await shareByApi(me, "anthropic-key", { grantee_kind: "unit", unit, projects, expires_at: `${dayFromNow(2)}T00:00:00Z` });
      await page.goto("/accounts?tab=sharing");
      const shares = page.locator("#main").getByTestId("account-shares");
      await expect(shares.getByTestId("share-row")).toHaveCount(2);
      await expect(shares.getByTestId("share-ends-soon")).toBeVisible();
      await expect(page.locator("#main").getByTestId("accounts-cost-empty")).toBeVisible();
      await expectNoSeriousViolations(page, "accounts with their shares");
      await shares.getByTestId("share-open").click();
      const dialog = page.getByTestId("share-dialog");
      await dialog.getByTestId("share-secret").selectOption("claude-sub");
      await expect(dialog.getByTestId("share-terms-text")).toBeVisible();
      await dialog.getByTestId("share-kind").selectOption("unit");
      await expect(dialog.getByTestId("share-save")).toBeDisabled();
      await dialog.getByTestId("share-terms-ack").check();
      await dialog.getByTestId("share-save").click(); // shows the field errors
      await expect(dialog.getByTestId("share-unit-error")).toBeVisible();
    },
  },
  {
    name: "worker detail with its Sharing card: a unit, a high doctor finding and the switch locked",
    open: async ({ page, me }) => {
      const live = await liveWorker(me, me.projects[0], uniqueName("studio"));
      const unit = await unitByApi("a11y-worker");
      await doctorReport(live, CLEAN_REPORT);
      await narrowByApi(me, live, unit);
      await doctorReport(live, HIGH_REPORT);
      await page.goto(`/workers/${live.worker.id}`);
      const card = page.locator("#main").getByTestId("worker-sharing");
      await expect(card.getByTestId("worker-doctor-high")).toBeVisible();
      await expect(card.getByTestId("worker-shared-locked")).toBeVisible();
      await expect(card.locator(`[data-unit="${unit}"]`)).toBeVisible();
    },
  },
  {
    name: "runs with no run yet",
    open: async ({ page, me }) => {
      await page.goto(`/p/${me.projects[0]}/runs`);
      await expect(page.locator("#main").getByTestId("state-empty")).toBeVisible();
    },
  },
  {
    name: "runs list with its summary, facets and a failed run",
    open: async ({ page, me }) => {
      await seedRuns(me);
      // Every run: the page opens on Running, and these two are queued and failed.
      await open(page, `/p/${me.projects[0]}/runs?state=all`);
      await expect(page.locator("#main").getByTestId("runs-table").locator("tbody tr")).toHaveCount(2);
      await expect(page.locator("#main").getByTestId("runs-summary")).toBeVisible();
    },
  },
  {
    name: "runs list with a chat session open into its turns",
    open: async ({ page, me }) => {
      await seedChatSession(me);
      await open(page, `/p/${me.projects[0]}/runs`);
      await page.locator("#main").getByTestId("runs-session-toggle").click();
      await expect(page.locator("#main").getByTestId("session-turn")).toHaveCount(3);
    },
  },
  {
    name: "a turn of a chat session with its session's title and the other turns",
    open: async ({ page, me }) => {
      const [, second] = await seedChatSession(me);
      await open(page, runPath(me.projects[0], second));
      await expect(page.locator("#main").getByTestId("run-turn-link")).toHaveCount(3);
    },
  },
  {
    name: "dispatch dialog with ready, waiting and folded steps",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await seedRunPlan(me, project);
      await liveWorker(me, project, uniqueName("a11y"));
      await open(page, `/p/${project}/runs`);
      await page.locator("#main").getByTestId("runs-empty-dispatch").click();
      const dialog = page.getByTestId("dispatch-dialog");
      await dialog.getByTestId("dispatch-step-2").click();
      await dialog.getByTestId("dispatch-settled").locator("summary").click();
      await expect(dialog.getByTestId("dispatch-worker-select")).not.toHaveValue("any");
      await expectNoSeriousViolations(page, "dispatch, worker filled in");
      // A click moves the pointer and clicks at once, so the timer of the hover could open the tip just before the
      // click toggles it closed. Once the hover has shown it, a click keeps it shown.
      const info = dialog.getByTestId("dispatch-runtime-info");
      await info.hover();
      await expect(info).toHaveAttribute("data-state", /open/);
      await info.click();
      await expect(info).toHaveAttribute("data-state", /open/);
      await expectNoSeriousViolations(page, "dispatch, an info tip shown");
    },
  },
  {
    name: "agents list with the default and a limited agent, then the deleted facet",
    open: async ({ page, me }) => {
      await seedAgents(me);
      const project = me.projects[0];
      await open(page, agentsPath(project));
      await expect(page.locator("#main").getByTestId("agents-table").locator("tbody tr")).toHaveCount(2);
      await expectNoSeriousViolations(page, "agents list");
      await page.locator("#main").getByTestId("agents-facet").getByRole("button", { name: /^Deleted/ }).click();
      await expect(page.locator("#main").getByTestId("agents-table").getByTestId("agent-deleted")).toBeVisible();
    },
  },
  {
    name: "agent page with its avatar, permissions, versions and runs, read by a writer",
    open: async ({ page, me }) => {
      const handle = await seedAgents(me);
      await new HubAdmin().grant(me.projects[0], me.login, "writer", "customer");
      await open(page, agentPath(me.projects[0], handle));
      await expect(page.locator("#main").getByTestId("agent-avatar-card").locator("img")).toBeVisible();
      await expect(page.locator("#main").getByTestId("agent-version-row")).toHaveCount(2);
      await expect(page.locator("#main").getByTestId("agent-runs-table")).toBeVisible();
      await expect(page.locator("#main").getByTestId("agents-read-only")).toBeVisible();
      await expect(page.locator("#main").getByTestId("agent-accounts-table")).toBeVisible();
    },
  },
  {
    name: "agent page's accounts with the add form and its errors, then the delete confirmation, for an admin",
    open: async ({ page, me }) => {
      const handle = await seedAgents(me);
      const project = me.projects[0];
      await open(page, agentPath(project, handle));
      const card = page.locator("#main").getByTestId("agent-accounts");
      await expect(card.getByTestId("agent-accounts-table")).toBeVisible();
      await expectNoSeriousViolations(page, "agent page with its accounts");
      await card.getByTestId("agent-account-add").click();
      const dialog = page.getByTestId("agent-account-dialog");
      await dialog.getByTestId("agent-account-dialog-save").click(); // shows the field errors
      await expect(dialog.getByTestId("agent-account-name-error")).toBeVisible();
      await expect(dialog.getByTestId("agent-account-workers")).toBeVisible();
      await expectNoSeriousViolations(page, "add agent account form");
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      await card.getByTestId("agent-account-delete").click();
      await expect(page.getByTestId("agent-account-delete-dialog")).toBeVisible();
    },
  },
  {
    name: "accounts with the agents' accounts group",
    open: async ({ page, me }) => {
      await seedAccounts(me);
      await seedAgents(me);
      await page.goto("/accounts");
      await expect(page.locator("#main").getByTestId("accounts-group-agent").getByTestId("agent-account-agent")).toBeVisible();
    },
  },
  {
    name: "an agent's memories on its page and in the project's list, and an archived one with Restore, read by an admin",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      const handle = uniqueName("keeper");
      await createAgentByApi(me, project, { handle, name: "Keeper" });
      const api = await apiOf(me);
      const read = await putMemory(api, { project, agent: handle, name: "claims.md", body: memoryFile("Claims", "Retries", "x") });
      const archived = await putMemory(api, { project, agent: handle, name: "old.md", body: memoryFile("Old", "Before", "y") });
      await markAgentMemory(read.id, { read: true, stale: true });
      await markAgentMemory(archived.id, { archived: true });
      await open(page, agentPath(project, handle));
      await expect(page.locator("#main").getByTestId("agent-memories-table").locator("[data-memory-name]")).toHaveCount(2);
      await expectNoSeriousViolations(page, "an agent's memories");
      await open(page, `/p/${project}/memories?agent=${handle}`);
      await expect(page.locator("#main").getByTestId("facet-agent")).toBeVisible();
      await expect(page.locator("#main").getByTestId("memories-table").locator("[data-memory-name]")).toHaveCount(2);
      await expectNoSeriousViolations(page, "the memories of an agent");
      await open(page, `/p/${project}/memories/${archived.id}`);
      await expect(page.locator("#main").getByTestId("memory-restore")).toBeVisible();
    },
  },
  {
    name: "a Curator's memory proposal with its promotion, and the shared memory it wrote, read by a writer",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      const owner = newAccount("owner");
      await new HubAdmin().grant(project, owner.login, "admin", "internal");
      const { shared, proposal } = await promotionNight(owner, project, uniqueName("a11y"), uniqueName("scout"));
      await open(page, `/p/${project}/memories/${shared.id}`);
      await expect(page.locator("#main").getByTestId("provenance-proposal").first()).toBeVisible();
      await expectNoSeriousViolations(page, "a memory a promotion wrote");
      await open(page, `/p/${project}/curator/proposals/${proposal.id}`);
      await expect(page.locator("#main").getByTestId("proposal-promotion")).toBeVisible();
      await expect(page.locator("#main").getByTestId("promotion-written-link")).toBeVisible();
    },
  },
  {
    name: "new agent form with its errors and every allowlist open, the edit form and the delete confirmation",
    open: async ({ page, me }) => {
      const handle = await seedAgents(me);
      const project = me.projects[0];
      await open(page, agentsPath(project));
      await page.locator("#main").getByTestId("agents-new").click();
      const dialog = page.getByTestId("agent-dialog");
      await dialog.getByTestId("agent-handle-input").fill(project);
      await dialog.getByTestId("agent-save").click(); // shows the field errors
      await expect(dialog.getByTestId("agent-form-problems")).toBeVisible();
      for (const axis of ["credential", "tool", "skill", "worker", "project", "model", "runtime"]) {
        await dialog.getByTestId(`agent-axis-${axis}-allowlist`).click();
      }
      await dialog.getByTestId("agent-axis-tool-typed").fill("shell");
      await dialog.getByTestId("agent-axis-tool-add").click();
      await expect(dialog.getByTestId("agent-axis-tool").getByTestId("agent-field-error")).toBeVisible();
      await expectNoSeriousViolations(page, "new agent form");
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      await open(page, agentPath(project, handle));
      await page.locator("#main").getByTestId("agent-edit").click();
      await expect(page.getByTestId("agent-dialog")).toHaveAttribute("data-mode", "edit");
      await expectNoSeriousViolations(page, "edit agent form");
      await page.keyboard.press("Escape");
      await page.locator("#main").getByTestId("agent-delete").click();
      await expect(page.getByTestId("agent-delete-dialog")).toBeVisible();
    },
  },
  {
    name: "dispatch dialog with an agent that refuses the runtime picked",
    open: async ({ page, me }) => {
      const handle = await seedAgents(me);
      const project = me.projects[0];
      await liveWorker(me, project, uniqueName("a11y"));
      await open(page, `/p/${project}/runs`);
      await page.locator("#main").getByTestId("runs-dispatch").click();
      const dialog = page.getByTestId("dispatch-dialog");
      await dialog.getByTestId("dispatch-step-4").click();
      await dialog.getByTestId("dispatch-agent-select").selectOption(handle);
      await dialog.getByRole("radio", { name: /^Codex CLI/ }).check();
      await expect(dialog.getByTestId("dispatch-agent-problem").first()).toBeVisible();
      await expect(dialog.getByTestId("dispatch-agent-blocked")).toBeVisible();
    },
  },
  {
    name: "agents on a small screen",
    open: async ({ page, me }) => {
      const handle = await seedAgents(me);
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, agentsPath(me.projects[0]));
      await expect(page.locator("#main").getByTestId("agents-table")).toHaveAttribute("data-layout", "list");
      await expectNoSeriousViolations(page, "agents at 375 px");
      await open(page, agentPath(me.projects[0], handle));
      await expect(page.locator("#main").getByTestId("agent-permissions")).toBeVisible();
    },
  },
  {
    name: "chats list with an unread chat beside the empty pane, then the new chat form with its errors",
    open: async ({ page, me }) => {
      const { chat } = await seedChat(me);
      await open(page, "/chats");
      // The agent's reply; Mai's came before the member's own message, which marks the chat read up to it.
      await expect(page.locator("#main").locator(`[data-testid="chat-row"][data-chat-id="${chat.id}"]`)).toHaveAttribute("data-unread", "1");
      await expect(page.locator("#main").getByTestId("chats-none-open")).toBeVisible();
      await expectNoSeriousViolations(page, "chats list");
      await page.locator("#main").getByTestId("chats-new").click();
      const dialog = page.getByTestId("new-chat-dialog");
      await dialog.getByTestId("people-search").fill("not a login");
      await expect(dialog.getByTestId("people-empty")).toBeVisible();
      await dialog.getByTestId("new-chat-submit").click();
      await expect(dialog.getByTestId("new-chat-title-hint")).toHaveText("Give the chat a title.");
    },
  },
  {
    name: "a chat with a mention the hub answered, reactions, an agent's reply with its run and pull request in a thread, and the @ suggestions",
    open: async ({ page, me }) => {
      const { chat, asked } = await seedChat(me);
      await open(page, `${chatPath(chat.id)}?thread=${asked.id}`);
      const thread = page.getByTestId("chat-thread");
      await expect(thread).toHaveAttribute("data-state", "ready");
      await expect(thread.getByTestId("message-pull-link")).toBeVisible();
      await expect(thread.getByTestId("message-run-link")).toBeVisible();
      await expectNoSeriousViolations(page, "a chat with its thread open");
      await thread.getByTestId("chat-thread-close").click();
      await expect(page.locator("#main").getByTestId("mention-refused")).toBeVisible();
      await expect(page.locator("#main").getByTestId("mention-run-link")).toBeVisible();
      await expect(page.locator("#main").getByTestId("reaction-chip").first()).toBeVisible();
      await page.locator("#main").getByTestId("chat-composer-text").pressSequentially("@");
      await expect(page.locator("#main").getByTestId("mention-menu")).toBeVisible();
    },
  },
  {
    name: "a chat whose mentions' runs are on their way: one opening a repo, one waiting for its worker",
    open: async ({ page, me }) => {
      const chat = await seedChatProgress(me);
      await open(page, chatPath(chat.id));
      const steps = page.locator("#main").getByTestId("message-progress-step");
      await expect(steps).toHaveCount(2);
      await expect(steps.first()).toHaveAttribute("data-step", "opening");
      await expect(steps.last()).toHaveAttribute("data-step", "waiting");
      await expectNoSeriousViolations(page, "a chat with its runs' progress");
    },
  },
  {
    name: "a chat's members and the leave confirmation",
    open: async ({ page, me }) => {
      const { chat } = await seedChat(me);
      await open(page, chatPath(chat.id));
      await page.locator("#main").getByTestId("chat-members-open").click();
      await expect(page.getByTestId("chat-members").getByTestId("chat-member-person")).toHaveCount(2);
      await expectNoSeriousViolations(page, "members sheet");
      await page.getByTestId("chat-members").getByTestId("chat-leave").click();
      await expect(page.getByTestId("chat-leave-confirm")).toBeVisible();
    },
  },
  {
    name: "a direct chat with its agent's head, the member's messages and the agent's answers as bubbles, a reply being written, then its details",
    open: async ({ page, me }) => {
      const chat = await seedDirectChat(me);
      await open(page, chatPath(chat.id));
      const main = page.locator("#main");
      await expect(main.getByTestId("chat-head")).toHaveAttribute("data-kind", "direct");
      await expect(main.locator('[data-testid="agent-strip-agent"][aria-current="true"]')).toHaveCount(1);
      await expect(main.locator('[data-testid="chat-message"][data-mine="true"]')).toHaveCount(3);
      await expect(main.getByTestId("message-quote")).toBeVisible();
      await expect(main.getByTestId("message-progress-step")).toBeVisible();
      await expect(main.getByTestId("message-pull-link")).toBeVisible();
      await expectNoSeriousViolations(page, "a direct chat");
      const answer = main.locator('[data-testid="chat-message"][data-author="agent"]').first();
      await answer.hover();
      await answer.getByTestId("message-reply").click();
      await expect(main.getByTestId("chat-composer-replying")).toBeVisible();
      await expectNoSeriousViolations(page, "a direct chat with a reply being written");
      await main.getByTestId("chat-details-open").click();
      await expect(page.getByTestId("chat-direct-note")).toBeVisible();
    },
  },
  {
    name: "a group chat with its image, a file its head refuses, then its image menu",
    open: async ({ page, me }) => {
      const { chat } = await seedChat(me);
      await putChatImageByApi(me, chat.id, PIXEL_PNG);
      await open(page, chatPath(chat.id));
      const head = page.locator("#main").getByTestId("chat-head");
      await expect(head.getByTestId("chat-image")).toBeVisible();
      await head.getByTestId("chat-image-input").setInputFiles({ name: "face.gif", mimeType: "image/gif", buffer: Buffer.from("GIF89a") });
      await expect(head.getByTestId("chat-image-problem")).toBeVisible();
      await expectNoSeriousViolations(page, "a chat's image refused");
      await head.getByTestId("chat-image-button").click();
      await expect(page.getByTestId("chat-image-menu")).toBeVisible();
    },
  },
  {
    name: "chats on a small screen",
    open: async ({ page, me }) => {
      const { chat } = await seedChat(me);
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, "/chats");
      await expect(page.locator("#main").getByTestId("chat-row")).toHaveCount(1);
      await expectNoSeriousViolations(page, "chats at 375 px");
      await open(page, chatPath(chat.id));
      await expect(page.locator("#main").getByTestId("chat-back")).toBeVisible();
      await expect(page.locator("#main").getByTestId("chats-list")).toBeHidden();
    },
  },
  {
    name: "plan step with its runs and the Run button",
    open: async ({ page, me }) => {
      await seedRuns(me);
      await open(page, `/p/${me.projects[0]}/plans/${RUN_PLAN}/steps/4`);
      await expect(page.locator("#main").getByTestId("step-runs-table")).toBeVisible();
      await expect(page.locator("#main").getByTestId("step-run")).toBeVisible();
    },
  },
  {
    name: "runs on a small screen",
    open: async ({ page, me }) => {
      const { live } = await seedRuns(me);
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, `/p/${me.projects[0]}/runs?state=all`);
      await expect(page.locator("#main").getByTestId("runs-table")).toBeVisible();
      await expectNoSeriousViolations(page, "runs at 375 px");
      await open(page, `/workers/${live.worker.id}`);
      await expect(page.locator("#main").getByTestId("worker-runs-table")).toBeVisible();
    },
  },
  {
    name: "insights with no run ended yet",
    open: async ({ page, me }) => {
      await open(page, `/p/${me.projects[0]}/insights`);
      await expect(page.locator("#main").getByTestId("state-empty")).toBeVisible();
    },
  },
  {
    name: "insights with its four charts, a chart's table and a tooltip",
    open: async ({ page, me }) => {
      await seedInsights(me, me.projects[0]);
      await open(page, `/p/${me.projects[0]}/insights`);
      const main = page.locator("#main");
      for (const chart of ["outcomes", "failure", "duration", "tokens"]) {
        await expect(main.getByTestId(`insights-${chart}-chart`).locator("svg.recharts-surface")).toBeVisible();
      }
      await expectNoSeriousViolations(page, "insights, charts");
      await main.getByTestId("insights-outcomes-view-table").click();
      await expect(main.getByTestId("insights-outcomes-table-region")).toBeVisible();
      await main.getByTestId("insights-duration-chart").locator("svg.recharts-surface").focus();
      await expect(main.getByTestId("insights-duration").getByTestId("insights-tooltip")).toBeVisible();
    },
  },
  {
    name: "insights on a small screen",
    open: async ({ page, me }) => {
      await seedInsights(me, me.projects[0]);
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, `/p/${me.projects[0]}/insights`);
      const main = page.locator("#main");
      await expect(main.getByTestId("insights-tokens-chart").locator("svg.recharts-surface")).toBeVisible();
      await main.getByTestId("insights-tokens-view-table").click();
      await expect(main.getByTestId("insights-tokens-table-region")).toBeVisible();
    },
  },
  {
    name: "command palette with actions, runs, plans, workers and pages, and with no match",
    open: async ({ page, me }) => {
      const { project } = await seedRuns(me);
      await open(page, `/p/${project}/runs`);
      await expect(page.getByTestId("palette-trigger-key")).toBeAttached(); // the shell has hydrated
      await page.keyboard.press("ControlOrMeta+k");
      const palette = page.getByTestId("command-palette");
      for (const group of ["actions", "runs", "plans", "workers", "goto"]) {
        await expect(palette.getByTestId(`palette-group-${group}`)).toBeVisible();
      }
      await expect(palette.locator('[data-item^="action:rerun:"]')).toBeVisible();
      await expectNoSeriousViolations(page, "command palette");
      await palette.getByRole("combobox").fill("no-such-thing-here");
      await expect(palette.getByTestId("palette-empty")).toBeVisible();
    },
  },
  {
    name: "command palette on a small screen",
    open: async ({ page, me }) => {
      await seedRuns(me);
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, `/p/${me.projects[0]}/runs`);
      await expect(page.getByTestId("palette-trigger-key")).toBeAttached();
      await page.getByTestId("palette-trigger").click();
      await expect(page.getByTestId("command-palette").getByTestId("palette-group-runs")).toBeVisible();
    },
  },
  {
    name: "keyboard shortcuts dialog, and the keys beside the runs page's Dispatch and a sidebar item",
    open: async ({ page, me }) => {
      await seedRuns(me);
      await open(page, `/p/${me.projects[0]}/runs`);
      await expect(page.getByTestId("palette-trigger-key")).toBeAttached(); // the shell has hydrated
      await expect(page.locator('#main [data-testid="runs-dispatch"] [data-shortcut="dispatch"]')).toBeVisible();
      await page.getByTestId("nav-workers").hover();
      await expect(page.locator('[data-testid="nav-workers"] [data-shortcut="goWorkers"]')).toBeVisible();
      await expectNoSeriousViolations(page, "the keys beside their actions");
      await page.keyboard.press("Shift+Slash");
      await expect(page.getByTestId("shortcuts-dialog")).toBeVisible();
    },
  },
  {
    name: "keyboard shortcuts dialog on a small screen, single keys off",
    open: async ({ page }) => {
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, "/");
      await expect(page.getByTestId("palette-trigger-key")).toBeAttached();
      await page.getByTestId("palette-trigger").click();
      await expect(page.getByTestId("command-palette").getByRole("combobox")).toBeFocused();
      await page.keyboard.type("shortcuts");
      await page.keyboard.press("Enter");
      const dialog = page.getByTestId("shortcuts-dialog");
      await expect(dialog).toBeVisible();
      await dialog.getByTestId("shortcuts-single").click();
      await expect(dialog.locator('[data-shortcut="search"]')).toHaveAttribute("data-off", "true");
    },
  },
  {
    name: "run page with its live log, the owner's controls and the Take over dialog",
    open: async ({ page, me }) => {
      const { project, run } = await seedLiveRun(me);
      await open(page, runPath(project, run.id));
      await expect(page.locator("#main").getByTestId("log-status")).toHaveAttribute("data-status", "live");
      // The failed call open under its group's row, the row then opened on its calls.
      await expect(page.locator("#main").getByTestId("trace-tool")).toHaveAttribute("open", "");
      await page.locator("#main").getByTestId("trace-group-toggle").click();
      await expect(page.locator("#main").getByTestId("trace-group-toggle")).toHaveAttribute("aria-expanded", "true");
      await expect(page.locator("#main").getByTestId("trace-raw-left")).toContainText("1 runtime event");
      await expect(page.locator("#main").getByTestId("run-composer")).toBeVisible();
      await page.locator("#main").getByTestId("trace-thought").getByText(/^Thought/).click();
      await expectNoSeriousViolations(page, "run page, its trace");
      await openRunDetails(page);
      await expect(page.locator("#main").getByTestId("run-usage-total")).toBeVisible();
      await expectNoSeriousViolations(page, "run page, its trace and its details");
      await page.locator("#main").getByRole("tab", { name: "Raw log" }).click();
      await expect(page.locator("#main").getByTestId("log-line").first()).toBeVisible();
      await page.locator("#main").getByTestId("log-search").fill("plan");
      await expect(page.locator("#main").getByTestId("log-lines").locator("mark").first()).toBeVisible();
      await expectNoSeriousViolations(page, "run page, its raw log");
      await page.locator("#main").getByTestId("run-takeover").click();
      await expect(page.getByTestId("takeover-dialog")).toBeVisible();
    },
  },
  {
    name: "run in review with its result, and its diff page",
    open: async ({ page, me }) => {
      const { project, live, run } = await seedLiveRun(me);
      await runToReview(live, run.id);
      await uploadDiff(live, run.id, `diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old ${run.id}\n+new ${run.id}\n`);
      await open(page, runPath(project, run.id));
      await expect(page.locator("#main").getByTestId("run-approve")).toBeVisible();
      await expect(page.locator("#main").getByTestId("run-view-diff")).toBeVisible();
      await openRunDetails(page);
      await expect(page.locator("#main").getByTestId("run-verify")).toBeVisible();
      await expectNoSeriousViolations(page, "run in review");
      await page.locator("#main").getByTestId("run-actions-menu").click();
      await expect(page.getByTestId("run-cancel")).toBeVisible();
      await expectNoSeriousViolations(page, "run in review, its menu of actions");
      await page.keyboard.press("Escape");
      await chooseRunAction(page, "run-cancel");
      await expect(page.getByTestId("cancel-run-dialog")).toBeVisible();
      await expectNoSeriousViolations(page, "cancel run dialog");
      await page.keyboard.press("Escape");
      await open(page, `${runPath(project, run.id)}/diff`);
      await expect(page.locator("#main").getByTestId("diff-file")).toHaveCount(1);
    },
  },
  {
    name: "run page with the credentials it got",
    open: async ({ page, me }) => {
      const { project, live, run } = await seedLiveRun(me);
      await putSecretByApi(me, "claude-oauth", { kind: "env", env_var: "CLAUDE_CODE_OAUTH_TOKEN", projects: [project], value: secretValue("a11y") });
      await leaseCredentials(live, run.id);
      await open(page, runPath(project, run.id));
      await openRunDetails(page, { all: true });
      await expect(page.locator("#main").getByTestId("run-lease-item")).toHaveCount(1);
    },
  },
  {
    name: "run page on a small screen",
    open: async ({ page, me }) => {
      const { project, run } = await seedLiveRun(me);
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, runPath(project, run.id));
      // The overview: the head, the trace's latest rows and the way to the full trace; then the trace's own screen.
      await expect(page.locator("#main").getByTestId("run-trace-preview-row").first()).toBeVisible();
      await expectNoSeriousViolations(page, "run page at 375 px, its overview");
      await openRunDetails(page);
      await expect(page.locator("#main").getByTestId("run-side")).toBeVisible();
      await expectNoSeriousViolations(page, "run page at 375 px, its details");
      await page.locator("#main").getByTestId("run-open-trace").click();
      await expect(page.locator("#main").getByTestId("run-detail")).toHaveAttribute("data-screen", "session");
      await expect(page.locator("#main").getByTestId("trace-item").first()).toBeVisible();
    },
  },
  {
    name: "plans list with a plan run waiting and Run plan",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await seedPlanRunPlan(me, project);
      await planRunUnderway(me, project, uniqueName("a11y"), { waiting: true });
      await seedRunPlan(me, project);
      await open(page, `/p/${project}/plans`);
      await expect(page.locator("#main").getByTestId("plan-run-link")).toBeVisible();
      await expect(page.locator("#main").getByTestId("plans-run-plan")).toHaveCount(2);
    },
  },
  {
    name: "plan page with Run plan and its plan run waiting for a decision",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await seedPlanRunPlan(me, project);
      await planRunUnderway(me, project, uniqueName("a11y"), { waiting: true });
      await open(page, `/p/${project}/plans/${PLAN_RUN_PLAN}`);
      await expect(page.locator("#main").getByTestId("plan-run-banner")).toHaveAttribute("data-phase", "waiting");
      await expect(page.locator("#main").getByTestId("plan-run-banner-decision")).toBeVisible();
      await expect(page.locator("#main").getByTestId("run-plan-lock")).toBeVisible();
    },
  },
  {
    name: "Run plan dialog, and a model that needs a runtime",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await seedPlanRunPlan(me, project);
      await planRunWorker(me, project, uniqueName("a11y"));
      await open(page, `/p/${project}/plans/${PLAN_RUN_PLAN}`);
      await page.locator("#main").getByTestId("run-plan").click();
      const dialog = page.getByTestId("plan-run-dialog");
      await expect(dialog.getByTestId("plan-run-summary")).toBeVisible();
      await expect(dialog.getByTestId("dispatch-worker-select")).not.toHaveValue("any");
      await expectNoSeriousViolations(page, "run plan dialog");
      await dialog.getByTestId("plan-run-model-select").selectOption({ label: "Another model…" });
      await dialog.getByTestId("plan-run-model-input").fill("sonnet");
      await expect(dialog.getByTestId("plan-run-model-error")).toBeVisible();
    },
  },
  {
    name: "plan run page with its plan's steps and the decision it waits on",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await seedPlanRunPlan(me, project);
      const { run } = await planRunUnderway(me, project, uniqueName("a11y"), { waiting: true });
      await open(page, runPath(project, run.id));
      await openRunDetails(page);
      await openPlanStepFolds(page.locator("#main"));
      await expect(page.locator("#main").getByTestId("run-plan-step")).toHaveCount(4);
      await expect(page.locator("#main").getByTestId("run-decision-link")).toBeVisible();
    },
  },
  {
    name: "plan run pages on a small screen",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await seedPlanRunPlan(me, project);
      const { run } = await planRunUnderway(me, project, uniqueName("a11y"), { waiting: true });
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, `/p/${project}/plans/${PLAN_RUN_PLAN}`);
      await expect(page.locator("#main").getByTestId("plan-run-banner")).toBeVisible();
      await expectNoSeriousViolations(page, "plan page with a plan run at 375 px");
      await open(page, runPath(project, run.id));
      await expect(page.locator("#main").getByTestId("run-trace-preview")).toBeVisible();
      await openRunDetails(page);
      await expect(page.locator("#main").getByTestId("run-plan-steps")).toBeVisible();
    },
  },
  {
    name: "inbox with open decisions first, a push notice and its filters",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await seedPlanRunPlan(me, project);
      const { live, run } = await planRunUnderway(me, project, uniqueName("a11y"), { waiting: true });
      await sendNotice(live, run.id, {
        kind: "push_default_branch",
        title: `Pushed to ${HARNESS_REPO} main`,
        body: "The catalog of use cases now lists plan runs.\n\nSecond paragraph.\nThird line.\nFourth line.",
        repo: HARNESS_REPO,
        branch: "main",
        commits: [COMMIT],
      });
      await open(page, "/inbox");
      await expect(page.locator("#main").getByTestId("inbox-waiting").getByTestId("notification")).toHaveCount(1);
      await page.locator("#main").getByTestId("notification-toggle").click();
      await expect(page.locator("#main").getByTestId("notice-commits")).toBeVisible();
      await expect(page.getByTestId("inbox-bell")).toHaveAttribute("data-unread", "2");
      await page.locator("#main").getByTestId("notification-body-toggle").click();
      await expect(page.locator("#main").getByTestId("notification-body-toggle")).toHaveAttribute("aria-expanded", "true");
    },
  },
  {
    name: "inbox with no notification",
    open: async ({ page }) => {
      await open(page, "/inbox");
      await expect(page.locator("#main").getByTestId("state-empty")).toBeVisible();
    },
  },
  {
    name: "decision in the inbox with its answer form and a refused empty answer",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await seedPlanRunPlan(me, project);
      const { decision } = await planRunUnderway(me, project, uniqueName("a11y"), { waiting: true });
      await open(page, `/inbox?decision=${decision}`);
      const sheet = page.getByTestId("decision-sheet");
      await expect(sheet.getByTestId("decision-form")).toBeVisible();
      await expect(sheet.getByTestId("decision-context-markdown")).toBeVisible();
      await expect(sheet.getByTestId("decision-parks")).toBeVisible();
      await expectNoSeriousViolations(page, "decision with its answer form");
      // The agent's pick is chosen for the owner; clear it to send nothing.
      await sheet.getByTestId("decision-clear-option").click();
      await sheet.getByTestId("decision-send").click();
      await expect(sheet.getByTestId("decision-problem")).toBeVisible();
    },
  },
  {
    name: "decision answered, with the answer and the options it chose",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await seedPlanRunPlan(me, project);
      const { decision } = await planRunUnderway(me, project, uniqueName("a11y"), { waiting: true });
      await open(page, `/inbox?decision=${decision}`);
      const sheet = page.getByTestId("decision-sheet");
      await sheet.getByRole("radio").first().check();
      await sheet.getByTestId("decision-text").fill("After the backup.");
      await sheet.getByTestId("decision-send").click();
      await expect(sheet.getByTestId("decision-answer")).toBeVisible();
      // The answer's toast is on screen while axe looks.
      await expect(page.getByTestId("toast").first()).toBeVisible();
    },
  },
  {
    name: "plan run page waiting for a decision, with the answer form in its side column",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await seedPlanRunPlan(me, project);
      const { live, run } = await planRunUnderway(me, project, uniqueName("a11y"), { waiting: true });
      await askDecision(live, run.id, "Drop the old staging bucket after the deploy?", "4");
      await open(page, runPath(project, run.id));
      await openRunDetails(page);
      await expect(page.locator("#main").getByTestId("run-aside").getByTestId("run-decisions").getByTestId("decision-form")).toHaveCount(2);
      await expect(page.locator("#main").getByTestId("run-decisions-count")).toBeVisible();
    },
  },
  {
    name: "inbox and a decision on a small screen",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await seedPlanRunPlan(me, project);
      const { decision } = await planRunUnderway(me, project, uniqueName("a11y"), { waiting: true });
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, "/inbox");
      await expect(page.locator("#main").getByTestId("notification")).toHaveCount(1);
      await expectNoSeriousViolations(page, "inbox at 375 px");
      await open(page, `/inbox?decision=${decision}`);
      // The decision is a screen of its own: the phone's whole width, Back to Inbox at hand, the answer's bar at its foot.
      const screen = page.getByTestId("decision-sheet");
      await expect(screen).toHaveAttribute("data-layout", "screen");
      await expect(screen.getByTestId("decision-form")).toBeVisible();
      await expect(screen.getByTestId("decision-back")).toBeVisible();
      await expect(screen.getByTestId("decision-bar")).toBeInViewport();
      expect((await screen.boundingBox())?.width).toBe(375);
    },
  },
  {
    name: "merge queue with a tripped breaker, its merge requests and samples, then a merge request's sheet, for an admin",
    open: async ({ page, me }) => {
      const project = await mergeQueueProject(me);
      const seeded = await seedMergeQueue(project, { admin: me.login });
      await open(page, mergeQueuePath(project));
      await expect(page.locator("#main").getByTestId("breaker-banner")).toBeVisible();
      await expect(page.locator("#main").getByTestId("sample-ok")).toBeVisible();
      await expectNoSeriousViolations(page, "merge queue for an admin");
      await open(page, `${mergeQueuePath(project)}?mr=${seeded.requests["40"]}`);
      await expect(page.getByTestId("merge-request-sheet").getByTestId("merge-release")).toBeVisible();
    },
  },
  {
    name: "merge queue read by a writer, then its figures",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await seedMergeQueue(project);
      await open(page, mergeQueuePath(project));
      await expect(page.locator("#main").getByTestId("merge-queue-read-only")).toBeVisible();
      await expectNoSeriousViolations(page, "merge queue for a writer");
      await open(page, mergeQueuePath(project, "/figures"));
      await expect(page.locator("#main").getByTestId("merge-figures-table")).toBeVisible();
    },
  },
  {
    name: "policy with its revisions and a comparison, then its form with errors",
    open: async ({ page, me }) => {
      const project = await mergeQueueProject(me);
      await open(page, mergeQueuePath(project, "/policy"));
      await expect(page.locator("#main").getByTestId("policy-compare").getByTestId("policy-change")).toHaveCount(3);
      await expectNoSeriousViolations(page, "policy");
      await open(page, `${mergeQueuePath(project, "/policy")}?edit=1`);
      const form = page.locator("#main").getByTestId("policy-form");
      await form.getByTestId("policy-field-policy-merge.pipeline_minutes").fill("0");
      await form.getByTestId("policy-save").click();
      await expect(form.getByTestId("policy-form-problems")).toBeVisible();
    },
  },
  {
    name: "inbox with the merge queue's notices",
    open: async ({ page, me }) => {
      const project = await mergeQueueProject(me);
      await seedMergeQueue(project, { admin: me.login, notify: me.login });
      await open(page, "/inbox");
      await expect(page.locator("#main").getByTestId("merge-notice")).toHaveCount(3);
      await page.locator("#main").getByTestId("merge-notice-faulty-open").click();
      await expect(page.locator("#main").getByTestId("merge-notice-note")).toBeVisible();
    },
  },
  {
    name: "the Loop stopped by its breaker with its work items by state and by chain, and the switch's dialog, for an admin",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      await seedLoopProject(me, project);
      await seedLoop(project, { tripped: true });
      await open(page, loopPath(project));
      await expect(page.locator("#main").getByTestId("loop-breaker-banner")).toBeVisible();
      await expect(page.locator("#main").getByTestId("loop-items-table")).toBeVisible();
      await expectNoSeriousViolations(page, "the Loop by state");
      await open(page, loopPath(project, "?view=chain"));
      await expect(page.locator("#main").getByTestId("loop-chain").first()).toBeVisible();
      await expectNoSeriousViolations(page, "the Loop by chain");
      await page.locator("#main").getByTestId("loop-turn-off").click();
      await expect(page.getByTestId("loop-switch-dialog")).toBeVisible();
    },
  },
  {
    name: "a work item's chain from its goal to its release, and one that waits for a person",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      await seedLoopProject(me, project);
      const seeded = await seedLoop(project);
      await open(page, loopItemPath(project, seeded.items.build));
      await expect(page.locator("#main").getByTestId("chain-rail")).toBeVisible();
      await expectNoSeriousViolations(page, "a released chain");
      await open(page, loopItemPath(project, seeded.items.held));
      await expect(page.locator("#main").getByTestId("chain-waiting")).toBeVisible();
    },
  },
  {
    name: "the goals with their metric against the target, then a goal's page",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      await seedLoopProject(me, project);
      await seedLoop(project);
      await open(page, goalsPath(project));
      await expect(page.locator("#main").getByTestId("goals-table")).toBeVisible();
      await expectNoSeriousViolations(page, "goals");
      await open(page, goalsPath(project, "G-1"));
      await expect(page.locator("#main").getByTestId("goal-items-table")).toBeVisible();
    },
  },
  {
    name: "the problems, a problem's page and its close dialog",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      await seedProblems(me, project);
      await open(page, problemsPath(project));
      await expect(page.locator("#main").getByTestId("problems-table")).toBeVisible();
      await expectNoSeriousViolations(page, "problems");
      await open(page, problemsPath(project, "P-1"));
      await expect(page.locator("#main").getByTestId("problem-metric-strip")).toBeVisible();
      await expect(page.locator("#main").getByTestId("problem-loop").getByTestId("chain-item").first()).toBeVisible();
      await expectNoSeriousViolations(page, "a problem's page");
      await page.locator("#main").getByTestId("problem-close").click();
      await expect(page.getByTestId("problem-close-dialog")).toBeVisible();
    },
  },
  {
    name: "the Decisions page with each gate and the Loop's exceptions",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      await seedLoopProject(me, project);
      await seedLoop(project, { tripped: true });
      await open(page, decidePath(project));
      await expect(page.locator("#main").locator('[data-testid="board-item"][data-kind="loop_breaker"]')).toBeVisible();
    },
  },
  {
    name: "the Loop, a chain, the goals and the Decisions page on a small screen",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      await seedLoopProject(me, project);
      const seeded = await seedLoop(project, { tripped: true });
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, loopPath(project));
      await expect(page.locator("#main").getByTestId("loop-items-table")).toHaveAttribute("data-layout", "list");
      await expectNoSeriousViolations(page, "the Loop at 375 px");
      await open(page, loopItemPath(project, seeded.items.build));
      await expect(page.locator("#main").getByTestId("chain-rail")).toBeVisible();
      await expectNoSeriousViolations(page, "a chain at 375 px");
      await open(page, goalsPath(project));
      await expect(page.locator("#main").getByTestId("goals-table")).toHaveAttribute("data-layout", "list");
      await expectNoSeriousViolations(page, "goals at 375 px");
      await open(page, decidePath(project));
      await expect(page.locator("#main").getByTestId("board-summary")).toBeVisible();
    },
  },
  {
    name: "merge queue and policy on a small screen",
    open: async ({ page, me }) => {
      const project = await mergeQueueProject(me);
      await seedMergeQueue(project, { admin: me.login });
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, mergeQueuePath(project, "/policy"));
      await expect(page.locator("#main").getByTestId("policy-view")).toBeVisible();
      await expectNoSeriousViolations(page, "policy at 375 px");
      await open(page, mergeQueuePath(project));
      await expect(page.locator("#main").getByTestId("merge-queue-table")).toHaveAttribute("data-layout", "list");
    },
  },
  {
    name: "the add form of a GitLab token for the hub",
    open: async ({ page, me }) => {
      const project = uniqueName("forge");
      const hub = new HubAdmin();
      await hub.registerProject(project);
      await hub.grant(project, me.login, "admin", "internal");
      await page.goto("/secrets");
      await page.locator("#main").getByTestId("secrets-empty-add").click();
      const dialog = page.getByTestId("secret-dialog");
      await dialog.getByTestId("secret-kind-forge").click();
      await dialog.getByTestId("secret-save").click();
      await expect(dialog.getByTestId("secret-expires-error")).toBeVisible();
    },
  },
  {
    name: "small screen with the sidebar open",
    open: async ({ page }) => {
      await page.setViewportSize({ width: 375, height: 812 });
      await page.goto("/");
      await page.locator('button[data-sidebar="trigger"]').click();
      await expect(page.getByRole("dialog")).toBeVisible();
    },
  },
  {
    name: "project plugins with one enabled, then the consent of another",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      const [on, off] = [await installedSample("a11y-on"), await installedSample("a11y-off")];
      await enablePlugin(me, project, on);
      await open(page, `/p/${project}/plugins`);
      const table = page.locator("#main").getByTestId("project-plugins-table");
      await expect(table.locator(`[data-plugin-link="${on}"]`)).toBeVisible();
      await expectNoSeriousViolations(page, "project plugins");
      await table.locator("tr", { has: page.locator(`[data-plugin-link="${off}"]`) }).getByTestId("plugin-enable").click();
      await expect(page.getByTestId("plugin-consent")).toBeVisible();
    },
  },
  {
    name: "a plugin of a project with its config form, what it gets and its history",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      const id = await installedSample("a11y-config");
      await enablePlugin(me, project, id);
      await open(page, `/p/${project}/plugins/${id}`);
      const form = page.locator("#main").getByTestId("project-config-form");
      await form.getByTestId("config-field-greeting").fill("hi");
      await form.getByTestId("project-config-form-save").click();
      await expect(page.locator("#main").getByTestId("project-config-history-version")).toHaveCount(1);
      await form.getByTestId("config-secret-api_key-replace").click();
      await expect(page.locator("#main").getByTestId("plugin-effective-config")).toContainText("hi");
    },
  },
  {
    name: "a plugin's page in its sandboxed frame",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      const id = await installedSample("a11y-frame");
      await enablePlugin(me, project, id);
      await open(page, pluginPagePath(project, id));
      await expect(page.frameLocator('[data-testid="plugin-frame"]').locator("#plans")).toHaveText("refused: not_granted");
    },
  },
  {
    name: "the native page of the core plugin notes",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      await enablePlugin(me, project, "notes", ["plans:read"]);
      await open(page, `/p/${project}/x/notes/entries`);
      await expect(page.locator("#main").getByTestId("notes-empty")).toBeVisible();
      await page.locator("#main").getByTestId("notes-form").getByRole("textbox").fill("Ship on Friday");
      await page.locator("#main").getByTestId("notes-add").click();
      await expect(page.locator("#main").getByTestId("notes-list")).toContainText("Ship on Friday");
    },
  },
  {
    name: "SRS documents, and the import with its preview",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      await enableSrs(me, project);
      await importSample(me, project);
      await open(page, srsPath(project));
      await expect(page.locator("#main").getByTestId("srs-documents-table")).toBeVisible();
      await expectNoSeriousViolations(page, "SRS documents");
      await open(page, `/p/${project}/x/srs/import`);
      await page.locator("#main").getByTestId("srs-import-target").selectOption(SAMPLE_SLUG);
      await page.locator("#main").getByTestId("srs-import-markdown").fill(SAMPLE_SRS.replace("Payment retries", "Payment retry rules"));
      await page.locator("#main").getByTestId("srs-import-preview-run").click();
      await expect(page.locator("#main").getByTestId("srs-import-changed")).toBeVisible();
    },
  },
  {
    name: "an SRS document's contents with a use case open, its draft and its editor",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      await enableSrs(me, project);
      await importSample(me, project);
      await open(page, documentPath(project, `?s=${encodeURIComponent(USE_CASE.code)}`));
      await expect(page.locator("#main").getByTestId("srs-steps")).toBeVisible();
      await expect(page.locator("#main").getByTestId("srs-draft")).toBeVisible();
      await expectNoSeriousViolations(page, "SRS document");
      await page.locator("#main").getByTestId("srs-edit").click();
      await expect(page.locator("#main").getByTestId("srs-editor")).toBeVisible();
      await expectNoSeriousViolations(page, "SRS editor");
      await page.locator("#main").getByTestId("srs-version-menu").click();
      await expect(page.getByRole("menu")).toBeVisible();
    },
  },
  {
    name: "a use case's page with its comments and trace, and a document's comments",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      await enableSrs(me, project);
      await importSample(me, project);
      await open(page, itemPath(project, USE_CASE.code));
      await page.locator("#main").getByTestId("srs-comment-form").getByRole("textbox").fill("Is the hold long enough?");
      await page.locator("#main").getByTestId("srs-comment-submit").click();
      await expect(page.locator("#main").getByTestId("srs-thread")).toBeVisible();
      await expect(page.locator("#main").getByTestId("srs-trace")).toBeVisible();
      await expectNoSeriousViolations(page, "SRS use case");
      await open(page, documentPath(project, "/comments"));
      await expect(page.locator("#main").getByTestId("srs-threads")).toBeVisible();
    },
  },
  {
    name: "an SRS version's review with the reviewer's form, its versions and a comparison",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      const author = newAccount("srs-author");
      const hub = new HubAdmin();
      await hub.grant(project, me.login, "admin", "customer");
      await hub.grant(project, author.login, "writer", "customer");
      await enableSrs(me, project);
      await importSample(author, project);
      const v2 = await editItem(author, project, USE_CASE.code, (held) => `${held}\n\nThe cart is kept for a day.`);
      await submitVersion(author, project, v2);
      await open(page, documentPath(project, "/versions"));
      await expect(page.locator("#main").getByTestId("srs-versions-table")).toBeVisible();
      await expectNoSeriousViolations(page, "SRS versions");
      await open(page, documentPath(project, "/compare?from=1&to=2"));
      await expect(page.locator("#main").getByTestId("srs-diff-item").first()).toBeVisible();
      await expectNoSeriousViolations(page, "SRS comparison");
      await open(page, documentPath(project, `/versions/${v2}`));
      await expect(page.locator("#main").getByTestId("srs-review-form")).toBeVisible();
    },
  },
  {
    name: "an SRS version's review with @ba's findings and Approve locked by the one to decide",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      const author = newAccount("srs-author");
      const hub = new HubAdmin();
      await hub.grant(project, me.login, "admin", "customer");
      await hub.grant(project, author.login, "writer", "customer");
      await enableSrs(me, project);
      const seeded = await seedFindings(author, project, me);
      await open(page, documentPath(project, `/versions/${seeded.version}`));
      const card = page.locator("#main").getByTestId("srs-findings");
      await expect(card.getByTestId("srs-findings-decide").getByTestId("srs-finding")).toHaveAttribute("data-finding", String(seeded.decide));
      await expect(card.getByTestId("srs-findings-settled").getByTestId("srs-finding-settled")).toBeVisible();
      await expect(page.getByTestId("srs-review-form").getByTestId("srs-review-lock-chat")).toHaveAttribute("href", seeded.chat);
    },
  },
  {
    name: "SRS pages on a small screen, read as a reader",
    open: async ({ page, me }) => {
      const project = me.projects[1];
      const owner = newAccount("srs-owner");
      const hub = new HubAdmin();
      await hub.grant(project, owner.login, "admin", "customer");
      await hub.grant(project, me.login, "reader", "customer");
      await enableSrs(owner, project);
      await importSample(owner, project);
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, srsPath(project));
      await expect(page.locator("#main").getByTestId("srs-documents-table")).toHaveAttribute("data-layout", "list");
      await expectNoSeriousViolations(page, "SRS documents at 375 px");
      await open(page, documentPath(project, `?s=${encodeURIComponent(USE_CASE.code)}`));
      await expect(page.locator("#main").getByTestId("srs-read-only")).toBeVisible();
      await page.locator("#main").getByTestId("srs-toc-disclosure").locator("summary").click();
      await expect(page.locator("#main").getByTestId("srs-toc-disclosure").getByRole("navigation")).toBeVisible();
    },
  },
  {
    name: "the Tasks board with its cards, and the backlog with the new sprint dialog",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      const mai = newAccount("tasks-mai");
      const hub = new HubAdmin();
      await hub.grant(project, me.login, "admin", "customer");
      await hub.grant(project, mai.login, "writer", "customer");
      await seedTasks(me, project, mai.login);
      await open(page, tasksPath(project));
      await expect(page.locator("#main").getByTestId("task-board")).toBeVisible();
      await expect(page.locator("#main").getByTestId("task-card").first()).toBeVisible();
      await expectNoSeriousViolations(page, "Tasks board");
      await open(page, tasksPath(project, "mine"));
      await expect(page.locator("#main").getByTestId("mine-open-table")).toBeVisible();
      await expectNoSeriousViolations(page, "My work");
      await open(page, tasksPath(project, "backlog"));
      await expect(page.locator("#main").getByTestId("tasks-table").first()).toBeVisible();
      await expectNoSeriousViolations(page, "Tasks backlog");
      await page.locator("#main").getByTestId("sprint-new").click();
      await expect(page.getByTestId("sprint-new-form")).toBeVisible();
    },
  },
  {
    name: "a task's page with its links, history and comments, and its editor",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      await seedTasks(me, project, me.login);
      await open(page, taskPath(project, "T-1"));
      await expect(page.locator("#main").getByTestId("task-links")).toBeVisible();
      await expect(page.locator("#main").getByTestId("task-comment")).toBeVisible();
      await expectNoSeriousViolations(page, "task page");
      await page.locator("#main").getByTestId("task-edit").click();
      await expect(page.locator("#main").getByTestId("task-editor")).toBeVisible();
    },
  },
  {
    name: "a new task linked to a use case, and the import of tasks with its preview",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      await enableSrs(me, project);
      await enableTasks(me, project);
      await importSample(me, project);
      await open(page, `${tasksPath(project, "board/new")}?link=${encodeURIComponent(USE_CASE.code)}`);
      await expect(page.locator("#main").getByTestId("task-new-form")).toBeVisible();
      await expectNoSeriousViolations(page, "new task");
      await open(page, tasksPath(project, "import"));
      await page.locator("#main").getByTestId("tasks-import-json").fill(SAMPLE_TASKS);
      await page.locator("#main").getByTestId("tasks-import-preview-run").click();
      await expect(page.locator("#main").getByTestId("tasks-import-preview")).toBeVisible();
    },
  },
  {
    name: "Tasks pages on a small screen, read as a reader",
    open: async ({ page, me }) => {
      const project = me.projects[1];
      const owner = newAccount("tasks-owner");
      const hub = new HubAdmin();
      await hub.grant(project, owner.login, "admin", "customer");
      await hub.grant(project, me.login, "reader", "customer");
      await seedTasks(owner, project, me.login);
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, tasksPath(project));
      await expect(page.locator("#main").getByTestId("tasks-read-only")).toBeVisible();
      await expect(page.locator("#main").getByTestId("task-board")).toBeVisible();
      await expectNoSeriousViolations(page, "Tasks board at 375 px");
      await open(page, taskPath(project, "T-1"));
      await expect(page.locator("#main").getByTestId("task-details")).toBeVisible();
      await expectNoSeriousViolations(page, "task page at 375 px");
      await open(page, tasksPath(project, "backlog"));
      await expect(page.locator("#main").getByTestId("tasks-table").first()).toHaveAttribute("data-layout", "list");
    },
  },
  {
    name: "Lean Canvas: the canvases, a canvas with a proposal and a baseline waiting, and the new claim dialog",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      await seedCanvas(me, project);
      await pushConversions(me, project);
      await valueCheck(project);
      await openBaselineByApi(me, project, "CL-2");
      await open(page, canvasesPath(project));
      await expect(page.locator("#main").getByTestId("canvases-table")).toBeVisible();
      await expectNoSeriousViolations(page, "Lean Canvases");
      await open(page, canvasPath(project));
      await expect(page.locator("#main").getByTestId("canvas-proposals")).toBeVisible();
      await expect(page.locator("#main").getByTestId("canvas-baseline-waiting")).toBeVisible();
      await expectNoSeriousViolations(page, "Lean Canvas");
      await page.locator("#main").locator('[data-testid="canvas-cell"][data-cell="problem"]').getByTestId("cell-add").click();
      await expect(page.getByTestId("claim-new-form")).toBeVisible();
    },
  },
  {
    name: "a claim's page with its evidence, history and compared revs, and its editor",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      await seedCanvas(me, project);
      await pushConversions(me, project);
      await valueCheck(project);
      await editByApi(me, project, [{ claim: "CL-1", base_rev: 1, confidence: "high" }], "More shops asked for it");
      await open(page, claimPath(project, "CL-1"));
      await expect(page.locator("#main").getByTestId("evidence-basis")).toBeVisible();
      await page.locator("#main").getByTestId("claim-compare-toggle").click();
      await expect(page.locator("#main").getByTestId("claim-compare-changes")).toBeVisible();
      await expectNoSeriousViolations(page, "claim page");
      await page.locator("#main").getByTestId("claim-edit").click();
      await expect(page.locator("#main").getByTestId("claim-editor")).toBeVisible();
    },
  },
  {
    name: "a baseline's review with an overdue test card, and the Signals page with a key's series",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      const owner = newAccount("canvas-owner");
      await new HubAdmin().grant(project, owner.login, "admin", "customer");
      await seedCanvas(owner, project);
      await pushConversions(owner, project);
      const number = await openBaselineByApi(owner, project, "CL-2");
      await open(page, baselinePath(project, number));
      await expect(page.locator("#main").getByTestId("baseline-decide")).toBeVisible();
      await expectNoSeriousViolations(page, "baseline review");
      await open(page, signalsPath(project, `?key=${METRIC}`));
      await expect(page.locator("#main").getByTestId("series-chart")).toBeVisible();
    },
  },
  {
    name: "Lean Canvas and Signals on a small screen, read as a reader",
    open: async ({ page, me }) => {
      const project = me.projects[1];
      const owner = newAccount("canvas-owner");
      const hub = new HubAdmin();
      await hub.grant(project, owner.login, "admin", "customer");
      await hub.grant(project, me.login, "reader", "customer");
      await seedCanvas(owner, project);
      await pushConversions(owner, project);
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, canvasesPath(project));
      await expect(page.locator("#main").getByTestId("canvases-table")).toHaveAttribute("data-layout", "list");
      await expectNoSeriousViolations(page, "Lean Canvases at 375 px");
      await open(page, canvasPath(project));
      await expect(page.locator("#main").getByTestId("canvas-read-only")).toBeVisible();
      await expectNoSeriousViolations(page, "Lean Canvas at 375 px");
      await open(page, claimPath(project, "CL-1"));
      await expect(page.locator("#main").getByTestId("claim-history")).toBeVisible();
      await expectNoSeriousViolations(page, "claim page at 375 px");
      await open(page, signalsPath(project, `?key=${METRIC}`));
      await expect(page.locator("#main").getByTestId("signal-keys-table")).toHaveAttribute("data-layout", "list");
    },
  },
  {
    name: "plugins on a small screen",
    open: async ({ page, me }) => {
      const project = me.projects[0];
      await new HubAdmin().grant(project, me.login, "admin", "customer");
      const id = await installedSample("a11y-phone");
      await enablePlugin(me, project, id);
      await page.setViewportSize({ width: 375, height: 812 });
      await open(page, `/p/${project}/plugins`);
      await expect(page.locator("#main").getByTestId("project-plugins-table")).toHaveAttribute("data-layout", "list");
      await expectNoSeriousViolations(page, "project plugins at 375 px");
      await open(page, pluginPagePath(project, id));
      await expect(page.frameLocator('[data-testid="plugin-frame"]').locator("#runs")).toHaveText(/^allowed/);
    },
  },
  {
    name: "invite page as the login it is for, then accepted with its Get started list",
    open: async ({ page, me, hidden }) => {
      const created = await new HubAdmin().invite({
        kind: "join",
        project: hidden,
        role: "writer",
        max_level: "internal",
        for_login: me.login,
        days: 7,
      });
      await page.goto(`/invite/${created.token}`);
      await expect(page.getByTestId("invite-summary")).toHaveAttribute("data-state", "for-viewer");
      await expectNoSeriousViolations(page, "invite page for its login");
      await page.getByTestId("invite-accept").click();
      await expect(page.getByTestId("invite-get-started")).toBeVisible();
    },
  },
];

/**
 * The admin area, opened by the stack's hub admin; each entry waits for its content (step 25). `slow` says why a check
 * gets three times the timeout: two pages run axe twice over a list that grows with every member the suite made before
 * them, which on a loaded machine takes longer than 30 s.
 */
const ADMIN_PAGES: { name: string; open: (page: Page) => Promise<void>; slow?: string }[] = [
  {
    name: "org chart as a hub admin, with Unplaced and the add-seat form with errors",
    slow: "two axe passes over /org, whose Unplaced lists every member the suite made so far",
    open: async (page) => {
      test.setTimeout(60_000); // two axe passes, which went past 30 s on a loaded machine
      const slug = await unitByApi("a11y-org");
      await page.goto("/org");
      await expect(page.locator("#main").getByTestId("org-unplaced")).toBeVisible();
      await expectNoSeriousViolations(page, "org chart as a hub admin");
      await page.locator("#main").locator(`[data-testid="org-unit"][data-slug="${slug}"] > div`).first().getByTestId("org-unit-menu").click();
      await page.getByTestId("org-action-add-seat").click();
      const dialog = page.getByTestId("org-seat-dialog");
      await dialog.getByTestId("org-seat-dialog-save").click(); // shows the field errors
      await expect(dialog.getByTestId("org-seat-login")).toHaveAttribute("aria-invalid", "true");
    },
  },
  {
    name: "admin diagnostics",
    open: async (page) => {
      await page.goto("/admin/diagnostics");
      await expect(page.locator("#main").getByTestId("diagnostics-table")).toBeVisible();
    },
  },
  {
    name: "admin overview and diagnostics on a small screen",
    open: async (page) => {
      await page.setViewportSize({ width: 375, height: 812 });
      await page.goto("/admin");
      await expect(page.locator("#main").getByTestId("admin-overview")).toBeVisible();
      await expectNoSeriousViolations(page, "overview at 375 px");
      await page.goto("/admin/diagnostics");
      await expect(page.locator("#main").getByTestId("diagnostics-table")).toHaveAttribute("data-layout", "list");
    },
  },
  {
    name: "admin members",
    open: async (page) => {
      await page.goto("/admin/members");
      await expect(page.getByTestId("members-table")).toBeVisible();
    },
  },
  {
    name: "admin grant dialog, form and confirmation",
    slow: "two axe passes over /admin/members, which lists every member the suite made so far",
    open: async (page) => {
      await page.goto("/admin/members");
      await page.getByTestId("grant-open").click();
      const dialog = page.getByTestId("grant-dialog");
      await dialog.getByTestId("grant-continue").click(); // shows the field errors
      await expect(dialog.getByTestId("grant-login")).toHaveAttribute("aria-invalid", "true");
      await expectNoSeriousViolations(page, "grant form with errors");
      await dialog.getByTestId("grant-login").fill("e2e-someone");
      await dialog.getByTestId("grant-project").selectOption({ index: 1 });
      await dialog.getByTestId("grant-continue").click();
      await expect(page.getByTestId("grant-summary")).toBeVisible();
    },
  },
  {
    name: "admin member page",
    open: async (page) => {
      await page.goto(`/admin/members/${ADMIN_ACCOUNT.login}`);
      await expect(page.getByTestId("member-tokens")).toBeVisible();
      await expect(page.getByTestId("member-activity")).toBeVisible();
    },
  },
  {
    name: "admin tokens with the revoke confirmation open",
    open: async (page) => {
      await page.goto("/admin/tokens");
      await expect(page.getByTestId("tokens-table")).toBeVisible();
      await expectNoSeriousViolations(page, "tokens");
      await page.getByTestId("tokens-table").locator('[data-testid^="revoke-token-"]').first().click();
      await expect(page.getByTestId("revoke-token-dialog")).toBeVisible();
    },
  },
  {
    name: "admin audit, filtered and on its second page",
    open: async (page) => {
      const busy = newAccount("busy");
      for (let i = 0; i < 26; i += 1) await machineToken(busy); // more rows than one page holds
      await page.goto(`/admin/audit?actor=${busy.login}&limit=25`);
      await expect(page.getByTestId("audit-table")).toBeVisible();
      await expectNoSeriousViolations(page, "audit");
      await page.getByTestId("pager-next").click();
      await expect(page.getByTestId("pager-page")).toHaveText(/\b2$/); // "Page 2" in English, "Trang 2" in Vietnamese
    },
  },
  {
    name: "admin audit with no matching row",
    open: async (page) => {
      await page.goto(`/admin/audit?actor=${uniqueName("nobody")}`);
      await expect(page.getByTestId("state-empty")).toBeVisible();
    },
  },
  {
    name: "admin members and audit on a small screen",
    open: async (page) => {
      // Scoped to <main>: a viewport change while a page streams in can leave the server's copy hidden outside it.
      await page.setViewportSize({ width: 375, height: 812 });
      await page.goto("/admin/audit");
      await expect(page.locator("#main").getByTestId("audit-table")).toBeVisible();
      await expectNoSeriousViolations(page, "audit at 375 px");
      await page.goto("/admin/members");
      await expect(page.locator("#main").getByTestId("members-table")).toBeVisible();
    },
  },
  {
    // The branding as a hub with nothing set has it: the spec that sets one (branding.spec.ts) runs first and clears it.
    name: "admin branding with its colours, their contrast, the images and the history",
    open: async (page) => {
      await page.goto("/admin/branding");
      const form = page.locator("#main").getByTestId("branding-form");
      await expect(form.getByTestId("checks-dark")).toBeVisible();
      await form.getByTestId("branding-name").fill("Acme Agents");
      await expect(form.getByTestId("branding-discard")).toBeVisible();
      await expect(page.locator("#main").getByTestId("branding-images")).toBeVisible();
      await expect(page.locator("#main").getByTestId("branding-history")).toBeVisible();
    },
  },
  {
    name: "admin features of the hub and of a project, a choice changed and not saved",
    open: async (page) => {
      const project = uniqueName("features");
      await new HubAdmin().registerProject(project);
      await page.goto("/admin/features");
      const main = page.locator("#main");
      await expect(main.getByTestId("features-form-hub")).toBeVisible();
      await main.getByTestId("features-project-select").selectOption(project);
      await main.getByTestId(`feature-${project}-kg-off`).click();
      await expect(main.getByTestId(`feature-${project}-kg`)).toHaveAttribute("data-choice", "off");
      await expect(main.getByTestId(`features-history-${project}`)).toBeVisible();
    },
  },
  {
    name: "admin plugins with the store and the publishers the hub trusts",
    open: async (page) => {
      await installedSample("a11y-store");
      await page.goto("/admin/plugins");
      await expect(page.locator("#main").getByTestId("plugins-table")).toBeVisible();
      await expect(page.locator("#main").getByTestId("plugin-publishers")).toContainText("acme");
    },
  },
  {
    name: "admin plugin page with its versions, an upgrade waiting and the hub's config, then the upgrade dialog",
    open: async (page) => {
      const id = uniqueName("a11y-detail");
      await publishSample(id, "1.0.0");
      await installPlugin(id);
      await publishSample(id, "1.1.0");
      await page.goto(`/admin/plugins/${id}`);
      await expect(page.locator("#main").getByTestId("plugin-versions-table").locator("tbody tr")).toHaveCount(2);
      await expect(page.locator("#main").getByTestId("instance-config-form")).toBeVisible();
      await expectNoSeriousViolations(page, "admin plugin page");
      await page.locator("#main").getByTestId("plugin-upgrade-open").click();
      await expect(page.getByTestId("plugin-upgrade-dialog")).toBeVisible();
    },
  },
  {
    name: "admin invite links with a link of each kind, the new-link form, then the link made",
    open: async (page) => {
      const hub = new HubAdmin();
      const project = uniqueName("a11y-invites");
      await hub.registerProject(project);
      await hub.invite({ kind: "join", project, role: "reader", max_level: "public", for_login: newAccount("a11y-invitee").login, days: 7 });
      await hub.invite({ kind: "register", days: 1 });
      await page.goto("/admin/invites");
      await expect(page.locator("#main").getByTestId("invites-table")).toBeVisible();
      await expectNoSeriousViolations(page, "admin invite links");
      await page.getByTestId("invite-open").click();
      const dialog = page.getByTestId("invite-dialog");
      await dialog.getByTestId("invite-kind-join").click();
      await expect(dialog.getByTestId("invite-project")).toBeVisible();
      await expectNoSeriousViolations(page, "new invite link form");
      await dialog.getByTestId("invite-kind-register").click();
      await dialog.getByTestId("invite-create").click();
      await expect(dialog.getByTestId("invite-created")).toBeVisible();
    },
  },
];

for (const scheme of ["light", "dark"] as const) {
  test.describe(`${scheme} theme`, () => {
    test.use({ colorScheme: scheme, reducedMotion: "reduce" });

    test.describe("signed out", () => {
      test.use({ storageState: { cookies: [], origins: [] } });

      test(`sign-in page has no serious axe violation (${scheme}) @deployed`, async ({ page }) => {
        await page.goto("/login");
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await expect(page.locator("html")).toHaveClass(new RegExp(scheme));
        await expectNoSeriousViolations(page, `/login (${scheme})`);
      });

      for (const path of ["/", "/about"]) {
        test(`product page at ${path} has no serious axe violation (${scheme})`, async ({ page }) => {
          await page.goto(path);
          await expect(page.getByTestId("product-page")).toBeVisible();
          await expect(page.locator("html")).toHaveClass(new RegExp(scheme));
          await expectNoSeriousViolations(page, `product page at ${path} (${scheme})`);
        });
      }

      test(`invite page of a visitor has no serious axe violation (${scheme})`, async ({ page, admin }) => {
        test.skip(isDeployed, "makes an invite link on the local stack");
        const project = uniqueName("a11y-invite");
        await admin.registerProject(project);
        const created = await admin.invite({ kind: "join", project, role: "writer", max_level: "internal", days: 7 });
        await page.goto(`/invite/${created.token}`);
        await expect(page.getByTestId("invite-sign-in")).toBeVisible();
        await expect(page.locator("html")).toHaveClass(new RegExp(scheme));
        await expectNoSeriousViolations(page, `invite page signed out (${scheme})`);
      });
    });

    for (const entry of PAGES) {
      const tag = entry.deployed ? " @deployed" : "";
      test(`${entry.name} has no serious axe violation (${scheme})${tag}`, async ({ page, member, admin }) => {
        test.skip(isDeployed && !entry.deployed, "seeds data through the local stack");
        let me: Member = { login: "", id: 0, projects: [] };
        let hidden = "";
        if (!isDeployed) {
          me = await member([
            { role: "writer", maxLevel: "customer" },
            { role: "reader", maxLevel: "public" },
          ]);
          hidden = uniqueName("hidden");
          await admin.registerProject(hidden);
        }
        await entry.open({ page, me, hidden });
        await expect(page.locator("html")).toHaveClass(new RegExp(scheme));
        await expectNoSeriousViolations(page, `${entry.name} (${scheme})`);
      });
    }

    test(`plans without a grant (403) have no serious axe violation (${scheme})`, async ({ page, admin, signInAs }) => {
      test.skip(isDeployed, "signs in as the stack's hub admin");
      const project = uniqueName("plans");
      const writer = newAccount("writer");
      await admin.registerProject(project);
      await admin.grant(project, writer.login, "writer", "internal");
      await seedPlans(writer, project);
      await signInAs(ADMIN_ACCOUNT);
      await open(page, `/p/${project}/plans/${ACTIVE_PLAN}`);
      await expect(page.getByTestId("state-forbidden")).toBeVisible();
      await expectNoSeriousViolations(page, `plans without a grant (${scheme})`);
    });

    test(`hub admin page has no serious axe violation (${scheme})`, async ({ page, signInAs }) => {
      test.skip(isDeployed, "signs in as the stack's hub admin");
      await signInAs(ADMIN_ACCOUNT);
      await page.goto("/admin");
      await expect(page.locator("#main").getByTestId("admin-overview")).toBeVisible();
      await expectNoSeriousViolations(page, `/admin as hub admin (${scheme})`);
    });

    test(`home of a member without a grant has no serious axe violation (${scheme})`, async ({ page, signInAs }) => {
      test.skip(isDeployed, "signs in a new member through the local stack");
      await signInAs(newAccount("a11y-nogrant"));
      await expect(page.locator("#main").getByTestId("state-empty")).toBeVisible();
      await expectNoSeriousViolations(page, `home without a grant (${scheme})`);
    });

    for (const entry of ADMIN_PAGES) {
      test(`${entry.name} has no serious axe violation (${scheme})`, async ({ page, signInAs }) => {
        test.skip(isDeployed, "signs in as the stack's hub admin");
        test.slow(entry.slow !== undefined, entry.slow);
        await signInAs(ADMIN_ACCOUNT);
        await entry.open(page);
        await expect(page.locator("html")).toHaveClass(new RegExp(scheme));
        await expectNoSeriousViolations(page, `${entry.name} (${scheme})`);
      });
    }
  });
}
