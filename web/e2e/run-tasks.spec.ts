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
 * under them, subagents apart from commands; a subagent's row opens its profile, a command's shows the call that
 * started it in the trace; once nothing runs the panel goes. A subagent's profile (`?agent=`, `&tab=`) has its brief,
 * its own activity and its report, and the composer under it says a message goes to the main agent. Axe finds nothing
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

  // Subagents and background commands are listed apart, a command named by what it runs.
  await expect(panel.getByTestId("run-tasks-agents").locator('[data-testid="run-task"]')).toHaveCount(2);
  await expect(panel.getByTestId("run-tasks-commands").locator('[data-testid="run-task"][data-task-id="m4"] [data-testid="run-task-name"]')).toHaveText(
    "evo-agents worker step 4 done",
  );

  // A command's row shows the call that started it in the trace, opened and focused.
  await panelOf(page).locator('[data-testid="run-task"][data-task-id="m4"] [data-testid="run-task-show"]').click();
  const commandCall = page.locator('#main [data-testid="trace"] [data-tool-call-id="toolu_record"]');
  await expect(commandCall).toBeInViewport();
  await expect(commandCall).toHaveAttribute("open", "");
  // A subagent's row opens its profile in the trace's place; Back returns to the main agent.
  await panelOf(page).locator('[data-testid="run-task"][data-task-id="a5"] [data-testid="run-task-open"]').first().click();
  await expect(page.getByTestId("agent-profile")).toHaveAttribute("data-task-id", "a5");
  await expect(page).toHaveURL(/[?&]agent=a5/);
  await page.goBack();
  await expect(page.getByTestId("agent-profile")).toHaveCount(0);
  await expect(panelOf(page)).toBeVisible();

  // Once they end, nothing runs and the panel goes.
  await sendEvents(live, run.id, [
    task("task_notification", { task_id: "b5", status: "completed", summary: "Run step 5 verify in background", tool_use_id: "toolu_verify" }),
    task("task_notification", { task_id: "m4", status: "completed", summary: "Record step 4 done", tool_use_id: "toolu_record" }),
    task("task_updated", { task_id: "a5", patch: { status: "completed", end_time: 1_792_662_854_935 } }),
    task("task_notification", { task_id: "a5", status: "completed", summary: "step: 5\nresult: pass", tool_use_id: "toolu_agent5" }),
  ]);
  await expect(panelOf(page)).toHaveCount(0, { timeout: 5_000 });
});

const BRIEF = "plan: org-chart\nstep: 5\n\nRun step 5 of the plan.\n\n## Report\nstep: <id>\nresult: pass | fail";
const sub = (event: WorkerEvent): WorkerEvent => ({ ...event, body: { ...event.body, _meta: { parentToolCallId: "toolu_agent5" } } });

/** A foreground subagent whose events name it (`_meta.parentToolCallId`), at work on step 5. */
const PROFILE_EVENTS: WorkerEvent[] = [
  { kind: "agent_message_chunk", body: { content: { type: "text", text: "Handing step 5 to a plan-step subagent." } } },
  call("toolu_agent5", "Agent", { description: "Org-chart step 5", subagent_type: "plan-step", prompt: BRIEF }),
  task("task_started", { task_id: "a5", task_type: "local_agent", description: "Org-chart step 5", subagent_type: "plan-step", tool_use_id: "toolu_agent5", is_backgrounded: false, prompt: BRIEF }),
  sub({ kind: "agent_message_chunk", body: { content: { type: "text", text: "Reading the step before I edit anything." } } }),
  sub(call("toolu_sub_read", "Bash", { command: "cd /srv/app && export CI=1 && uv run pytest -q tests/test_org.py" })),
];

test("a sub-agent profile opens from the address and the delegation card, on its brief, activity and result", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await runningRun(me);
  await sendEvents(live, run.id, PROFILE_EVENTS);

  // The main agent's trace: one card for the handing, the subagent's own words left to its profile.
  await open(page, runPath(project, run.id));
  const trace = page.locator('#main [data-testid="trace"]');
  const card = trace.getByTestId("trace-delegation");
  await expect(card.getByTestId("trace-delegation-agent")).toHaveAttribute("data-task-id", "a5");
  await expect(card.getByTestId("trace-delegation-brief")).toContainText("Run step 5 of the plan.");
  await expect(card.getByTestId("trace-delegation-outcome")).toContainText("Working");
  await expect(trace).not.toContainText("Reading the step before I edit anything.");

  // `?agent=a5&tab=brief`: the brief as Markdown, the raw tool call behind a button.
  await open(page, `${runPath(project, run.id)}?agent=a5&tab=brief`);
  const profile = page.getByTestId("agent-profile");
  await expect(profile).toHaveAttribute("data-tab", "brief");
  await expect(profile.getByTestId("agent-profile-name")).toHaveText("Org-chart step 5");
  await expect(profile.getByTestId("agent-profile-brief-text")).toContainText("Run step 5 of the plan.");
  await profile.getByTestId("agent-profile-raw-toggle").click();
  await expect(profile.getByTestId("agent-profile-raw")).toContainText('"subagent_type": "plan-step"');
  // A message while a subagent is shown goes to the main agent.
  await expect(page.getByTestId("run-composer-hint")).toContainText("a message goes to the main agent");

  // Activity: its own events, by the trace's rules; the tab is in the address.
  await profile.getByTestId("agent-profile-tab-activity").click();
  await expect(page).toHaveURL(/[?&]tab=activity/);
  const activity = profile.getByTestId("agent-profile-activity");
  await expect(activity).toContainText("Reading the step before I edit anything.");
  await expect(activity.getByTestId("trace-group")).toBeVisible();

  // Result: none yet, with what the brief asks it to report; then its whole report once it ends.
  await profile.getByTestId("agent-profile-tab-result").click();
  await expect(profile.getByTestId("agent-profile-no-result")).toHaveText("No report yet: the sub-agent is still working.");
  await expect(profile.getByTestId("agent-profile-ask")).toContainText("result: pass | fail");
  await sendEvents(live, run.id, [
    task("task_notification", { task_id: "a5", status: "completed", summary: "Step 5 is done.\n\nAll 12 tests pass on the branch.", tool_use_id: "toolu_agent5" }),
  ]);
  await expect(profile.getByTestId("agent-profile-result-text")).toContainText("All 12 tests pass on the branch.");
  await expect(profile.getByTestId("agent-profile-status")).toHaveText("Completed");

  // Back to the main agent, then the card's Open button.
  await profile.getByTestId("agent-profile-back").click();
  await expect(page.getByTestId("agent-profile")).toHaveCount(0);
  await expect(page).not.toHaveURL(/[?&]agent=/);
  await trace.getByTestId("trace-delegation-open").click();
  await expect(page.getByTestId("agent-profile")).toHaveAttribute("data-tab", "result");
  await expectNoSeriousViolations(page, "a sub-agent profile at 1440 px");
  if (dir) await page.screenshot({ path: path.join(dir, "agent-profile.png") });

  // At 375 px the profile fills the session's screen; its Back steps back to the main agent's trace.
  await page.setViewportSize({ width: 375, height: 812 });
  await open(page, `${runPath(project, run.id)}?agent=a5&tab=activity`);
  await expect(page.getByTestId("run-detail")).toHaveAttribute("data-screen", "session");
  await expect(page.getByTestId("agent-profile")).toBeVisible();
  await expect(page.getByTestId("agent-profile-activity")).toContainText("Reading the step before I edit anything.");
  const box = await page.getByTestId("agent-profile").boundingBox();
  expect(box?.width ?? 0).toBeGreaterThan(330);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375);
  await expectNoSeriousViolations(page, "a sub-agent profile at 375 px");
  if (dir) await page.screenshot({ path: path.join(dir, "agent-profile-375.png") });
  await page.getByTestId("run-screen-back").click();
  await expect(page.getByTestId("agent-profile")).toHaveCount(0);
  await expect(page.locator('#main [data-testid="trace"]')).toBeVisible();
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
