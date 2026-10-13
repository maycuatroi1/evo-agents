import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { agentAsks, authorWorker, chatOf, dispatchAuthor, ensureAuthorSkill, startAuthor } from "./support/author";
import { expect, isDeployed, test } from "./support/fixtures";
import { newAccount, uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { askDecision, runOf, runPath } from "./support/runs";

/**
 * The decisions of an author run against the real API (docs/workers.md, The chat of an author run): asked before the
 * run has a plan, each is the kit's DecisionCard in the run's Chat, after the agent's message that ended the turn which
 * asked it; the owner picks with the mouse (the agent's pick chosen) and sends each card, which folds into its answer,
 * and no answer comes back as a message of the chat; the chat's status counts what still waits; the Trace's "Asked you"
 * opens the card in the Chat; the Inbox names the decision by its author run and links to no plan; another member
 * reads the card without a form. axe looks at each in light and dark, and the Chat never scrolls sideways at 375 px.
 * The specs play the worker. Pages render in English.
 */
test.skip(isDeployed, "dispatches author runs through the local stack");

const REQUEST = "Write a plan for the decisions of author runs.";
const TURN = "Before I write the plan I asked you two things; the cards are below.";
const SCOPE = "Does the first revision cover author runs, or plan runs only?";
const SHAPE = "Where do the answers go: the chat's messages, or the decisions alone?";

function main(page: Page) {
  return page.locator("#main");
}

/** axe on what the page shows now, in light and in dark. */
async function axeBoth(page: Page, label: string) {
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expect(page.locator("html")).toHaveClass(new RegExp(`\\b${scheme}\\b`));
    await expectNoSeriousViolations(page, `${label}, ${scheme}`);
  }
  await page.emulateMedia({ colorScheme: "light" });
}

/** The agent's turn: two decisions without a plan or a step, then the message that ends the turn, and the run waits. */
async function askTwo(live: Parameters<typeof askDecision>[0], runId: number): Promise<[number, number]> {
  const scope = await askDecision(live, runId, SCOPE, undefined, {
    category: "scope",
    step_key: null,
    context: "Plan runs already ask decisions; author runs had **none** until now.",
    options: [
      { key: "both", label: "Author runs as well", description: "One more step for the chat's cards." },
      { key: "plan", label: "Plan runs only" },
    ],
    recommended: "both",
  });
  const shape = await askDecision(live, runId, SHAPE, undefined, {
    category: "architecture",
    step_key: null,
    context: null,
    options: [
      { key: "decisions", label: "The decisions alone" },
      { key: "chat", label: "The chat's messages too" },
    ],
    recommended: "decisions",
  });
  await agentAsks(live, runId, TURN);
  return [scope, shape];
}

test("an author run's decisions are cards in its Chat: answered with the mouse, counted, opened from the Trace and the Inbox", async ({ page, member }) => {
  test.setTimeout(150_000);
  await ensureAuthorSkill();
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const live = await authorWorker(me, project, uniqueName("desk"));
  const run = await dispatchAuthor(me, project, live.worker.id, REQUEST);
  await startAuthor(live, run.id);
  const [scope, shape] = await askTwo(live, run.id);

  // The Chat: the agent's message, then both cards in the order asked, and the status counts them.
  await open(page, runPath(project, run.id));
  const chat = main(page).getByTestId("run-chat");
  await expect(chat.getByTestId("run-chat-status")).toHaveText("2 decisions wait for you");
  const cards = chat.getByTestId("run-chat-decision");
  await expect(cards).toHaveCount(2);
  await expect(cards.nth(0)).toHaveAttribute("data-decision-id", String(scope));
  await expect(cards.nth(1)).toHaveAttribute("data-decision-id", String(shape));
  const order = await chat
    .getByTestId("run-chat-messages")
    .evaluate((log) => Array.from(log.children).map((line) => line.getAttribute("data-testid")));
  expect(order).toEqual(["run-chat-request", "run-chat-agent", "run-chat-decision", "run-chat-decision"]);
  const first = cards.nth(0);
  await expect(first.getByRole("radio", { name: /Author runs as well/ })).toBeChecked();
  await expect(first.getByTestId("decision-plan-link")).toHaveCount(0);
  await expect(chat.locator('a[href*="/plans/"]')).toHaveCount(0);
  await expect(chat.getByTestId("run-chat-reply")).toBeVisible();
  await expect(chat.getByTestId("run-chat-end")).toBeVisible();
  await axeBoth(page, "an author run's chat with two decisions waiting");

  // The Trace's "Asked you" opens the card in the Chat.
  await main(page).getByRole("tab", { name: "Trace" }).click();
  const ask = main(page).getByTestId("trace-item").filter({ hasText: SHAPE });
  await expect(ask.getByTestId("trace-ask-link")).toHaveAttribute("href", `#run-decision-${shape}`);
  await ask.getByTestId("trace-ask-link").click();
  await expect(main(page).getByRole("tab", { name: "Chat" })).toHaveAttribute("aria-selected", "true");
  await expect(main(page).locator(`#run-decision-${shape}`)).toBeFocused();
  await expect(main(page).locator(`#run-decision-${shape}`)).toBeInViewport();

  // The owner picks the other option with the mouse, adds a few words and sends: the card folds into its answer.
  await first.getByText("Plan runs only", { exact: true }).click();
  await expect(first.getByRole("radio", { name: /Plan runs only/ })).toBeChecked();
  await first.getByTestId("decision-text").fill("Author runs come in the next plan.");
  await first.getByTestId("decision-send").click();
  await expect(first).toHaveAttribute("data-state", "answered");
  await expect(first.getByTestId("decision-answer")).toContainText("Author runs come in the next plan.");
  await expect(first.getByRole("radio")).toHaveCount(0);
  await expect(chat.getByTestId("run-chat-status")).toHaveText("1 decision waits for you");
  await cards.nth(1).getByTestId("decision-send").click();
  await expect(cards.nth(1)).toHaveAttribute("data-state", "answered");
  await expect(chat.getByTestId("run-chat-status")).not.toContainText("decision");
  // No answer comes back as a message of the chat.
  await expect(chat.getByTestId("run-chat-owner")).toHaveCount(0);
  expect((await chatOf(me, project, run.id)).messages.filter((message) => message.author === "owner")).toEqual([]);
  await axeBoth(page, "an author run's chat with its decisions answered");

  // The Inbox names the decision by its author run and its title, and links to no plan.
  const title = (await runOf(me, project, run.id)).title;
  await open(page, `/inbox?decision=${scope}`);
  const sheet = page.getByTestId("decision-sheet");
  await expect(sheet.getByTestId("decision-run-link")).toHaveText(`Author run #${run.id}`);
  await expect(sheet.getByTestId("decision-where")).toHaveText(`${project} / ${title ?? `author run #${run.id}`}`);
  await expect(sheet.getByTestId("decision-plan-link")).toHaveCount(0);
  await expect(page.locator('a[href*="/plans/null"]')).toHaveCount(0);
  await axeBoth(page, "the Inbox's sheet with an author run's decision");

  // At 375 px the Chat and its cards fit without scrolling sideways.
  await page.setViewportSize({ width: 375, height: 812 });
  await open(page, runPath(project, run.id));
  await expect(main(page).getByTestId("run-chat-decision")).toHaveCount(2);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect.soft(overflow, "the author run's chat at 375 px scrolls sideways").toBeLessThanOrEqual(0);
  await axeBoth(page, "an author run's chat with its decisions at 375 px");
});

test("another member reads an author run's decision card in its Chat without a form", async ({ page, member, admin }) => {
  test.setTimeout(90_000);
  await ensureAuthorSkill();
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  const project = me.projects[0];
  const writer = newAccount("writer");
  await admin.grant(project, writer.login, "writer", "internal");
  const live = await authorWorker(writer, project, uniqueName("their-desk"));
  const run = await dispatchAuthor(writer, project, live.worker.id, REQUEST);
  await startAuthor(live, run.id);
  await askTwo(live, run.id);

  await open(page, runPath(project, run.id));
  const chat = main(page).getByTestId("run-chat");
  await expect(chat.getByTestId("run-chat-status")).toHaveText(`2 decisions wait for ${writer.login}`);
  const card = chat.getByTestId("run-chat-decision").first();
  await expect(card.getByTestId("decision-locked")).toContainText(`Only ${writer.login}`);
  await expect(card.getByRole("radio")).toHaveCount(0);
  await expect(card.getByTestId("decision-option")).toHaveCount(2);
  await expect(chat.getByTestId("run-chat-reply")).toHaveCount(0);
  await axeBoth(page, "an author run's decision as another member reads it");
});
