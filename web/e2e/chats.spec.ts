import type { Page } from "@playwright/test";

import { createAgentByApi, PIXEL_PNG } from "./support/agents";
import {
  agentAnswers,
  agentAsks,
  agentSays,
  agentStarts,
  chatPageOf,
  chatPath,
  chatsOf,
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
  takeInbox,
} from "./support/chats";
import { expect, GITHUB_AVATARS, isDeployed, test } from "./support/fixtures";
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
 * and leaving; the row of agents opening a direct chat, each chat's face and last message in the list, and a group's
 * image changed from its head; a direct chat drawn as a chat app, the member's messages on the right and the agent's
 * answers in the line under them, Reply going on in the session. The specs are the worker: they claim the chat run
 * and post the agent's reply as the daemon does, so no model is asked anything. Pages render in English.
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

test("Enter sends a chat message and Shift Enter adds a line to it, as in messenger apps", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const chat = await createChatByApi(me, { title: uniqueName("Keys talk") });
  await open(page, chatPath(chat.id));
  const box = main(page).getByTestId("chat-composer-text");
  await expect(box).toHaveAttribute("aria-keyshortcuts", "Enter Meta+Enter Control+Enter");
  await expect(main(page).getByTestId("chat-composer-send").locator("kbd")).toHaveText("↵");
  await box.pressSequentially("The nightly build is red.");
  await box.press("Shift+Enter");
  await box.pressSequentially("Since Tuesday.");
  await expect(box).toHaveValue("The nightly build is red.\nSince Tuesday.");
  await box.press("Enter");
  await expect(box).toHaveValue("");
  await expect(messageOf(page, "The nightly build is red.")).toHaveCount(1);
  expect((await chatPageOf(me, chat.id)).messages.map((item) => item.body)).toEqual(["The nightly build is red.\nSince Tuesday."]);
  // Enter on a blank box sends nothing and says why; Cmd or Ctrl with Enter still sends.
  await box.press("Enter");
  await expect(main(page).getByTestId("chat-composer-problem")).toBeVisible();
  await box.fill("And the release notes.");
  await box.press("Control+Enter");
  await expect(messageOf(page, "And the release notes.")).toHaveCount(1);
});

test("under a mention the thread says at once that the agent got it, then each step of its run's setup, until its reply", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const handle = uniqueName("reviewer");
  await createAgentByApi(me, project, { handle, name: "Code reviewer", color: "#16a34a" });
  const workerName = uniqueName("mini");
  const live = await chatWorker(me, project, workerName);
  const chat = await createChatByApi(me, { title: "Setup talk", agents: [handle] });
  await open(page, chatPath(chat.id));

  // The answer to the message already says the agent got it and the worker its run waits for: a still, neutral dot.
  const box = main(page).getByTestId("chat-composer-text");
  await box.fill(`@${handle} is the wait test flaky?`);
  await box.press("Control+Enter");
  const asked = messageOf(page, "is the wait test flaky?");
  const line = asked.getByTestId("message-progress-step");
  await expect(line).toHaveText(`@${handle} got it, waiting for worker ${workerName}`);
  await expect(line).toHaveAttribute("data-step", "waiting");
  await expect(line).toHaveRole("status");
  await expect(line.locator(".animate-live-ping")).toHaveCount(0);
  const runId = Number(await line.getAttribute("data-run-id"));
  await expect(asked.getByTestId("mention-run-link")).toHaveAttribute("data-run-id", String(runId));

  // Each step its worker notes replaces the one before, live, with the running dot; no message comes with them.
  await claimOnly(live, runId);
  await expect(line).toHaveText(`@${handle}: worker ${workerName} claimed the run`);
  await expect(line.locator(".animate-live-ping")).toHaveCount(1);
  const steps: [string, string | undefined, string][] = [
    ["checking", undefined, `@${handle} is checking access to the repos`],
    ["opening", "evo-agents-harness", `@${handle} is opening evo-agents-harness`],
    ["starting", undefined, `@${handle} is starting up`],
  ];
  for (const [index, [setup, repo, said]] of steps.entries()) {
    await setupNote(live, runId, index + 1, setup, repo);
    await expect(line).toHaveText(said);
    await expect(asked.getByTestId("message-progress-step")).toHaveCount(1);
  }
  await agentStarts(live, runId);
  await expect(line).toHaveText(`@${handle} is working on it`);
  await expect(main(page).getByTestId("chat-log").getByTestId("chat-message")).toHaveCount(1);

  // A message in the thread while the agent works waits in the inbox of that turn, until the agent takes it.
  await asked.hover(); // React and Reply in thread show while the pointer is over the message
  await asked.getByTestId("message-reply").click();
  const thread = page.getByTestId("chat-thread");
  await expect(thread).toHaveAttribute("data-state", "ready");
  await thread.getByTestId("thread-composer-text").fill(`@${handle} and the retries?`);
  await thread.getByTestId("thread-composer-text").press("Control+Enter");
  const more = thread.getByTestId("chat-message").filter({ hasText: "and the retries?" }).getByTestId("message-progress-step");
  await expect(more).toHaveText(`@${handle} got it, in the inbox of the running turn`);
  await expect(more.locator(".animate-live-ping")).toHaveCount(0);

  // The agent's reply ends the line under the first message; the one it took next works until its own answer.
  await agentSays(live, runId, "Not flaky: it waits on the clock.");
  await expect(asked.getByTestId("message-progress")).toHaveCount(0);
  await expect(more).toHaveText(`@${handle} got it, in the inbox of the running turn`);
  await takeInbox(live, runId);
  await expect(more).toHaveText(`@${handle} is working on it`);
  await agentAnswers(live, runId, "Retries are bounded too.");
  await expect(thread.getByTestId("message-progress")).toHaveCount(0);
  await expect(asked.locator(`[data-testid="reaction-chip"][data-emoji="${CHECK}"]`)).toHaveAccessibleName(`Check mark: 1, by @${handle}`);
});

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

test("a chat run's decision shows under the agent's message in its thread and is answered with one click", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const handle = uniqueName("triager");
  await createAgentByApi(me, project, { handle, name: "Triage agent", color: "#16a34a" });
  const live = await chatWorker(me, project, uniqueName("mini"));
  const chat = await createChatByApi(me, { title: "Triage", agents: [handle] });
  const root = await sendByApi(me, chat.id, `@${handle} the wait test failed twice`);
  const runId = (await runsOf(me, project)).find((item) => item.chat_id === chat.id)?.id;
  expect(runId).toBeDefined();
  await claimChatRun(live, runId as number);
  const question = "The wait test fails twice in three. Retry or skip it?";
  const decision = await agentAsks(live, runId as number, "I can retry it or skip it.", question);

  // The hub's link to the thread (?message=ROOT) shows the root; its replies, with the card, open from the root's summary.
  await open(page, chatPath(chat.id, root.id));
  await page.getByTestId("thread-summary").click();
  const thread = page.getByTestId("chat-thread");
  const card = thread.getByTestId("chat-decision");
  await expect(card).toHaveCount(1);
  await expect(card).toHaveAttribute("data-decision-id", String(decision));
  await expect(card).toHaveAttribute("data-state", "open");
  await expect(card.getByRole("radio", { name: /Retry the flaky test/ })).toBeChecked();
  // It follows the agent's message in the thread: the card is inside that message's row.
  await expect(thread.getByTestId("chat-message").filter({ hasText: "I can retry it or skip it." }).getByTestId("chat-decision")).toHaveCount(1);

  // The agent's pick is checked: one click answers.
  await card.getByTestId("decision-send").click();
  await expect(card).toHaveAttribute("data-state", "answered");
  await expect(card.getByTestId("decision-answer-option")).toHaveAttribute("data-key", "retry");
});

test("the row of agents opens the member's direct chat with an agent, its composer focused, and the same chat on the next press", async ({ page, member }) => {
  const me = await member([
    { role: "admin", maxLevel: "internal" },
    { role: "reader", maxLevel: "internal" },
  ]);
  const [project, readOnly] = me.projects;
  const handle = uniqueName("reviewer");
  await createAgentByApi(me, project, { handle, name: "Code reviewer", color: "#16a34a" });

  // The agents of the project the member writes to, its default first; none of the project it only reads.
  await open(page, "/chats");
  const strip = main(page).getByTestId("agent-strip");
  await expect(strip).toHaveAttribute("data-state", "ready");
  const agents = strip.getByTestId("agent-strip-agent");
  await expect(agents).toHaveCount(2);
  expect(await agents.evaluateAll((items) => items.map((item) => item.getAttribute("data-handle")))).toEqual([project, handle]);
  await expect(strip.locator(`[data-project="${readOnly}"]`)).toHaveCount(0);
  const reviewer = strip.locator(`[data-testid="agent-strip-agent"][data-handle="${handle}"]`);
  await expect(reviewer).toHaveAccessibleName(`Chat with Code reviewer, @${handle} of ${project}`);
  await expect(reviewer).toContainText("Code reviewer");
  await reviewer.hover();
  await expect(page.getByRole("tooltip")).toContainText(`@${handle}, ${project}`);

  // Pressing it opens the direct chat, its composer focused: the agent's head, no members to add or take out.
  await reviewer.click();
  await page.waitForURL(/\/chats\/[0-9]+$/);
  const chatId = Number(new URL(page.url()).pathname.split("/").at(-1));
  const box = main(page).getByTestId("chat-composer-text");
  await expect(box).toBeFocused();
  await expect(box).toHaveAttribute("placeholder", "Message Code reviewer");
  await expect(main(page).getByTestId("chat-composer-hint")).toHaveText("Every message goes to Code reviewer, no @ needed: it answers under your message.");
  const head = main(page).getByTestId("chat-head");
  await expect(head).toHaveAttribute("data-kind", "direct");
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Code reviewer");
  await expect(head.getByTestId("chat-avatar")).toHaveAttribute("data-face", "agent");
  await expect(head.getByTestId("chat-agent-link")).toHaveText(`@${handle}`);
  await expect(head.getByTestId("chat-agent-link")).toHaveAttribute("href", `/p/${project}/agents/${handle}`);
  await expect(head.getByTestId("chat-agent-project")).toContainText(project);
  await expect(head.getByTestId("chat-members-open")).toHaveCount(0);
  await expect(head.getByTestId("chat-image-button")).toHaveCount(0);
  await expect(reviewer).toHaveAttribute("aria-current", "true");

  // Its row: the agent's face and name, nothing said yet.
  const row = main(page).locator(`[data-testid="chat-row"][data-chat-id="${chatId}"]`);
  await expect(row).toHaveAttribute("data-kind", "direct");
  await expect(row.getByTestId("chat-row-name")).toHaveText("Code reviewer");
  await expect(row.getByTestId("chat-avatar")).toHaveAttribute("data-face", "agent");
  await expect(row.getByTestId("chat-row-last")).toHaveText("No messages yet");

  // Details: the agent and the member for good, and the budget.
  await head.getByTestId("chat-details-open").click();
  const sheet = page.getByTestId("chat-members");
  await expect(sheet.getByRole("heading", { name: "Details" })).toBeVisible();
  await expect(sheet.getByTestId("chat-direct-note")).toContainText("start a group chat with New chat");
  for (const control of ["chat-add-agent-select", "chat-add-person-login", "chat-remove-agent", "chat-remove-person", "chat-leave"]) {
    await expect(sheet.getByTestId(control)).toHaveCount(0);
  }
  await expect(sheet.getByTestId("chat-budget-input")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();

  // From another page, Enter on the agent opens the same chat again; the hub holds one.
  await open(page, "/chats");
  await expect(strip).toHaveAttribute("data-state", "ready");
  await reviewer.focus();
  await page.keyboard.press("Enter");
  await page.waitForURL(`**/chats/${chatId}`);
  await expect(box).toBeFocused();
  expect((await openDirectByApi(me, handle)).id).toBe(chatId);
  expect((await chatsOf(me)).filter((item) => item.kind === "direct").map((item) => item.id)).toEqual([chatId]);
});

test("each chat's row shows its face, name and last message; a member changes a group's image from its head, with its errors said", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const mai = await signedInPerson("mai", [project]);
  const lan = await signedInPerson("lan", [project]);
  const release = await createChatByApi(me, { title: "Release talk", people: [mai.login], agents: [project] });
  const pair = await createChatByApi(me, { title: "Just us", people: [lan.login] });
  const direct = await openDirectByApi(me, project);
  await sendByApi(me, pair.id, "Lunch at noon?");
  await sendByApi(mai, release.id, "The **docs** are up for review.");
  // Lan's GitHub avatar fails to load: their initials stand in.
  await page.route(`${GITHUB_AVATARS}/u/${lan.id}*`, (route) => route.abort());

  await open(page, "/chats");
  const rowOf = (id: number) => main(page).locator(`[data-testid="chat-row"][data-chat-id="${id}"]`);
  // A group: two of its other members (Mai with her GitHub avatar, the agent), the last message as "author: text", unread in bold.
  const releaseRow = rowOf(release.id);
  await expect(releaseRow.getByTestId("chat-avatar")).toHaveAttribute("data-face", "members");
  await expect(releaseRow.getByTestId("person-avatar").locator("img")).toHaveAttribute("src", new RegExp(`^${GITHUB_AVATARS}/u/${mai.id}\\?`));
  await expect(releaseRow.getByTestId("agent-avatar")).toHaveCount(1);
  await expect(releaseRow.getByTestId("chat-row-last")).toHaveText(`${mai.login}: The docs are up for review.`);
  await expect(releaseRow).toHaveAccessibleName("Release talk, 1 unread message");
  await expect(releaseRow).toHaveAccessibleDescription(`${mai.login}: The docs are up for review.`);
  await expect(releaseRow.getByTestId("chat-row-name")).toHaveCSS("font-weight", "600");
  await expect(releaseRow.getByTestId("chat-row-time")).toHaveAttribute("datetime", /.+/);
  // One other member: that member's face, here their initials since their avatar failed; the member's own message.
  const pairRow = rowOf(pair.id);
  await expect(pairRow.getByTestId("person-avatar")).toHaveCount(1);
  await expect(pairRow.getByTestId("person-avatar")).toHaveText("EL"); // e2e-lan-...
  await expect(pairRow.getByTestId("person-avatar").locator("img")).toHaveCount(0);
  await expect(pairRow.getByTestId("chat-row-last")).toHaveText("You: Lunch at noon?");
  await expect(pairRow.getByTestId("chat-row-name")).toHaveCSS("font-weight", "500");
  // A direct chat: its agent's face and name.
  await expect(rowOf(direct.id).getByTestId("chat-avatar")).toHaveAttribute("data-face", "agent");

  // The group's head: its face is the button that changes its image; the menu says what it takes.
  await releaseRow.click();
  await page.waitForURL(`**/chats/${release.id}`);
  const head = main(page).getByTestId("chat-head");
  const face = head.getByTestId("chat-image-button");
  await expect(face).toHaveAccessibleName("Chat image: upload one");
  await expect(face.getByTestId("chat-avatar")).toHaveAttribute("data-face", "members");
  await face.click();
  const menu = page.getByTestId("chat-image-menu");
  await expect(menu).toContainText("PNG, JPEG or WebP, at most 512 KiB.");
  await expect(menu.getByTestId("chat-image-remove")).toHaveCount(0);
  await page.keyboard.press("Escape");

  // A file of another type, then one over 512 KiB: each refused under the head before anything is sent.
  const input = head.getByTestId("chat-image-input");
  await input.setInputFiles({ name: "face.gif", mimeType: "image/gif", buffer: Buffer.from("GIF89a") });
  await expect(head.getByTestId("chat-image-problem")).toHaveText("Pick a PNG, JPEG or WebP image.");
  await input.setInputFiles({ name: "big.png", mimeType: "image/png", buffer: Buffer.concat([PIXEL_PNG, Buffer.alloc(512 * 1024)]) });
  await expect(head.getByTestId("chat-image-problem")).toHaveText("The image is over 512 KiB.");
  expect((await chatPageOf(me, release.id)).chat.image_url).toBeNull();

  // A PNG: the head and the row show it, and Mai's list has it too.
  await input.setInputFiles({ name: "face.png", mimeType: "image/png", buffer: PIXEL_PNG });
  await expect(toast(page, "Chat image changed")).toBeVisible();
  await expect(head.getByTestId("chat-image-problem")).toHaveCount(0);
  await expect(face.getByTestId("chat-avatar")).toHaveAttribute("data-face", "image");
  await expect(face.getByTestId("chat-image")).toBeVisible();
  await expect(face).toHaveAccessibleName("Chat image: change or remove it");
  await expect(releaseRow.getByTestId("chat-avatar")).toHaveAttribute("data-face", "image");
  expect((await chatsOf(mai)).find((item) => item.id === release.id)?.image_url).toMatch(new RegExp(`^/v1/chats/${release.id}/image\\?`));

  // Removed after a confirmation: the members' faces again.
  await face.click();
  await menu.getByTestId("chat-image-remove").click();
  const confirm = page.getByTestId("chat-image-remove-confirm");
  await expect(confirm.getByRole("heading", { name: "Remove the image of Release talk?" })).toBeVisible();
  await confirm.getByRole("button", { name: "Remove image" }).click();
  await expect(toast(page, "Chat image removed")).toBeVisible();
  await expect(face.getByTestId("chat-avatar")).toHaveAttribute("data-face", "members");

  // Mai sets one: it arrives live, without a reload.
  await putChatImageByApi(mai, release.id, PIXEL_PNG);
  await expect(face.getByTestId("chat-avatar")).toHaveAttribute("data-face", "image");
  await expect(face.getByTestId("chat-image")).toBeVisible();
});

test("a direct chat reads as a chat app: the member's messages on the right, the agent's answers under them, and Reply goes on in the session", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const handle = uniqueName("reviewer");
  await createAgentByApi(me, project, { handle, name: "Code reviewer", color: "#16a34a" });
  const live = await chatWorker(me, project, uniqueName("mini"), 2);
  const chat = await openDirectByApi(me, handle);
  await open(page, chatPath(chat.id));
  await expect(main(page).getByTestId("chat-empty")).toContainText("every message goes to Code reviewer, and its answer shows right under yours");

  // A message without @: the member's own, on the right with no face; the agent took it as a new task.
  const box = main(page).getByTestId("chat-composer-text");
  await box.fill("Is the wait test flaky?");
  await box.press("Control+Enter");
  const asked = messageOf(page, "Is the wait test flaky?");
  await expect(asked).toHaveAttribute("data-mine", "true");
  await expect(asked.getByTestId("person-avatar")).toHaveCount(0);
  const runId = Number(await asked.getByTestId("mention-run-link").getAttribute("data-run-id"));
  await claimChatRun(live, runId);
  await agentAnswers(live, runId, "Not flaky: it waits on the clock.");

  // The answer arrives live in the chat's own line, right under the message: the agent's face and name, no thread.
  const answer = messageOf(page, "Not flaky: it waits on the clock.");
  await expect(answer).toHaveAttribute("data-author", "agent");
  await expect(answer).toHaveAttribute("data-mine", "false");
  await expect(answer.getByTestId("agent-avatar")).toBeVisible();
  await expect(answer).toContainText("Code reviewer");
  await expect(answer.getByTestId("message-quote")).toHaveCount(0);
  await expect(main(page).getByTestId("thread-summary")).toHaveCount(0);
  const line = main(page).getByTestId("chat-log").getByTestId("chat-message");
  const askedId = Number(await asked.getAttribute("data-message-id"));
  const answerId = Number(await answer.getAttribute("data-message-id"));
  expect(await line.evaluateAll((items) => items.map((item) => Number(item.getAttribute("data-message-id"))))).toEqual([askedId, answerId]);
  const log = (await main(page).getByTestId("chat-log").boundingBox())!;
  const mine = (await asked.getByTestId("message-bubble").boundingBox())!;
  const theirs = (await answer.getByTestId("message-bubble").boundingBox())!;
  expect(log.x + log.width - (mine.x + mine.width), "the member's bubble ends at the right").toBeLessThan(32);
  expect(theirs.x - log.x, "the agent's bubble starts after its face, at the left").toBeLessThan(80);
  expect(theirs.y).toBeGreaterThan(mine.y);

  // Reply on the answer: the composer says what it answers and takes the focus; the reply goes on in the session.
  await answer.hover();
  await answer.getByTestId("message-reply").click();
  const replying = main(page).getByTestId("chat-composer-replying");
  await expect(replying).toContainText("Replying to Code reviewer");
  await expect(replying).toContainText("Not flaky: it waits on the clock.");
  await expect(box).toBeFocused();
  await expect(main(page).getByTestId("chat-composer-hint")).toHaveText("Your reply goes on in the same session, so Code reviewer picks up where it left off.");
  await box.press("Escape");
  await expect(replying).toHaveCount(0);
  await answer.hover();
  await answer.getByTestId("message-reply").click();
  await box.fill("And the retries?");
  await box.press("Control+Enter");
  await expect(replying).toHaveCount(0);
  const followUp = messageOf(page, "And the retries?");
  await expect(followUp).toHaveAttribute("data-mine", "true");
  const thread = (await chatPageOf(me, chat.id, askedId)).messages;
  expect(thread.map((item) => item.body)).toEqual(["Is the wait test flaky?", "Not flaky: it waits on the clock.", "And the retries?"]);
  expect(thread[2].mentions.map((mention) => [mention.handle, mention.via])).toEqual([[handle, "thread"]]);
  const followRun = thread[2].mentions[0].run_id as number;

  // A new task while that turn waits; the turn's answer then comes after it, so it quotes the reply it answers.
  await box.fill("Check the docs too?");
  await box.press("Control+Enter");
  await expect(messageOf(page, "Check the docs too?")).toBeVisible();
  await claimChatRun(live, followRun);
  await agentAnswers(live, followRun, "Retries are bounded too.");
  const late = messageOf(page, "Retries are bounded too.");
  const quote = late.getByTestId("message-quote");
  await expect(quote).toHaveAccessibleName("Show the message this answers, by You: And the retries?");
  await quote.click();
  await expect(followUp).toHaveAttribute("data-highlighted", "true");
  await expect(followUp).toBeFocused();
});
