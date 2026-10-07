import type { Page } from "@playwright/test";

import { signIn } from "./support/auth";
import { BASE_URL } from "./support/env";
import { expect, isDeployed, test } from "./support/fixtures";
import { newAccount, uniqueName } from "./support/hub";
import { open } from "./support/plans";
import {
  answerByApi,
  askDecision,
  COMMIT,
  decisionOf,
  HARNESS_REPO,
  notificationCount,
  planRunUnderway,
  runPath,
  seedPlanRunPlan,
  sendNotice,
  workerInbox,
} from "./support/runs";
import { toast } from "./support/toast";

/**
 * The Inbox and the bell against the real API: the bell in the top bar counts the member's unread notifications (read
 * every 10 seconds, its name says the number), the Inbox lists the decisions still open first and the notices after
 * (a push into a default branch with its repo, branch and commits), and filters and marks them read. The run's owner
 * answers a decision in the sheet the Inbox opens over its list (the kit's DecisionCard, the agent's pick chosen) and
 * from the run's page; the answer reaches the run's inbox for the worker, and a decision once answered offers no form
 * and the hub refuses a second answer. Another member reads the decision but cannot answer it. The specs play the
 * worker. Pages render in English.
 */
test.skip(isDeployed, "asks decisions and sends notices through the local stack");

const QUESTION = "Deploy the plan-runs build to staging now?";
const SECOND = "Drop the old staging bucket after the deploy?";
const SHA_2 = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c";

function main(page: Page) {
  return page.locator("#main");
}

test("the bell counts unread notifications, and the Inbox lists open decisions first, then notices", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedPlanRunPlan(me, project);
  const { live, run, decision } = await planRunUnderway(me, project, uniqueName("bell"), { waiting: true });
  await sendNotice(live, run.id, {
    kind: "push_default_branch",
    title: `Pushed 2 commits to ${HARNESS_REPO} main`,
    body: "The catalog of use cases now lists plan runs.",
    repo: HARNESS_REPO,
    branch: "main",
    commits: [COMMIT, SHA_2],
  });
  expect(await notificationCount(me)).toEqual({ unread: 2, open_decisions: 1 });

  await open(page, "/");
  const bell = page.getByTestId("inbox-bell");
  await expect(bell).toHaveAttribute("data-unread", "2");
  await expect(bell).toHaveAccessibleName("Inbox: 2 unread notifications, 1 decision waits for your answer");
  await expect(page.getByTestId("inbox-bell-count")).toHaveText("2");

  // A new decision reaches the bell within one read, without a reload, and is announced.
  const second = await askDecision(live, run.id, SECOND, "4");
  await expect(bell).toHaveAttribute("data-unread", "3", { timeout: 15_000 });
  await expect(bell).toHaveAccessibleName("Inbox: 3 unread notifications, 2 decisions wait for your answer");
  await expect(page.getByTestId("inbox-bell-live")).toHaveText("1 new notification in your inbox.");

  await bell.click();
  await expect(page).toHaveURL(/\/inbox$/);
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Inbox");
  await expect(bell).toHaveAttribute("aria-current", "page");
  await expect(main(page).getByTestId("inbox-unread-count")).toHaveText("3 unread");
  await expect(main(page).getByTestId("inbox-open-count")).toHaveText("2 decisions wait for you");
  const waiting = main(page).getByTestId("inbox-waiting").getByTestId("notification");
  await expect(waiting).toHaveCount(2);
  await expect(waiting.nth(0)).toHaveAttribute("data-decision-id", String(second));
  await expect(waiting.nth(1)).toHaveAttribute("data-decision-id", String(decision));
  await expect(waiting.nth(1).getByTestId("notification-open")).toHaveText(`Run #${run.id} asks: ${QUESTION}`);
  await expect(waiting.nth(1).getByTestId("notification-open")).toHaveAttribute("href", `/inbox?decision=${decision}`);

  // The notice of the push names its repo, branch and commits.
  const notice = main(page).getByTestId("inbox-rest").getByTestId("notification");
  await expect(notice).toHaveCount(1);
  await expect(notice.getByTestId("notice-kind")).toHaveText("Pushed to a default branch");
  await expect(notice.getByTestId("notice-repo")).toHaveText(HARNESS_REPO);
  await expect(notice.getByTestId("notice-branch")).toHaveText("main");
  await expect(notice.getByTestId("notice-commits").locator("[data-sha]")).toHaveText([
    `${COMMIT.slice(0, 7)}commit ${COMMIT}`,
    `${SHA_2.slice(0, 7)}commit ${SHA_2}`,
  ]);
  await expect(notice.getByTestId("notification-run-link")).toHaveAttribute("href", runPath(project, run.id));

  // Filters live in the URL.
  await main(page).getByTestId("inbox-facet-kind").getByRole("button", { name: "Notices" }).click();
  await expect(page).toHaveURL(/\?kind=notice$/);
  await expect(main(page).getByTestId("notification")).toHaveCount(1);
  await expect(main(page).getByTestId("inbox-summary")).toHaveText("1 notification matches.");

  // Marking the notice read lowers the bell at once.
  await notice.getByTestId("notification-mark-read").click();
  await expect(notice).toHaveAttribute("data-unread", "false");
  await expect(bell).toHaveAttribute("data-unread", "2");
  await main(page).getByTestId("inbox-facet-unread").getByRole("button", { name: "Unread" }).click();
  await expect(page).toHaveURL(/\?kind=notice&unread=1$/);
  await expect(main(page).getByTestId("state-empty")).toContainText("No notification matches");
  await main(page).getByTestId("inbox-clear-filters").click();
  await expect(main(page).getByTestId("notification")).toHaveCount(3);

  // Mark all as read leaves the decisions open: they still wait for an answer.
  await main(page).getByTestId("inbox-mark-all").click();
  await expect(toast(page, "2 notifications marked as read")).toBeVisible();
  await expect(bell).toHaveAttribute("data-unread", "0");
  await expect(bell).toHaveAccessibleName("Inbox: no unread notification, 2 decisions wait for your answer");
  await expect(page.getByTestId("inbox-bell-count")).toHaveCount(0);
  await expect(main(page).getByTestId("inbox-waiting").getByTestId("notification")).toHaveCount(2);
  expect(await notificationCount(me)).toEqual({ unread: 0, open_decisions: 2 });
});

test("the owner answers a decision from the Inbox, and an answered decision takes no second answer", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedPlanRunPlan(me, project);
  const { live, run, decision } = await planRunUnderway(me, project, uniqueName("inbox"), { waiting: true });
  if (decision === null) throw new Error("the plan run asked no decision");

  // The notification's link opens the decision in a sheet over the list, and opening it reads the notification.
  await open(page, `/inbox?decision=${decision}`);
  const panel = page.getByTestId("decision-sheet");
  await expect(panel).toHaveAccessibleName(`Decision #${decision}`);
  await expect(main(page).getByTestId("inbox-list")).toBeVisible();
  await expect(panel.getByRole("heading", { level: 2, name: QUESTION })).toBeVisible();
  await expect(panel.getByTestId("decision-state")).toHaveText("Waiting for you");
  await expect(panel.getByTestId("decision-category")).toHaveText("Deploy");
  await expect(panel.getByTestId("decision-context-markdown").locator("strong")).toHaveText("staging");
  await expect(panel.getByTestId("decision-run-link")).toHaveAttribute("href", runPath(project, run.id));
  await expect(panel.getByTestId("decision-step-link")).toHaveText("step 3");
  // The run waits for the answer: the head says when it parks, 24 hours after it started waiting.
  await expect(panel.getByTestId("decision-parks")).toHaveText(/^parks in 23 h \d+ min$/);
  await expect(panel.getByTestId("decision-choice-deploy").getByTestId("decision-recommended")).toHaveText("Agent's pick");
  await expect(panel.getByTestId("decision-choice-wait").getByTestId("decision-recommended")).toHaveCount(0);
  await expect(panel.getByRole("radio", { checked: true })).toHaveAccessibleName("Deploy to staging now Agent's pick");
  await expect(panel.getByTestId("decision-form-hint")).toHaveText("The agent continues as soon as you answer.");
  // The run waits, and its worker keeps the web terminal closed: no Take over.
  await expect(panel.getByTestId("decision-takeover")).toHaveCount(0);
  await expect(page.getByTestId("inbox-bell")).toHaveAttribute("data-unread", "0");
  const item = main(page).locator(`[data-testid="notification"][data-decision-id="${decision}"]`);
  await expect(item).toHaveAttribute("aria-current", "true");

  // With the agent's pick cleared, nothing chosen or written is refused before the hub is asked.
  await panel.getByTestId("decision-clear-option").click();
  await expect(panel.getByRole("radio", { checked: true })).toHaveCount(0);
  await panel.getByTestId("decision-send").click();
  await expect(panel.getByTestId("decision-problem")).toHaveText("Pick an option or write an answer first.");

  // Another option and a note, sent with Cmd or Ctrl and Enter from the note.
  await panel.getByRole("radio", { name: /Wait until tomorrow/ }).check();
  await panel.getByTestId("decision-text").fill("Staging is frozen until the backup finishes.");
  await panel.getByTestId("decision-text").press("ControlOrMeta+Enter");
  await expect(toast(page, `Answer sent to run #${run.id}`)).toContainText("The worker hands it to the agent at its next turn.");
  await expect(panel.getByTestId("decision-state")).toHaveText("Answered");
  await expect(panel.getByTestId("decision-form")).toHaveCount(0);
  await expect(panel.getByTestId("decision-answered-by")).toContainText(`by ${me.login}`);
  await expect(panel.getByTestId("decision-answer-option")).toContainText("Wait until tomorrow");
  await expect(panel.getByTestId("decision-answer-text")).toContainText("Staging is frozen until the backup finishes.");
  await expect(panel.getByTestId("decision-delivery")).toHaveAttribute("data-delivered", "false");
  await expect(item).toHaveAttribute("data-decision-state", "answered", { timeout: 12_000 });

  // The answer waits in the run's inbox for the worker, naming the decision and the option.
  const [message] = await workerInbox(live, run.id);
  expect(message.sent_by).toBe(me.login);
  expect(message.text).toContain(`Answer to decision #${decision} (deploy): ${QUESTION}`);
  expect(message.text).toContain("Chosen option: wait, Wait until tomorrow.");
  expect(message.text).toContain("Staging is frozen until the backup finishes.");
  expect((await decisionOf(me, project, decision)).state).toBe("answered");

  // Once answered, the decision takes no other answer: no form on the page, and the hub says 409.
  expect(await answerByApi(me, project, decision, { option: "deploy" })).toBe(409);
  await open(page, `/inbox?decision=${decision}`);
  await expect(panel.getByTestId("decision-answer")).toBeVisible();
  await expect(panel.getByTestId("decision-form")).toHaveCount(0);
  await expect(panel.getByTestId("decision-send")).toHaveCount(0);
  await expect(panel.getByTestId("decision-answer-option")).toHaveAttribute("data-key", "wait");
  expect((await decisionOf(me, project, decision)).answer_option).toBe("wait");

  // Close goes back to the list, with the decision now among the past ones and focus on its link.
  await panel.getByTestId("decision-close").click();
  await expect(page).toHaveURL(/\/inbox$/);
  await expect(page.getByTestId("decision-sheet")).toHaveCount(0);
  await expect(main(page).locator(`[data-decision-link="${decision}"]`)).toBeFocused();
  await expect(main(page).getByTestId("inbox-rest").locator(`[data-decision-id="${decision}"]`)).toHaveAttribute("data-decision-state", "answered");
});

test("an answer sent while the page was open is refused with the hub's reason", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedPlanRunPlan(me, project);
  const { decision } = await planRunUnderway(me, project, uniqueName("race"), { waiting: true });
  if (decision === null) throw new Error("the plan run asked no decision");
  await open(page, `/inbox?decision=${decision}`);
  const panel = page.getByTestId("decision-sheet");
  await expect(panel.getByRole("radio", { name: /Deploy to staging now/ })).toBeChecked();

  // The owner answers from the command line before sending the form.
  expect(await answerByApi(me, project, decision, { option: "wait" })).toBe(200);
  await panel.getByTestId("decision-send").click();
  const alert = toast(page, "Couldn't send the answer");
  await expect(alert).toHaveAttribute("role", "alert");
  await expect(alert).toContainText("The decision was answered or closed meanwhile, or its run ended.");
  await expect(alert).toContainText(`decision ${decision} is answered, not open`);
  // The card reads the decision again and shows it as the hub holds it.
  await expect(panel.getByTestId("decision-state")).toHaveText("Answered");
  await expect(panel.getByTestId("decision-form")).toHaveCount(0);
  await expect(panel.getByTestId("decision-answer-option")).toContainText("Wait until tomorrow");
  expect((await decisionOf(me, project, decision)).answer_option).toBe("wait");
});

test("the owner answers from the run's page, and another member reads the decision without a form", async ({ page, member, admin, browser }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedPlanRunPlan(me, project);
  const { live, run, decision } = await planRunUnderway(me, project, uniqueName("runpage"), { waiting: true });
  if (decision === null) throw new Error("the plan run asked no decision");

  await open(page, runPath(project, run.id));
  const banner = main(page).getByTestId("run-decisions");
  await expect(banner.getByRole("heading", { level: 2 })).toHaveText("Waiting for your decision");
  await expect(banner.getByTestId("run-decision-note")).toContainText("for your answer to a decision. Time spent waiting does not count");
  await expect(banner.getByTestId("run-decision-link")).toHaveAttribute("href", `/inbox?decision=${decision}`);
  const view = banner.getByTestId("decision");
  await expect(view.getByRole("heading", { level: 3, name: QUESTION })).toBeVisible();
  await view.getByRole("radio", { name: /Deploy to staging now/ }).check();
  await view.getByTestId("decision-send").click();
  await expect(toast(page, `Answer sent to run #${run.id}`)).toContainText("The worker hands it to the agent at its next turn.");
  await expect(view.getByTestId("decision-form")).toHaveCount(0);
  await expect(view.getByTestId("decision-answer-option")).toContainText("Deploy to staging now");
  await expect(banner.getByRole("heading", { level: 2 })).toHaveText("Answer sent", { timeout: 12_000 });
  const [message] = await workerInbox(live, run.id);
  expect(message.text).toContain("Chosen option: deploy, Deploy to staging now.");
  expect(await answerByApi(me, project, decision, { text: "again" })).toBe(409);

  // Another writer of the project reads a decision of the run but cannot answer it; the hub refuses them with 403.
  const colleague = newAccount("colleague");
  await admin.grant(project, colleague.login, "writer", "internal");
  const other = await askDecision(live, run.id, SECOND, "4");
  expect(await answerByApi(colleague, project, other, { option: "deploy" })).toBe(403);
  const context = await browser.newContext({ baseURL: BASE_URL, locale: "vi-VN", timezoneId: "Asia/Ho_Chi_Minh", reducedMotion: "reduce" });
  const readerPage = await context.newPage();
  try {
    await signIn(readerPage, colleague);
    await open(readerPage, `/inbox?decision=${other}`);
    const panel = readerPage.getByTestId("decision-sheet");
    await expect(panel.getByRole("heading", { level: 2, name: SECOND })).toBeVisible();
    await expect(panel.getByTestId("decision-state")).toHaveText(`Waiting for ${me.login}`);
    await expect(panel.getByTestId("decision-locked")).toHaveText(`Only ${me.login}, who dispatched run #${run.id}, can answer this decision.`);
    await expect(panel.getByTestId("decision-form")).toHaveCount(0);
    await expect(panel.getByTestId("decision-option")).toHaveCount(2);
    await expect(readerPage.getByTestId("inbox-bell")).toHaveAttribute("data-unread", "0");
  } finally {
    await context.close();
  }
});

test("the owner takes over from a decision while the agent still works, in the run's Terminal tab", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedPlanRunPlan(me, project);
  // The agent asks and goes on working: the run stays running, on a worker that allows the web terminal.
  const { live, run } = await planRunUnderway(me, project, uniqueName("desk"), { terminal: true });
  const decision = await askDecision(live, run.id, QUESTION);

  await open(page, `/inbox?decision=${decision}`);
  const sheet = page.getByTestId("decision-sheet");
  await expect(sheet.getByTestId("decision-form")).toBeVisible();
  // No park time while the agent works.
  await expect(sheet.getByTestId("decision-parks")).toHaveCount(0);
  const takeOver = sheet.getByTestId("decision-takeover");
  await expect(takeOver).toHaveAttribute("href", `${runPath(project, run.id)}?view=terminal`);
  await expect(takeOver).toHaveAccessibleName(`Take over run #${run.id} in the terminal`);
  await takeOver.click();
  await expect(page).toHaveURL(new RegExp(`${runPath(project, run.id)}\\?view=terminal$`));
  const log = main(page).getByTestId("run-log");
  await expect(log).toHaveAttribute("data-tab", "terminal");

  // On the run's own page the card's Take over shows the Terminal tab without leaving it.
  await open(page, runPath(project, run.id));
  await expect(log).toHaveAttribute("data-tab", "log");
  const card = main(page).getByTestId("run-decisions").getByTestId("decision");
  await card.getByTestId("decision-takeover").click();
  await expect(log).toHaveAttribute("data-tab", "terminal");
  await expect(main(page).getByTestId("run-tab-terminal")).toBeFocused();
  await expect(page).toHaveURL(new RegExp(`${runPath(project, run.id)}$`));

});

test("the Inbox and a decision never scroll sideways at 375, 768 and 1024 px", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const LONG = "a-rather-long-unbroken-name-that-never-wraps-in-an-inbox";
  await seedPlanRunPlan(me, project);
  const { live, run, decision } = await planRunUnderway(me, project, `${LONG}-worker`, { waiting: true });
  await sendNotice(live, run.id, {
    kind: "merge_default_branch",
    title: `Merged into ${HARNESS_REPO} main`,
    body: `${LONG} `.repeat(12),
    repo: HARNESS_REPO,
    branch: "main",
    commits: Array.from({ length: 8 }, (_, index) => `${index}`.repeat(40)),
  });
  for (const width of [375, 768, 1024]) {
    await page.setViewportSize({ width, height: 900 });
    for (const [path, ready] of [
      ["/inbox", "inbox-list"],
      [`/inbox?decision=${decision}`, "decision-form"],
      [runPath(project, run.id), "run-decisions"],
    ] as const) {
      await open(page, path);
      await expect(page.getByTestId(ready)).toBeVisible();
      await expect(page.getByTestId("state-loading")).toHaveCount(0);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect.soft(overflow, `${path} at ${width} px scrolls sideways`).toBeLessThanOrEqual(0);
    }
    // The sheet fits the viewport, and its card does not scroll sideways inside it.
    await open(page, `/inbox?decision=${decision}`);
    const sheet = page.getByTestId("decision-sheet");
    await expect(sheet.getByTestId("decision-form")).toBeVisible();
    const box = await sheet.boundingBox();
    expect.soft(box && box.x >= 0 && box.x + box.width <= width, `the sheet fits ${width} px`).toBe(true);
    const inner = await sheet.getByTestId("decision-panel").evaluate((element) => element.scrollWidth - element.clientWidth);
    expect.soft(inner, `the sheet scrolls sideways at ${width} px`).toBeLessThanOrEqual(0);
  }
});

test.describe("in Vietnamese", () => {
  test.use({ uiLocale: "vi" });

  test("the bell, the Inbox and the answer form speak Vietnamese and keep the English terms", async ({ page, member }) => {
    const me = await member([{ role: "writer", maxLevel: "internal" }]);
    const project = me.projects[0];
    await seedPlanRunPlan(me, project);
    const { run, decision } = await planRunUnderway(me, project, uniqueName("vi"), { waiting: true });
    await open(page, "/inbox");
    await expect(page.getByTestId("inbox-bell")).toHaveAccessibleName("Inbox: 1 thông báo chưa đọc, 1 quyết định chờ bạn trả lời");
    await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Inbox");
    await expect(main(page).getByTestId("inbox-waiting").getByRole("heading", { level: 2 })).toHaveAccessibleName("Đang chờ bạn trả lời (1 thông báo)");
    await main(page).getByTestId("notification-answer").click();
    await expect(page).toHaveURL(new RegExp(`/inbox\\?decision=${decision}$`));
    const panel = page.getByTestId("decision-sheet");
    await expect(panel.getByRole("heading", { level: 2, name: QUESTION })).toBeFocused();
    await expect(panel.getByTestId("decision-state")).toHaveText("Đang chờ bạn");
    await expect(panel.getByTestId("decision-category")).toHaveText("Triển khai");
    await expect(panel.getByTestId("decision-run-link")).toHaveText(`Run #${run.id}`);
    await expect(panel.getByTestId("decision-recommended")).toHaveText("Agent đề xuất");
    await expect(panel.getByTestId("decision-send")).toHaveAccessibleName("Gửi câu trả lời");
    await expect(panel.getByTestId("decision-form-hint")).toHaveText("Agent làm tiếp ngay khi bạn trả lời.");
    // Back closes the decision again, and focus returns to the Answer button that opened it.
    await page.goBack();
    await expect(page).toHaveURL(/\/inbox$/);
    await expect(page.getByTestId("decision-sheet")).toHaveCount(0);
    await expect(main(page).getByTestId("notification-answer")).toBeFocused();
  });
});
