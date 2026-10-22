import path from "node:path";

import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { claimRun, dispatch, liveWorker, runPath, seedRunPlan, sendEvents, startRun, type WorkerEvent } from "./support/runs";

/**
 * The subagents and background commands of a running run, over its Trace, as Claude Code reports them through the
 * worker (task_started, task_progress, task_updated and task_notification, logged as `output` events): each subagent
 * with what it does now and the commands it runs, the main agent's background commands, the ones that ended folded
 * under them; a row shows the call that started it in the trace; once nothing runs the panel goes. Axe finds nothing
 * serious in it, light and dark, at 1440 and 375 px. With
 * E2E_SCREENSHOT_DIR set it also writes review screenshots, light and dark, desktop and 375 px.
 */
test.skip(isDeployed, "runs a fake worker against the local stack");

const dir = process.env.E2E_SCREENSHOT_DIR;

const task = (subtype: string, body: Record<string, unknown>): WorkerEvent => ({
  kind: "output",
  body: { raw: { type: "system", subtype, session_id: "3f2a9c1e-0000-4000-8000-000000000001", ...body } },
});

const call = (id: string, title: string, rawInput: Record<string, unknown>): WorkerEvent => ({
  kind: "tool_call",
  body: { toolCallId: id, title, kind: title === "Bash" ? "execute" : "other", status: "pending", rawInput },
});

/** A plan run in small: its step's subagent at work, one of its commands sent to the background, a background command
 * of the main agent, and one that was stopped. */
const EVENTS: WorkerEvent[] = [
  { kind: "agent_message_chunk", body: { content: { type: "text", text: "Handing step 5 to a plan-step subagent." } } },
  call("toolu_agent5", "Agent", { description: "Org-chart step 5", subagent_type: "plan-step", prompt: "plan: org-chart\nstep: 5" }),
  task("task_started", {
    task_id: "a5",
    task_type: "local_agent",
    description: "Org-chart step 5",
    subagent_type: "plan-step",
    tool_use_id: "toolu_agent5",
    is_backgrounded: false,
    spawn_depth: 1,
  }),
  call("toolu_verify", "Bash", { command: "bash /tmp/step5-verify.sh > /tmp/step5-verify.log 2>&1", description: "Run step 5 verify in background", run_in_background: true }),
  task("task_started", {
    task_id: "b5",
    task_type: "local_bash",
    description: "Run step 5 verify in background",
    tool_use_id: "toolu_verify",
    parent_task_id: "a5",
    is_backgrounded: true,
    owned_by_subagent: true,
  }),
  task("task_progress", {
    task_id: "a5",
    description: "Running Wait for verify run",
    subagent_type: "plan-step",
    last_tool_name: "Bash",
    tool_use_id: "toolu_agent5",
    usage: { tool_uses: 31, total_tokens: 105976, duration_ms: 4_723_613 },
  }),
  call("toolu_record", "Bash", { command: "evo-agents worker step 4 done", description: "Record step 4 done", run_in_background: true }),
  task("task_started", { task_id: "m4", task_type: "local_bash", description: "Record step 4 done", tool_use_id: "toolu_record", is_backgrounded: true }),
  call("toolu_watch", "Bash", { command: "tail -f /tmp/hub.log", description: "Watch the hub log", run_in_background: true }),
  task("task_started", { task_id: "w1", task_type: "local_bash", description: "Watch the hub log", tool_use_id: "toolu_watch", is_backgrounded: true }),
  task("task_updated", { task_id: "w1", patch: { status: "killed", end_time: 1_792_654_989_150 } }),
  task("task_notification", { task_id: "w1", status: "stopped", summary: "Watch the hub log", tool_use_id: "toolu_watch" }),
];

async function runningRun(me: Member) {
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, uniqueName("laptop"));
  const [run] = await dispatch(me, project, ["2"], { runtime: "claude-code" });
  expect((await claimRun(live))?.id).toBe(run.id);
  await startRun(live, run.id);
  return { project, live, run };
}

function panelOf(page: Page) {
  return page.locator("#main").getByTestId("run-tasks");
}

test("the subagents and background commands running show over the trace, lead to their calls, and go once they end", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await runningRun(me);
  await sendEvents(live, run.id, EVENTS);

  await open(page, runPath(project, run.id));
  const panel = panelOf(page);
  await expect(panel).toBeVisible();
  await expect(panel.getByTestId("run-tasks-toggle")).toHaveAttribute("aria-expanded", "true");
  await expect(panel.getByTestId("run-tasks-counts")).toHaveText("1 sub-agent, 2 commands");

  // The subagent: its name and type, what it does now and its figures, its background command under it.
  const agent = panel.locator('[data-testid="run-task"][data-task-id="a5"]');
  await expect(agent.getByTestId("run-task-name").first()).toHaveText("Org-chart step 5");
  await expect(agent).toContainText("plan-step");
  await expect(agent.getByTestId("run-task-activity")).toHaveText("Bash: Wait for verify run");
  await expect(agent).toContainText("31 tool calls, 106K tokens");
  const child = agent.getByTestId("run-task-children").locator('[data-testid="run-task"][data-task-id="b5"]');
  await expect(child).toContainText("Run step 5 verify in background");
  await expect(child).toContainText("in the background");
  // The main agent's background command on its own row; the stopped one folded under "1 finished".
  await expect(panel.locator('[data-testid="run-task"][data-task-id="m4"]')).toContainText("Record step 4 done");
  await expect(panel.getByTestId("run-task-finished")).toHaveCount(0);
  await panel.getByTestId("run-tasks-finished-toggle").click();
  await expect(panel.getByTestId("run-task-finished")).toHaveAttribute("data-status", "stopped");
  await expect(panel.getByTestId("run-task-finished")).toContainText("Watch the hub log");

  if (dir) {
    for (const scheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      await page.screenshot({ path: path.join(dir, `run-tasks-${scheme}.png`) });
    }
    await page.setViewportSize({ width: 375, height: 812 });
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto(`${runPath(project, run.id)}?view=trace`);
    await expect(panelOf(page)).toBeVisible();
    await page.screenshot({ path: path.join(dir, "run-tasks-375.png"), fullPage: false });
    await page.setViewportSize({ width: 1440, height: 900 });
    await open(page, runPath(project, run.id));
  }

  // A row shows the call that started it in the trace, opened and focused.
  await panelOf(page).locator('[data-testid="run-task"][data-task-id="a5"] [data-testid="run-task-show"]').first().click();
  const agentCall = page.locator('#main [data-testid="trace"] [data-tool-call-id="toolu_agent5"]');
  await expect(agentCall).toBeInViewport();
  await expect(agentCall).toHaveAttribute("open", "");

  // Once they end, nothing runs and the panel goes.
  await sendEvents(live, run.id, [
    task("task_notification", { task_id: "b5", status: "completed", summary: "Run step 5 verify in background", tool_use_id: "toolu_verify" }),
    task("task_notification", { task_id: "m4", status: "completed", summary: "Record step 4 done", tool_use_id: "toolu_record" }),
    task("task_updated", { task_id: "a5", patch: { status: "completed", end_time: 1_792_662_854_935 } }),
    task("task_notification", { task_id: "a5", status: "completed", summary: "step: 5\nresult: pass", tool_use_id: "toolu_agent5" }),
  ]);
  await expect(panelOf(page)).toHaveCount(0, { timeout: 5_000 });
});

for (const scheme of ["light", "dark"] as const) {
  test(`the running subagents and commands pass axe at 1440 and 375 px in ${scheme}`, async ({ page, member }) => {
    await page.emulateMedia({ colorScheme: scheme });
    const me = await member([{ role: "writer", maxLevel: "internal" }]);
    const { project, live, run } = await runningRun(me);
    await sendEvents(live, run.id, EVENTS);
    for (const [width, height] of [
      [1440, 900],
      [375, 812],
    ] as const) {
      await page.setViewportSize({ width, height });
      await open(page, runPath(project, run.id));
      await expect(panelOf(page)).toBeVisible();
      await panelOf(page).getByTestId("run-tasks-finished-toggle").click();
      await expect(panelOf(page).getByTestId("run-task-finished")).toBeVisible();
      await expectNoSeriousViolations(page, `the running tasks at ${width} px in ${scheme}`);
    }
  });
}
