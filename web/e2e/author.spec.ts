import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import {
  agentAsks,
  agentPuts,
  agentResumes,
  authorWorker,
  chatOf,
  dispatchAuthor,
  ensureAuthorSkill,
  HARNESS_CHECKOUT,
  startAuthor,
} from "./support/author";
import { expect, isDeployed, test } from "./support/fixtures";
import { newAccount, uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { dispatch, liveWorker, MODELS, RUN_PLAN, runPath, runPlanBody, runsOf, seedRunPlan } from "./support/runs";
import { toast } from "./support/toast";

/**
 * Author runs from the web against the real API (docs/workers.md, Author runs): New plan on a project's Plans page and
 * Revise with agent on a plan's page and in the command palette, for a writer only; the dialog queues one run of kind
 * author on the visitor's worker with the request, model and timeout picked; the run's page opens on its Chat, where
 * the agent's question waits for the owner's reply, the reply goes to the agent, the plan it put is linked, and the
 * owner ends the chat; Home lists the run while it waits. axe looks at each in light and dark, the pages never scroll
 * sideways from 375 to 1440 px, and a run of another kind keeps its page. The specs play the worker. Pages render in
 * English.
 */
test.skip(isDeployed, "dispatches author runs through the local stack");

const QUESTION = "Should **New plan** sit in the head of the Plans page, or in its empty state only?";
const REPLY = "In the head, and in the empty state too.";
const REQUEST = "Let members write plans from the web with an agent.\nKeep the CLI as it is.";
const WRITTEN = "web-authoring";

/** A new plan as an author run writes it: the runs plan without any progress, which only plan and step runs write. */
function writtenPlan(id: string) {
  const body = runPlanBody(id);
  return {
    ...body,
    repos: body.repos.map(({ status: _status, ...repo }) => repo),
    steps: body.steps.map(({ status: _status, evidence: _evidence, ...step }: Record<string, unknown>) => step),
  };
}

function main(page: Page) {
  return page.locator("#main");
}

/** axe on what the page shows now, in light and in dark (the hub follows the system's scheme until one is picked). */
async function axeBoth(page: Page, label: string) {
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expect(page.locator("html")).toHaveClass(new RegExp(`\\b${scheme}\\b`));
    await expectNoSeriousViolations(page, `${label}, ${scheme}`);
  }
  await page.emulateMedia({ colorScheme: "light" });
}

test("a writer starts New plan, answers the agent in the run's chat, follows the plan it wrote and ends the chat", async ({ page, member }) => {
  test.setTimeout(150_000);
  await ensureAuthorSkill();
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const live = await authorWorker(me, project, uniqueName("desk"));

  // The Plans page of a project without a plan offers New plan in its head and in its empty state.
  await open(page, `/p/${project}/plans`);
  await expect(main(page).getByTestId("new-plan-empty")).toHaveText("New plan");
  await main(page).getByTestId("new-plan").click();
  const dialog = page.getByTestId("author-run-dialog");
  await expect(dialog.getByRole("heading", { name: "New plan" })).toBeVisible();
  await expect(dialog.getByTestId("author-run-runtime")).toContainText("Claude Code");
  await expect(dialog.getByTestId("author-run-worker-select")).toHaveValue(String(live.worker.id));
  await expect(dialog.getByTestId("author-run-worker")).toContainText(HARNESS_CHECKOUT);
  await expect(dialog.getByTestId("dispatch-outlook")).toHaveText(`${live.worker.name} can take it now.`);
  await dialog.getByTestId("author-run-submit").click();
  await expect(dialog.getByTestId("author-run-request-problem")).toHaveText("Say what the plan should achieve first.");
  await axeBoth(page, "the New plan dialog, with a blank request refused");
  await dialog.getByTestId("author-run-request").fill(REQUEST);
  await dialog.getByTestId("plan-run-model-input").fill(MODELS[1]);
  await dialog.getByTestId("author-run-timeout").selectOption("4");
  await dialog.getByTestId("author-run-submit").click();
  await expect(dialog).toBeHidden();

  // One queued run of kind author, pinned to the worker, with the request as typed.
  const [run] = await runsOf(me, project);
  expect(run).toMatchObject({
    kind: "author",
    state: "queued",
    plan_id: "",
    request: REQUEST,
    requested_runtime: "claude-code",
    model: MODELS[1],
    timeout_min: 240,
    pinned_worker_id: live.worker.id,
    dispatched_by: me.login,
  });
  const dispatched = toast(page, `Author run #${run.id} dispatched`);
  await expect(dispatched).toBeVisible();
  await dispatched.getByRole("link", { name: "Open chat" }).click();
  await expect(page).toHaveURL(new RegExp(`/p/${project}/runs/${run.id}\\?tab=chat$`));

  // The run's page opens on its Chat: the request first, and the agent not started yet.
  const chat = main(page).getByTestId("run-chat");
  await expect(main(page).getByTestId("run-kind")).toHaveText("Author run");
  await expect(main(page).getByRole("tab", { name: "Chat" })).toHaveAttribute("aria-selected", "true");
  await expect(chat.getByTestId("run-chat-request")).toContainText("Keep the CLI as it is.");
  await expect(chat.getByTestId("run-chat-status")).toHaveText("Agent is working");
  await expect(chat.getByTestId("run-chat-empty")).toContainText("The run waits for its worker.");
  await expect(chat.getByTestId("run-chat-no-plan")).toHaveText("No plan on the hub yet");
  await expect(main(page).getByTestId("run-composer")).toHaveCount(0);

  // The worker takes it and the agent asks: the chat says it waits for the visitor, without a reload.
  await startAuthor(live, run.id);
  await agentAsks(live, run.id, QUESTION);
  await expect(chat.getByTestId("run-chat-status")).toHaveText("Waiting for you");
  const asked = chat.getByTestId("run-chat-agent");
  await expect(asked).toHaveCount(1);
  await expect(asked.locator("strong")).toHaveText("New plan");
  await expect(asked.getByTestId("run-chat-asks")).toHaveText("Waiting for a reply");
  await expect(chat.getByTestId("run-chat-reply")).toContainText("The agent waits for your reply.");
  await axeBoth(page, "the chat waiting for a reply");

  // Home lists the run while it waits for the visitor, linked to its chat.
  await open(page, "/");
  const waiting = main(page).getByRole("region", { name: "Waiting for your reply" });
  const item = waiting.getByTestId("author-waiting-item");
  await expect(item).toHaveAttribute("data-run-id", String(run.id));
  await expect(item.getByTestId("author-waiting-link")).toContainText("Should **New plan** sit");
  await axeBoth(page, "Home with an author run waiting for a reply");
  await item.getByTestId("author-waiting-reply").click();
  await expect(page).toHaveURL(new RegExp(`/p/${project}/runs/${run.id}\\?tab=chat$`));

  // The reply goes to the run, in the chat's order, and the agent works again.
  const box = chat.getByRole("textbox", { name: "Reply to the agent" });
  await box.fill(REPLY);
  await box.press("Control+Enter");
  await expect(chat.getByTestId("run-chat-reply-sent")).toHaveText("Sent.");
  await expect(chat.getByTestId("run-chat-owner")).toContainText(REPLY);
  expect((await chatOf(me, project, run.id)).messages.map((message) => message.author)).toEqual(["agent", "owner"]);
  await agentResumes(live, run.id);
  await expect(chat.getByTestId("run-chat-status")).toHaveText("Agent is working");
  await expect(chat.getByTestId("run-chat-typing")).toBeVisible();

  // The agent puts its plan and says so: the chat links the plan and its revision.
  const written = await agentPuts(live, run.id, writtenPlan(WRITTEN));
  await agentAsks(live, run.id, "The plan is on the hub. Anything to change?");
  const link = chat.getByTestId("run-chat-plan");
  await expect(link).toHaveText(`Plan ${WRITTEN}, revision ${written.revision}`);
  await expect(link).toHaveAttribute("href", `/p/${project}/plans/${WRITTEN}`);
  await expect(chat.getByTestId("run-chat-agent")).toHaveCount(2);

  // A lost connection keeps the chat on screen and says so; it comes back by itself.
  await page.route("**/v1/projects/*/runs/*/chat", (route) => route.abort("internetdisconnected"));
  await expect(chat.getByTestId("run-chat-offline")).toContainText("Can't reach the hub.");
  await expect(chat.getByTestId("run-chat-agent")).toHaveCount(2);
  await axeBoth(page, "the chat while the hub cannot be reached");
  await page.unroute("**/v1/projects/*/runs/*/chat");
  await expect(chat.getByTestId("run-chat-offline")).toHaveCount(0);

  // The owner ends the chat; the worker is told at its next heartbeat and the run is done after the turn.
  await chat.getByTestId("run-chat-end").click();
  const end = page.getByTestId("run-chat-end-dialog");
  await expect(end).toContainText("The agent finishes its turn, then the run ends done.");
  await axeBoth(page, "the End chat dialog");
  await end.getByTestId("run-chat-end-dialog-confirm").click();
  await expect(toast(page, "Chat ended")).toContainText("The run ends done after the agent's turn.");
  await expect(chat.getByTestId("run-chat-status")).toHaveText("Ending after the agent's turn");
  await expect(chat.getByTestId("run-chat-reply")).toHaveCount(0);
  await open(page, `/p/${project}/plans/${WRITTEN}`);
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Worker fleet rollout");
});

test("Revise with agent from a plan's page and from the palette queues an author run of that plan", async ({ page, member }) => {
  test.setTimeout(90_000);
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await authorWorker(me, project, uniqueName("desk"));

  await open(page, `/p/${project}/plans/${RUN_PLAN}`);
  const actions = main(page).getByTestId("plan-actions");
  await expect(actions.getByTestId("run-plan")).toBeVisible();
  await actions.getByTestId("revise-with-agent").click();
  const dialog = page.getByTestId("author-run-dialog");
  await expect(dialog).toHaveAttribute("data-mode", "revise");
  await expect(dialog.getByRole("heading", { name: "Revise with agent" })).toBeVisible();
  await expect(dialog).toContainText("Worker fleet rollout");
  await axeBoth(page, "the Revise with agent dialog");
  await dialog.getByTestId("author-run-request").fill("Split step 4 into the list and the run page.");
  await dialog.getByTestId("author-run-submit").click();
  await expect(dialog).toBeHidden();
  const [run] = await runsOf(me, project);
  expect(run).toMatchObject({ kind: "author", plan_id: RUN_PLAN, state: "queued", pinned_worker_id: live.worker.id, timeout_min: 120 });
  await expect(toast(page, `Author run #${run.id} dispatched`)).toContainText(RUN_PLAN);

  // The palette offers both: New plan at once, Revise with agent once the plan is typed.
  await page.getByTestId("palette-trigger").click();
  const palette = page.getByTestId("command-palette");
  await expect(palette.locator(`[data-item="action:new-plan:${project}"]`)).toBeVisible();
  await palette.getByRole("combobox").fill("revise rollout");
  const revise = palette.locator(`[data-item="action:revise:${project}:${RUN_PLAN}"]`);
  await expect(revise).toHaveText(/Revise Worker fleet rollout with agent/);
  await axeBoth(page, "the palette with Revise with agent");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("author-run-dialog")).toHaveAttribute("data-mode", "revise");
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("author-run-dialog")).toBeHidden();

  // The run's page names the plan it revises; the runs list tags the run.
  await open(page, runPath(project, run.id));
  await expect(main(page).getByTestId("run-plan-link")).toHaveAttribute("href", `/p/${project}/plans/${RUN_PLAN}`);
  await open(page, `/p/${project}/runs`);
  await expect(main(page).locator(`[data-run-id="${run.id}"]`).first()).toBeVisible();
  await expect(main(page).getByTestId("run-kind").filter({ hasText: "Author run" })).toHaveCount(1);
});

test("a reader is offered neither New plan nor Revise with agent, and reads a writer's chat without a reply box", async ({ page, member, admin }) => {
  test.setTimeout(90_000);
  await ensureAuthorSkill();
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  const project = me.projects[0];
  const writer = newAccount("writer");
  await admin.grant(project, writer.login, "writer", "internal");
  await seedRunPlan(writer, project);
  const live = await authorWorker(writer, project, uniqueName("their-desk"));
  const run = await dispatchAuthor(writer, project, live.worker.id, "Write a plan for the docs.");
  await startAuthor(live, run.id);
  await agentAsks(live, run.id, "Which docs come first?");

  await open(page, `/p/${project}/plans`);
  await expect(main(page).getByTestId("plans-table-active")).toContainText(RUN_PLAN);
  await expect(main(page).getByTestId("new-plan")).toHaveCount(0);
  await open(page, `/p/${project}/plans/${RUN_PLAN}`);
  await expect(main(page).getByRole("heading", { level: 1 })).toBeVisible();
  await expect(main(page).getByTestId("revise-with-agent")).toHaveCount(0);
  await open(page, runPath(project, run.id));
  const chat = main(page).getByTestId("run-chat");
  await expect(chat.getByTestId("run-chat-status")).toHaveText(`Waiting for ${writer.login}`);
  await expect(chat.getByTestId("run-chat-agent")).toContainText("Which docs come first?");
  await expect(chat.getByTestId("run-chat-reply")).toHaveCount(0);
  await expect(chat.getByTestId("run-chat-end")).toHaveCount(0);
  await expect(chat.getByTestId("run-chat-readonly")).toHaveText(`Only ${writer.login} replies in this chat: they dispatched the run.`);
  await axeBoth(page, "a writer's chat as a reader reads it");
});

test("the Chat is a tab of the session region beside the side column from 1280 px, and above it below", async ({ page, member }) => {
  test.setTimeout(120_000);
  await ensureAuthorSkill();
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const live = await authorWorker(me, project, uniqueName("split"));
  const run = await dispatchAuthor(me, project, live.worker.id, REQUEST);
  await startAuthor(live, run.id);
  await agentAsks(live, run.id, QUESTION);

  const split = main(page).getByTestId("run-split");
  const session = split.getByTestId("run-log");
  const aside = split.getByRole("region", { name: `About run #${run.id}` });
  const chat = session.getByTestId("run-chat");
  for (const size of [
    { width: 375, height: 812 },
    { width: 1024, height: 900 },
    { width: 1280, height: 900 },
    { width: 1440, height: 900 },
  ]) {
    await page.setViewportSize(size);
    // The notice's link: ?tab=chat opens the Chat, the first tab of an author run, among the session's tabs.
    await open(page, `${runPath(project, run.id)}?tab=chat`);
    await expect(chat.getByTestId("run-chat-agent")).toBeVisible();
    await expect(session.getByRole("tab", { name: "Chat" })).toHaveAttribute("aria-selected", "true");
    await expect(session.getByRole("tab").first()).toHaveAttribute("data-testid", "run-tab-chat");
    await expect(session.getByTestId("run-tab-trace")).toBeVisible();
    await expect(session.getByTestId("run-tab-log")).toBeVisible();
    await expect(aside).toBeVisible();
    await expect(aside.getByTestId("run-side")).toBeVisible();
    await expect(aside.getByTestId("run-chat")).toHaveCount(0);

    const sessionBox = (await session.boundingBox())!;
    const asideBox = (await aside.boundingBox())!;
    if (size.width >= 1280) {
      // Two regions side by side, level at the top; the session fills the window down to its 24 px gutter, and the
      // Chat fills the session card, as the Trace does: no space under it.
      expect(asideBox.x, `at ${size.width} px the side column is right of the session`).toBeGreaterThanOrEqual(sessionBox.x + sessionBox.width);
      // The side column's 4 px of padding keep its cards' focus outlines inside it: its first card starts level.
      const sideTop = (await aside.getByTestId("run-side").boundingBox())!.y;
      expect(Math.abs(sideTop - sessionBox.y), `at ${size.width} px the two regions start level`).toBeLessThanOrEqual(1);
      await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
      const sessionBottom = (await session.boundingBox())!;
      expect(Math.abs(size.height - (sessionBottom.y + sessionBottom.height) - 24), `at ${size.width} px the session ends on the gutter`).toBeLessThanOrEqual(1);
      const chatBox = (await chat.boundingBox())!;
      expect(
        Math.abs(sessionBottom.y + sessionBottom.height - (chatBox.y + chatBox.height)),
        `at ${size.width} px the Chat fills the session card`,
      ).toBeLessThanOrEqual(2);
      await expect(aside).toHaveAttribute("tabindex", "0");
    } else {
      // One column: the side column first, the session under it.
      expect(asideBox.y + asideBox.height, `at ${size.width} px the side column is above the session`).toBeLessThanOrEqual(sessionBox.y);
    }
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect.soft(overflow, `the author run's page at ${size.width} px scrolls sideways`).toBeLessThanOrEqual(0);
    if (size.width === 1024 || size.width === 1440) await axeBoth(page, `the Chat in the session region at ${size.width} px`);
  }
});

test("the author pages fit from 375 to 1440 px without scrolling sideways, and a step run's page keeps its tabs", async ({ page, member }) => {
  test.setTimeout(120_000);
  await ensureAuthorSkill();
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const LONG = "a-rather-long-unbroken-name-that-never-wraps";
  const live = await authorWorker(me, project, `${LONG}-${uniqueName("w")}`);
  const run = await dispatchAuthor(me, project, live.worker.id, `${LONG} ${"word ".repeat(80)}`);
  await startAuthor(live, run.id);
  await agentAsks(live, run.id, `A long question: ${"https://example.org/".concat(LONG.repeat(4))} and a table\n\n| a | b |\n| - | - |\n| 1 | 2 |`);

  for (const width of [375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    for (const [path, ready] of [
      [runPath(project, run.id), "run-chat-agent"],
      ["/", "author-waiting-item"],
      [`/p/${project}/plans`, "plans-table-active"],
    ] as const) {
      await open(page, path);
      await expect(main(page).getByTestId(ready).first()).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect.soft(overflow, `${path} at ${width} px scrolls sideways`).toBeLessThanOrEqual(0);
    }
    await main(page).getByTestId("new-plan").click();
    const dialog = page.getByTestId("author-run-dialog");
    await expect(dialog.getByTestId("author-run-worker-select")).toBeVisible();
    const box = await dialog.boundingBox();
    expect.soft(box && box.x >= 0 && box.x + box.width <= width, `the dialog fits ${width} px`).toBe(true);
    const inner = await dialog.getByTestId("author-run-body").evaluate((element) => element.scrollWidth - element.clientWidth);
    expect.soft(inner, `the dialog's body scrolls sideways at ${width} px`).toBeLessThanOrEqual(0);
    if (width === 375) await axeBoth(page, "the New plan dialog at 375 px");
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
  }
  await page.setViewportSize({ width: 375, height: 812 });
  await open(page, runPath(project, run.id));
  await expect(main(page).getByTestId("run-chat-agent")).toBeVisible();
  await axeBoth(page, "the chat at 375 px");

  // A step run's page is as it was: no Chat tab, the Trace first, and the composer for its owner.
  await page.setViewportSize({ width: 1280, height: 900 });
  const stepWorker = await liveWorker(me, project, uniqueName("step-box"));
  const [step] = await dispatch(me, project, ["2"], { worker_id: stepWorker.worker.id });
  await open(page, runPath(project, step.id));
  await expect(main(page).getByTestId("run-detail")).toHaveAttribute("data-kind", "step");
  await expect(main(page).getByRole("tab", { name: "Trace" })).toHaveAttribute("aria-selected", "true");
  await expect(main(page).getByTestId("run-tab-chat")).toHaveCount(0);
  await expect(main(page).getByTestId("run-composer")).toBeVisible();
});
