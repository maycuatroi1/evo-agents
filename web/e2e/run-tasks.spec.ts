import path from "node:path";

import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { claimRun, dispatch, liveWorker, runPath, seedRunPlan, sendEvents, startRun, type WorkerEvent } from "./support/runs";

/**
 * The Agents section of a running run, over its Trace: one lane for the main agent, one for each sub-agent and one for
 * the background commands on one time axis, as Claude Code reports them through the worker (task_started,
 * task_progress, task_updated and task_notification, logged as `output` events). The main agent's time waiting on a
 * sub-agent is drawn apart from its work, "This step" and "Whole run" set the range, and a lane opens its agent: a
 * sub-agent's profile (`?agent=`, `&tab=`, with its brief, its own activity and its report, and the composer under it
 * saying a message goes to the main agent), a command's call in the trace. Many finished sub-agents fold. Axe finds
 * nothing serious in it, light and dark, at 1440 and 375 px. With E2E_SCREENSHOT_DIR set it also writes review
 * screenshots, light and dark, desktop and 375 px.
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

/** The minute a run in these tests started, on the clock of the page: the lanes draw from event times. */
const START = Math.floor((Date.now() - 40 * 60_000) / 60_000) * 60_000;
const at = (minute: number) => new Date(START + minute * 60_000).toISOString();
const timed = (minute: number, event: WorkerEvent): WorkerEvent => ({ ...event, at: at(minute) });

/** A plan run in small: step 3 reported done, then step 4's sub-agent at work, one of its commands sent to the
 * background, a background command of the main agent, and one that was stopped. */
const EVENTS: WorkerEvent[] = [
  timed(0, { kind: "agent_message_chunk", body: { content: { type: "text", text: "Starting." } } }),
  timed(1, call("toolu_done3", "Bash", { command: "evo-agents worker step 3 done" })),
  timed(2, { kind: "agent_message_chunk", body: { content: { type: "text", text: "Handing step 5 to a plan-step subagent." } } }),
  timed(3, call("toolu_agent5", "Agent", { description: "Org-chart step 5", subagent_type: "plan-step", prompt: "plan: org-chart\nstep: 5" })),
  timed(
    3,
    task("task_started", {
      task_id: "a5",
      task_type: "local_agent",
      description: "Org-chart step 5",
      subagent_type: "plan-step",
      tool_use_id: "toolu_agent5",
      is_backgrounded: false,
      spawn_depth: 1,
    }),
  ),
  timed(5, call("toolu_verify", "Bash", { command: "bash /tmp/step5-verify.sh > /tmp/step5-verify.log 2>&1", description: "Run step 5 verify in background", run_in_background: true })),
  timed(
    5,
    task("task_started", {
      task_id: "b5",
      task_type: "local_bash",
      description: "Run step 5 verify in background",
      tool_use_id: "toolu_verify",
      parent_task_id: "a5",
      is_backgrounded: true,
      owned_by_subagent: true,
    }),
  ),
  timed(
    7,
    task("task_progress", {
      task_id: "a5",
      description: "Running Wait for verify run",
      subagent_type: "plan-step",
      last_tool_name: "Bash",
      tool_use_id: "toolu_agent5",
      usage: { tool_uses: 31, total_tokens: 105976, duration_ms: 4_723_613 },
    }),
  ),
  timed(9, call("toolu_record", "Bash", { command: "evo-agents hub plan show org-chart", description: "Show the plan", run_in_background: true })),
  timed(9, task("task_started", { task_id: "m4", task_type: "local_bash", description: "Show the plan", tool_use_id: "toolu_record", is_backgrounded: true })),
  timed(10, call("toolu_watch", "Bash", { command: "tail -f /tmp/hub.log", description: "Watch the hub log", run_in_background: true })),
  timed(10, task("task_started", { task_id: "w1", task_type: "local_bash", description: "Watch the hub log", tool_use_id: "toolu_watch", is_backgrounded: true })),
  timed(12, task("task_updated", { task_id: "w1", patch: { status: "killed", end_time: 1_792_654_989_150 } })),
  timed(12, task("task_notification", { task_id: "w1", status: "stopped", summary: "Watch the hub log", tool_use_id: "toolu_watch" })),
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
  return page.locator("#main").getByTestId("agent-lanes");
}

test("agent lanes put the main agent, each sub-agent and the background commands on one timeline, and open the one picked", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await runningRun(me);
  await sendEvents(live, run.id, EVENTS);

  // Tall enough that the trace keeps room under the lanes.
  await page.setViewportSize({ width: 1280, height: 1000 });
  await open(page, runPath(project, run.id));
  const panel = panelOf(page);
  await expect(panel).toBeVisible();
  // One lane for the main agent, one for the sub-agent, one for the background commands.
  const lanes = panel.getByTestId("agent-lane");
  await expect(lanes).toHaveCount(3);
  await expect(lanes.nth(0)).toHaveAttribute("data-kind", "main");
  await expect(lanes.nth(1)).toHaveAttribute("data-kind", "agent");
  await expect(lanes.nth(2)).toHaveAttribute("data-kind", "commands");

  // The main agent: its work and the time it waits on the sub-agent drawn apart, a mark where it handed work over.
  const main = lanes.nth(0);
  await expect(main.getByTestId("lane-status")).toHaveText("Waiting for a sub-agent");
  await expect(main.getByTestId("lane-wait")).toHaveCount(1);
  await expect(main.getByTestId("lane-work")).not.toHaveCount(0);
  await expect(main.getByTestId("lane-handoff")).toHaveAttribute("title", "Handed work to Org-chart step 5");
  // The sub-agent: its name and type, what it does now, its figures.
  const agent = lanes.nth(1);
  await expect(agent.getByTestId("lane-name")).toHaveText("Org-chart step 5");
  await expect(agent).toContainText("plan-step");
  await expect(agent.getByTestId("lane-status")).toContainText("Running");
  await expect(agent.getByTestId("lane-status")).toContainText("31 tool calls, 106K tokens");
  await expect(agent.getByTestId("lane-work-now")).toHaveText("Bash: Wait for verify run");
  // The commands: named by what they run (rule G1), 2 of 3 running.
  const commands = lanes.nth(2);
  await expect(commands.getByTestId("lane-work-now")).toHaveText("evo-agents hub plan show org-chart");
  await expect(commands.getByTestId("lane-status")).toContainText("2 of 3 running");
  // Nothing on the page moves over and over but the status dot.
  await expect(panel.locator(".animate-live-ping")).toHaveCount(0);

  // This step and Whole run change the range: the axis starts where the step began, or where the run did.
  const stepFrom = await panel.getByTestId("agent-lanes-axis").locator("time").first().textContent();
  await expect(panel.getByTestId("agent-lanes-step")).toHaveAttribute("aria-pressed", "true");
  await panel.getByTestId("agent-lanes-run").click();
  await expect(panel.getByTestId("agent-lanes-run")).toHaveAttribute("aria-pressed", "true");
  await expect(panel.getByTestId("agent-lanes-list")).toHaveAttribute("data-scope", "run");
  await expect(panel.getByTestId("agent-lanes-axis").locator("time").first()).not.toHaveText(stepFrom ?? "");
  await panel.getByTestId("agent-lanes-step").click();
  await expect(panel.getByTestId("agent-lanes-axis").locator("time").first()).toHaveText(stepFrom ?? "");

  if (dir) {
    for (const scheme of ["light", "dark"] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      await page.screenshot({ path: path.join(dir, `agent-lanes-${scheme}.png`) });
    }
    await page.setViewportSize({ width: 375, height: 812 });
    await page.emulateMedia({ colorScheme: "light" });
    await page.goto(`${runPath(project, run.id)}?view=trace`);
    await expect(panelOf(page)).toBeVisible();
    await page.screenshot({ path: path.join(dir, "agent-lanes-375.png"), fullPage: false });
    await page.setViewportSize({ width: 1440, height: 900 });
    await open(page, runPath(project, run.id));
  }

  // The commands lane shows the call of its newest command in the trace, opened and focused.
  await panelOf(page).locator('[data-testid="agent-lane"][data-kind="commands"]').click();
  const commandCall = page.locator('#main [data-testid="trace"] [data-tool-call-id="toolu_record"]');
  await expect(commandCall).toBeInViewport();
  await expect(commandCall).toHaveAttribute("open", "");
  // A sub-agent's lane opens its profile in the trace's place; Back returns to the main agent.
  await panelOf(page).locator('[data-testid="agent-lane"][data-lane="a5"]').click();
  await expect(page.getByTestId("agent-profile")).toHaveAttribute("data-task-id", "a5");
  await expect(page).toHaveURL(/[?&]agent=a5/);
  await page.goBack();
  await expect(page.getByTestId("agent-profile")).toHaveCount(0);
  await expect(panelOf(page)).toBeVisible();
  // The main agent's lane opens its own trace.
  await panelOf(page).locator('[data-testid="agent-lane"][data-kind="main"]').click();
  await expect(page.getByTestId("agent-profile")).toHaveCount(0);
  await expect(page).not.toHaveURL(/[?&]agent=/);
  await expect(page.locator('#main [data-testid="trace"]')).toBeVisible();

  // Once they end the section stays, with the sub-agent completed.
  await sendEvents(live, run.id, [
    task("task_notification", { task_id: "b5", status: "completed", summary: "Run step 5 verify in background", tool_use_id: "toolu_verify" }),
    task("task_notification", { task_id: "m4", status: "completed", summary: "Show the plan", tool_use_id: "toolu_record" }),
    task("task_updated", { task_id: "a5", patch: { status: "completed", end_time: 1_792_662_854_935 } }),
    task("task_notification", { task_id: "a5", status: "completed", summary: "step: 5\nresult: pass", tool_use_id: "toolu_agent5" }),
  ]);
  await expect(panelOf(page).locator('[data-lane="a5"] [data-testid="lane-status"]')).toContainText("Completed", { timeout: 5_000 });
  await expect(panelOf(page).locator('[data-kind="main"] [data-testid="lane-status"]')).toHaveText("Working");
  await expect(panelOf(page).getByTestId("lane-return")).toHaveCount(1);
});

test("agent lanes fold the finished sub-agents of a run that has many, and fit a phone in 64 px rows", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, live, run } = await runningRun(me);
  const events: WorkerEvent[] = [timed(0, { kind: "agent_message_chunk", body: { content: { type: "text", text: "Starting." } } })];
  for (let i = 1; i <= 6; i++) {
    events.push(
      timed(i * 2, call(`toolu_s${i}`, "Agent", { description: `Sub-agent ${i}`, subagent_type: "Explore", prompt: `Look at part ${i}` })),
      timed(i * 2, task("task_started", { task_id: `s${i}`, task_type: "local_agent", description: `Sub-agent ${i}`, subagent_type: "Explore", tool_use_id: `toolu_s${i}`, is_backgrounded: false })),
      timed(i * 2 + 1, task("task_notification", { task_id: `s${i}`, status: "completed", summary: `Part ${i} looked at.`, tool_use_id: `toolu_s${i}` })),
    );
  }
  events.push(
    timed(14, call("toolu_s7", "Agent", { description: "Sub-agent 7", subagent_type: "Explore", prompt: "Look at part 7" })),
    timed(14, task("task_started", { task_id: "s7", task_type: "local_agent", description: "Sub-agent 7", subagent_type: "Explore", tool_use_id: "toolu_s7", is_backgrounded: true })),
  );
  await sendEvents(live, run.id, events);

  await open(page, runPath(project, run.id));
  const panel = panelOf(page);
  // The main agent and the one that runs; the six that ended fold under one line.
  await expect(panel.getByTestId("agent-lane")).toHaveCount(2);
  await expect(panel.getByTestId("agent-lanes-finished")).toContainText("6 finished sub-agents");
  await panel.getByTestId("agent-lanes-finished").click();
  await expect(panel.getByTestId("agent-lane")).toHaveCount(8);

  await page.setViewportSize({ width: 375, height: 812 });
  await open(page, `${runPath(project, run.id)}?view=trace`);
  await expect(panelOf(page)).toBeVisible();
  const box = await panelOf(page).getByTestId("agent-lane").first().boundingBox();
  expect(box?.height ?? 0).toBeGreaterThanOrEqual(63);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375);
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
  test(`the agent lanes pass axe at 1440 and 375 px in ${scheme}`, async ({ page, member }) => {
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
      await expectNoSeriousViolations(page, `the agent lanes at ${width} px in ${scheme}`);
    }
  });
}
