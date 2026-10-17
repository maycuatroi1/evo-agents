import type { Page } from "@playwright/test";

import { createAgentByApi } from "./support/agents";
import {
  agentAnswers,
  chatPageOf,
  chatPath,
  chatsOf,
  chatWorker,
  claimChatRun,
  createChatByApi,
  reactByApi,
  sendByApi,
  signedInPerson,
} from "./support/chats";
import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { runsOf } from "./support/runs";
import { toast } from "./support/toast";

/**
 * The Chats module against the real API (docs/hub.md, Group chats): a member makes a chat from the sidebar with an
 * agent and a person, mentions the agent through the @ suggestions, and watches the run that makes come in live (the
 * agent's eyes, its reply in the thread with the run and the draft pull request, the check mark); reactions put and
 * taken off, and another member's arriving live; a thread opened from a link to a reply, answered in place, and the
 * hub's answer to a mention it refuses; the members sheet adding and taking out an agent and a person, the budget,
 * and leaving. The specs are the worker: they claim the chat run and post the agent's reply as the daemon does, so no
 * model is asked anything. Pages render in English.
 */
test.skip(isDeployed, "makes chats through the local stack");

const PARTY = "\u{1F389}";
const EYES = "\u{1F440}";
const CHECK = "✅";

function main(page: Page) {
  return page.locator("#main");
}

function messageOf(page: Page, text: string) {
  return main(page).getByTestId("chat-log").getByTestId("chat-message").filter({ has: page.getByTestId("message-body").filter({ hasText: text }) });
}

test("a member makes a chat, mentions its agent through the suggestions and follows its run live to the reply and the check mark", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const handle = uniqueName("reviewer");
  await createAgentByApi(me, project, { handle, name: "Code reviewer", color: "#16a34a" });
  const mai = await signedInPerson("mai", [project]);
  const live = await chatWorker(me, project, uniqueName("mini"));

  // The sidebar leads to Chats, which says what a chat is for while there is none.
  await open(page, "/");
  await page.getByTestId("nav-chats").click();
  await page.waitForURL("**/chats");
  await expect(main(page).getByRole("heading", { level: 1, name: "Chats" })).toBeVisible();
  await expect(page.getByTestId("nav-chats")).toHaveAttribute("aria-current", "page");
  await expect(main(page).getByTestId("chats-empty")).toBeVisible();

  // New chat: a title, the agent of the project the member writes to, and a person typed by login.
  await main(page).getByTestId("chats-empty-new").click();
  const dialog = page.getByTestId("new-chat-dialog");
  await expect(dialog.getByRole("heading", { name: "New chat" })).toBeVisible();
  await dialog.getByTestId("new-chat-submit").click();
  await expect(dialog.getByTestId("new-chat-title-hint")).toHaveText("Give the chat a title.");
  await dialog.getByTestId("new-chat-title").fill("Release talk");
  await dialog.locator(`[data-testid="new-chat-agent"][data-handle="${handle}"]`).check();
  await expect(dialog.getByTestId("new-chat-agents-count")).toHaveText("1 picked, of at most 10");
  await dialog.getByTestId("new-chat-login").fill(mai.login);
  await dialog.getByTestId("new-chat-add-person").click();
  await expect(dialog.getByTestId("new-chat-people-list")).toContainText(mai.login);
  await dialog.getByTestId("new-chat-submit").click();
  await page.waitForURL(/\/chats\/[0-9]+$/);
  const chatId = Number(new URL(page.url()).pathname.split("/").at(-1));
  await expect(toast(page, "Chat Release talk created")).toBeVisible();
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Release talk");
  await expect(main(page).getByTestId("chat-who")).toContainText(`@${handle}`);
  await expect(main(page).getByTestId("chat-who")).toContainText(mai.login);
  await expect(main(page).locator(`[data-testid="chat-row"][data-chat-id="${chatId}"]`)).toHaveAttribute("aria-current", "page");

  // @ opens the suggestions: the agent by handle, the project (its default agent) and the person; Enter puts it in.
  const box = main(page).getByTestId("chat-composer-text");
  await box.click();
  await box.pressSequentially("@");
  const menu = main(page).getByTestId("mention-menu");
  await expect(menu.locator(`[data-testid="mention-option"][data-name="${handle}"]`)).toHaveAttribute("data-kind", "agent");
  await expect(menu.locator(`[data-testid="mention-option"][data-name="${mai.login}"]`)).toHaveAttribute("data-kind", "person");
  await box.pressSequentially(handle.slice(0, 8));
  await expect(menu.getByRole("option", { selected: true })).toHaveAttribute("data-name", handle);
  await box.press("Enter");
  await expect(box).toHaveValue(`@${handle} `);
  await box.pressSequentially("please fix the flaky wait test");
  await expect(main(page).getByTestId("chat-composer-hint")).toHaveText(`Hands work to @${handle}.`);
  await box.press("Control+Enter");
  await expect(box).toHaveValue("");

  // The mention made one chat run of the agent, for the member, on their worker; the message links it.
  const asked = messageOf(page, "please fix the flaky wait test");
  const runLink = asked.getByTestId("mention-run-link");
  await expect(runLink).toBeVisible();
  const runId = Number(await runLink.getAttribute("data-run-id"));
  await expect(runLink).toHaveAttribute("href", `/p/${project}/runs/${runId}`);
  const run = (await runsOf(me, project)).find((item) => item.id === runId);
  expect([run?.kind, run?.profile, run?.dispatched_by, run?.chat_id]).toEqual(["chat", handle, me.login, chatId]);

  // The worker claims it: the agent's eyes arrive on the message without a reload.
  await claimChatRun(live, runId);
  const eyes = asked.locator(`[data-testid="reaction-chip"][data-emoji="${EYES}"]`);
  await expect(eyes).toHaveAccessibleName(`Eyes: 1, by @${handle}`);
  await expect(eyes).toHaveAttribute("aria-pressed", "false");

  // The agent's reply in the thread, then the run done: the check mark takes the eyes' place.
  const pull = "https://github.com/example-org/api/pull/7";
  await agentAnswers(live, runId, `Fixed the wait on chat/${runId}. Draft PR: ${pull}`);
  await expect(asked.getByTestId("thread-summary")).toContainText("1 reply");
  await expect(asked.locator(`[data-testid="reaction-chip"][data-emoji="${CHECK}"]`)).toHaveAccessibleName(`Check mark: 1, by @${handle}`);
  await expect(eyes).toHaveCount(0);

  // The thread: the agent's face and name, its run and its draft pull request.
  await asked.getByTestId("thread-summary").click();
  const thread = page.getByTestId("chat-thread");
  await expect(thread.getByRole("heading", { name: "Thread" })).toBeVisible();
  const reply = thread.getByTestId("chat-message").filter({ hasText: "Fixed the wait" });
  await expect(reply).toHaveAttribute("data-author", "agent");
  await expect(reply).toContainText("Code reviewer");
  await expect(reply.getByTestId("agent-avatar")).toBeVisible();
  await expect(reply.getByTestId("message-pull-link")).toHaveAttribute("href", pull);
  await expect(reply.getByTestId("message-pull-link")).toContainText("PR #7 in example-org/api");
  await expect(page).toHaveURL(new RegExp(`/chats/${chatId}\\?thread=`));

  // The run's page names its kind and leads back to the message it answers.
  await reply.getByTestId("message-run-link").click();
  await page.waitForURL(`**/p/${project}/runs/${runId}`);
  await expect(main(page).getByTestId("run-detail")).toHaveAttribute("data-kind", "chat");
  const back = main(page).getByTestId("run-chat-link");
  await expect(back).toHaveAttribute("href", chatPath(chatId, (await chatPageOf(me, chatId)).messages[0].id));
  await back.click();
  await page.waitForURL(`**/chats/${chatId}?message=*`);
  await expect(main(page).getByTestId("chat-log").locator("[data-highlighted=true]")).toContainText("please fix the flaky wait test");
});

test("reactions: a member puts an emoji and takes it off, another's arrives live, and reading the chat clears its unread count", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const mai = await signedInPerson("mai", [project]);
  const chat = await createChatByApi(me, { title: "Docs week", people: [mai.login], agents: [project] });
  const hello = await sendByApi(mai, chat.id, "The docs are up for review.");

  // Mai's message waits unread: in the sidebar's count and on the chat's row.
  await open(page, "/chats");
  await expect(page.getByTestId("nav-chats-count")).toHaveText("1");
  await expect(page.getByTestId("nav-chats")).toHaveAccessibleName("Chats, 1 unread chat message");
  const row = main(page).locator(`[data-testid="chat-row"][data-chat-id="${chat.id}"]`);
  await expect(row).toHaveAttribute("data-unread", "1");
  await expect(row).toHaveAccessibleName("Docs week, 1 unread message");
  await row.click();
  await page.waitForURL(`**/chats/${chat.id}`);
  await expect(row).toHaveAttribute("data-unread", "0");
  await expect(page.getByTestId("nav-chats-count")).toHaveCount(0);
  expect((await chatsOf(me)).find((item) => item.id === chat.id)?.unread).toBe(0);

  // Add a reaction from the message's actions, then take it off from its chip.
  const message = messageOf(page, "The docs are up for review.");
  await message.hover();
  await message.getByTestId("reaction-add").click();
  await page.getByTestId("reaction-picker").getByRole("button", { name: "Party popper" }).click();
  const party = message.locator(`[data-testid="reaction-chip"][data-emoji="${PARTY}"]`);
  await expect(party).toHaveAccessibleName("Party popper: 1, by you");
  await expect(party).toHaveAttribute("aria-pressed", "true");
  expect((await chatPageOf(mai, chat.id)).messages.find((item) => item.id === hello.id)?.reactions).toEqual([
    { emoji: PARTY, count: 1, mine: false, people: [me.login], agents: [] },
  ]);

  // Mai puts one too: it arrives live, and the chip counts both.
  await reactByApi(mai, chat.id, hello.id, PARTY);
  await expect(party).toHaveAccessibleName(`Party popper: 2, by you and ${mai.login}`);
  await party.click();
  await expect(party).toHaveAccessibleName(`Party popper: 1, by ${mai.login}`);
  await expect(party).toHaveAttribute("aria-pressed", "false");
  expect((await chatPageOf(mai, chat.id)).messages.find((item) => item.id === hello.id)?.reactions[0].people).toEqual([mai.login]);

  // A message of Mai's arrives live too, and the open chat reads it at once.
  await sendByApi(mai, chat.id, "Thanks for the party.");
  await expect(messageOf(page, "Thanks for the party.")).toBeVisible();
  await expect.poll(async () => (await chatsOf(me)).find((item) => item.id === chat.id)?.unread).toBe(0);
});

test("threads: a link to a reply opens its thread marked, a reply goes in it, and a mention the hub refuses is answered there", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const outsider = uniqueName("outsider");
  await createAgentByApi(me, project, { handle: outsider, name: "Outsider", color: "#7c3aed" });
  const mai = await signedInPerson("mai", [project]);
  const chat = await createChatByApi(me, { title: "Flaky tests", people: [mai.login], agents: [project] });
  const root = await sendByApi(me, chat.id, "Who looks at the flaky wait test?");
  const answer = await sendByApi(mai, chat.id, "I can, after lunch.", root.id);

  // A notice's link: the reply, shown in its thread and marked.
  await open(page, chatPath(chat.id, answer.id));
  const thread = page.getByTestId("chat-thread");
  await expect(thread).toHaveAttribute("data-root", String(root.id));
  await expect(thread.locator("[data-highlighted=true]")).toContainText("I can, after lunch.");
  await expect(thread.getByTestId("chat-thread-count")).toHaveText("1 reply");

  // A reply in the thread: in it, counted under the first message.
  const box = thread.getByTestId("thread-composer-text");
  await box.fill("Thanks, ping me when done.");
  await thread.getByTestId("thread-composer-send").click();
  await expect(thread.getByTestId("chat-message").filter({ hasText: "Thanks, ping me when done." })).toBeVisible();
  await expect(messageOf(page, "Who looks at the flaky wait test?").getByTestId("thread-summary")).toContainText("2 replies");
  expect((await chatPageOf(me, chat.id, root.id)).messages.map((item) => item.body)).toEqual([
    "Who looks at the flaky wait test?",
    "I can, after lunch.",
    "Thanks, ping me when done.",
  ]);
  await thread.getByTestId("chat-thread-close").click();
  await expect(thread).toHaveCount(0);

  // An agent the member sees that is not in the chat: the mention says why, and the hub answers in its thread.
  const composer = main(page).getByTestId("chat-composer-text");
  await composer.fill(`@${outsider} can you take it?`);
  await composer.press("Control+Enter");
  const asked = messageOf(page, "can you take it?");
  await expect(asked.getByTestId("mention-refused")).toContainText(`@${outsider} took no work:`);
  await expect(asked.getByTestId("mention-refused")).toContainText("is not in this chat");
  await expect(asked.getByTestId("thread-summary")).toContainText("1 reply");
  await asked.getByTestId("thread-summary").click();
  const hub = page.getByTestId("chat-thread").locator('[data-testid="chat-message"][data-author="hub"]');
  await expect(hub).toContainText(`@${outsider} is not in this chat: add it to the chat, then mention it again`);
});

test("members: add an agent and a person, take the agent out, change the budget and leave the chat", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const handle = uniqueName("helper");
  await createAgentByApi(me, project, { handle, name: "Helper", color: "#d97706" });
  const lan = await signedInPerson("lan", [project]);
  const chat = await createChatByApi(me, { title: "Just me" });

  await open(page, chatPath(chat.id));
  await expect(main(page).getByTestId("chat-empty")).toBeVisible();
  await main(page).getByTestId("chat-members-open").click();
  const sheet = page.getByTestId("chat-members");
  await expect(sheet.getByRole("heading", { name: "Members" })).toBeVisible();

  await sheet.getByTestId("chat-add-agent-select").selectOption(handle);
  await sheet.getByTestId("chat-add-agent-submit").click();
  await expect(toast(page, `@${handle} added to the chat`)).toBeVisible();
  await expect(sheet.locator(`[data-testid="chat-member-agent"][data-handle="${handle}"]`)).toContainText("Helper");

  await sheet.getByTestId("chat-add-person-login").fill("not a login");
  await sheet.getByTestId("chat-add-person-submit").click();
  await expect(sheet.getByTestId("chat-add-person-hint")).toContainText("A GitHub login is letters, digits and dashes");
  await sheet.getByTestId("chat-add-person-login").fill(lan.login);
  await sheet.getByTestId("chat-add-person-submit").click();
  await expect(sheet.locator(`[data-testid="chat-member-person"][data-login="${lan.login}"]`)).toBeVisible();

  await sheet.locator(`[data-testid="chat-member-agent"][data-handle="${handle}"]`).getByTestId("chat-remove-agent").click();
  await expect(sheet.locator(`[data-testid="chat-member-agent"][data-handle="${handle}"]`)).toHaveCount(0);

  await sheet.getByTestId("chat-budget-input").fill("7.5");
  await sheet.getByTestId("chat-budget-save").click();
  await expect(toast(page, "Budget changed")).toBeVisible();
  const held = await chatPageOf(me, chat.id);
  expect([held.chat.budget_usd, held.chat.people.map((person) => person.login).sort(), held.chat.agents]).toEqual([7.5, [me.login, lan.login].sort(), []]);

  await sheet.getByTestId("chat-leave").click();
  const confirm = page.getByTestId("chat-leave-confirm");
  await confirm.getByRole("button", { name: "Leave" }).click();
  await page.waitForURL("**/chats");
  await expect(toast(page, "You left Just me")).toBeVisible();
  await expect(main(page).locator(`[data-testid="chat-row"][data-chat-id="${chat.id}"]`)).toHaveCount(0);
});
