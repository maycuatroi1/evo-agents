import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { chatPath } from "./support/chats";
import { expect, isDeployed, test } from "./support/fixtures";
import { open } from "./support/plans";
import { finishWork, listedWork, seedWork, workSystem } from "./support/work";

/**
 * Work chats (web/DESIGN.md, Work card): the chat list groups them under their system with each work's state and what
 * it waits for, the done ones folded; a work chat's head names its state, number, system and coordinator; the card
 * pinned under it gives the goal, figures, sub-tasks (each opens its agent's thread) and decisions, an open one of the
 * visitor answered in place; the question that opened a thread carries its ask card; the root thread where the person
 * asked shows the live card with Open work chat, then Done with the totals. Pages render in English.
 */
test.skip(isDeployed, "makes agents, systems and workers through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

test("the chat list groups work chats under their system, open ones with their state, done ones folded", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const system = await workSystem(me, me.projects[0]);
  const open_ = await seedWork(me, system, "Refund backlog");
  const done = await seedWork(me, system, "Invoice cleanup", { decide: false });
  await finishWork(system, done);
  await expect.poll(async () => (await listedWork(me, done.workChat))?.status).toBe("done");

  await open(page, "/chats");
  const group = main(page).getByTestId("work-group");
  await expect(group).toHaveCount(1);
  await expect(group.getByTestId("work-group-name")).toHaveText(system.name);
  const row = group.locator(`[data-chat-id="${open_.workChat}"]`);
  await expect(row).toHaveAttribute("data-work", "needs_person");
  await expect(row.getByTestId("chat-row-name")).toHaveText("Refund backlog");
  await expect(row.getByTestId("work-dot")).toHaveAttribute("data-status", "needs_person");
  await expect(row.getByTestId("work-row-line")).toHaveText(`Needs a person: Waiting for ${me.login} to decide`);

  // The done work is folded under its count; opening it shows its row.
  const folded = group.getByTestId("work-done-group");
  await expect(folded.locator("summary")).toHaveText("1 done work");
  const doneRow = group.locator(`[data-chat-id="${done.workChat}"]`);
  await expect(doneRow).toBeHidden();
  await folded.locator("summary").click();
  await expect(doneRow).toHaveAttribute("data-work", "done");
  await expect(doneRow.getByTestId("work-row-line")).toHaveText("Done: Done");

  // The chats people asked in stay with the other chats.
  await expect(main(page).getByTestId("chats-rows").locator(`[data-chat-id="${open_.rootChat}"]`)).toBeVisible();
});

test("a work chat pins its card, opens a sub-task's thread from the table and answers a decision in the card", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const system = await workSystem(me, me.projects[0]);
  const work = await seedWork(me, system, "Refund backlog");
  await open(page, chatPath(work.workChat));

  // The head: the title, the state, Work #N, the system linking to Observe, the coordinator.
  const head = main(page).getByTestId("chat-header");
  await expect(head.getByTestId("chat-title")).toHaveText("Refund backlog");
  await expect(head.getByTestId("work-head-status")).toHaveText("Needs a person");
  await expect(head.getByTestId("work-head-number")).toHaveText(`Work #${work.traceId}`);
  await expect(head.getByTestId("work-head-system")).toHaveAttribute("href", `/p/${system.project}/systems/${system.slug}?mode=observe`);
  await expect(head.getByTestId("work-head-coordinator")).toHaveText(`Coordinator@${system.lead}`);

  // The pinned card: what it waits for, the figures, the goal, the sub-task and the decision.
  const card = main(page).getByTestId("work-card");
  await expect(card).toHaveAttribute("data-status", "needs_person");
  await expect(card.getByTestId("work-card-waiting")).toHaveText(`Waiting for ${me.login} to decide`);
  await expect(card.getByTestId("work-card-runs")).toContainText("2 of");
  await expect(card.getByTestId("work-card-hop")).toHaveText(/^\d of \d+$/);
  await expect(card.getByTestId("work-card-goal")).toHaveText("Every refund ticket of Northwind has a reply.");
  await expect(card.getByTestId("work-card-done-when")).toHaveText("No refund ticket is left without a reply.");
  const subtask = card.getByTestId("work-subtask");
  await expect(subtask).toHaveCount(1);
  await expect(subtask).toHaveAttribute("data-agent", system.helper);
  await page.screenshot({
    path: test.info().outputPath("work-chat-light.png"),
  });

  // The composer says where a message goes.
  await expect(main(page).getByTestId("chat-composer-text")).toHaveAttribute("placeholder", `Goes to @${system.lead}, the coordinator, unless you @ an agent`);

  // The ask card on the main line, under the question that opened @helper's thread.
  const ask = main(page).getByTestId("work-ask-card");
  await expect(ask).toHaveCount(1);
  await expect(ask.getByRole("heading")).toHaveText(`@${system.lead} asked @${system.helper}`);
  await expect(ask.getByTestId("work-ask-brief")).toContainText("Draft the refund replies");

  // A sub-task's row opens its thread beside the chat.
  await subtask.getByTestId("work-subtask-open").click();
  const thread = page.getByTestId("chat-thread");
  await expect(thread).toBeVisible();
  await expect(thread).toContainText("Two tickets ask for more than the policy allows.");
  await thread.getByTestId("chat-thread-close").click();
  await expect(thread).toBeHidden();

  // The decision that asks the visitor is answered in the card.
  const answer = card.getByTestId("work-decision-answer");
  await expect(answer).toHaveAttribute("data-decision-id", String(work.decision));
  await expect(answer.getByRole("radio", { name: /Retry the flaky test/ })).toBeChecked();
  await answer.getByTestId("decision-send").click();
  await expect(card.getByTestId("work-decision-answer")).toHaveCount(0);
  await expect(card.getByTestId("work-decision").filter({ hasText: "Refund the two tickets" })).toHaveAttribute("data-state", "answered");
  await expect(card).not.toHaveAttribute("data-status", "needs_person");
});

test("the root thread shows the live work card with Open work chat, then Done with the totals", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const system = await workSystem(me, me.projects[0]);
  const work = await seedWork(me, system, "Invoice cleanup", { decide: false });
  await open(page, chatPath(work.rootChat, work.root));
  await main(page).getByTestId("thread-summary").first().click();
  const thread = page.getByTestId("chat-thread");
  const card = thread.getByTestId("work-root-card");
  await expect(card).toHaveCount(1);
  await expect(card.getByRole("heading")).toHaveText("Invoice cleanup");
  await expect(card).toHaveAttribute("data-status", "running");
  await expect(card.getByTestId("work-root-open")).toHaveAttribute("href", `/chats/${work.workChat}`);
  await page.screenshot({ path: test.info().outputPath("work-root-card.png") });

  // The work ends: the card follows without a reload.
  await finishWork(system, work);
  await expect(card).toHaveAttribute("data-status", "done", {
    timeout: 20_000,
  });
  await expect(card.getByTestId("work-root-line")).toHaveText(/^Done: 2 runs, .+, .+$/);

  await card.getByTestId("work-root-open").click();
  await page.waitForURL(new RegExp(`/chats/${work.workChat}$`));
  await expect(main(page).getByTestId("work-card")).toHaveAttribute("data-status", "done");
});

for (const scheme of ["light", "dark"] as const) {
  test.describe(`${scheme} theme`, () => {
    test.use({ colorScheme: scheme });

    test(`a work chat and the grouped list have no serious axe violation (${scheme})`, async ({ page, member }) => {
      const me = await member([{ role: "admin", maxLevel: "internal" }]);
      const system = await workSystem(me, me.projects[0]);
      const work = await seedWork(me, system, "Refund backlog");
      await open(page, chatPath(work.workChat));
      await expect(page.locator("html")).toHaveClass(new RegExp(scheme));
      await expect(main(page).getByTestId("work-decision-answer")).toBeVisible();
      await expect(main(page).getByTestId("work-ask-card")).toBeVisible();
      await expectNoSeriousViolations(page, `work chat (${scheme})`);
      if (scheme === "dark")
        await page.screenshot({
          path: test.info().outputPath("work-chat-dark.png"),
        });
      await open(page, "/chats");
      await expect(main(page).getByTestId("work-group")).toBeVisible();
      await expectNoSeriousViolations(page, `chat list with work chats (${scheme})`);
    });
  });
}

test("at 375 px the work chat folds its card to the figures and nothing scrolls sideways", async ({ page, member }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const system = await workSystem(me, me.projects[0]);
  const work = await seedWork(me, system, "Refund backlog");
  await open(page, chatPath(work.workChat));
  const card = main(page).getByTestId("work-card");
  await expect(card.getByTestId("work-card-figures")).toBeVisible();
  await expect(card.getByTestId("work-card-body")).toBeHidden();
  const toggle = card.getByTestId("work-card-toggle");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await toggle.click();
  await expect(card.getByTestId("work-card-body")).toBeVisible();
  await expect(card.getByTestId("work-subtask")).toHaveCount(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(375);
  await page.screenshot({ path: test.info().outputPath("work-chat-375.png") });
  await expectNoSeriousViolations(page, "work chat at 375 px");
});
