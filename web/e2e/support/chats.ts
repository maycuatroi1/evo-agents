import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { API_URL } from "./env";
import { type Account, HubAdmin, machineToken, newAccount } from "./hub";
import { apiOf } from "./memories";
import { type LiveWorker, liveWorker, MODELS, REPO } from "./runs";
import { HARNESS_CHECKOUT } from "./author";
import { PROTOCOL_HEADER } from "./workers";

/**
 * Group chats through the real API (docs/hub.md, Group chats): chats made and messages sent as `evo-agents hub chat
 * create|send` does, and the worker's side of a chat run (docs/workers.md, Chat runs) with the worker's token, as the
 * daemon does: its heartbeat says it runs chat runs, it claims the run a mention made, and posts the agent's reply at
 * the end of the turn. The specs are the fake worker; no model is asked anything.
 */
export type Chat = components["schemas"]["Chat"];
export type ChatPage = components["schemas"]["ChatPage"];
export type GroupMessage = components["schemas"]["GroupMessage"];

/**
 * What a daemon of this release says it runs, the feature that says it applies agent profiles, and the one that says it
 * gives the Claude Code agent of a profile other than the project's default a config dir of its own (`runs.AGENT_HOME`),
 * without which the hub hands it no run of such a profile.
 */
export const CHAT_RUN_KINDS = ["step", "plan", "review", "judge", "author", "chat"];
export const FEATURES = ["agent_profiles", "agent_home"];

async function workerPost(live: LiveWorker, path: string, body: unknown): Promise<unknown> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${live.token}`, ...PROTOCOL_HEADER },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`POST ${path}: ${response.status} ${await response.text()}`);
  return response.json();
}

/** The daemon's heartbeat: Claude Code, checkouts of the project's harness and repo, chat runs among its kinds. */
export async function chatHeartbeat(live: LiveWorker, runs: number[] = []): Promise<void> {
  await workerPost(live, "/v1/worker/heartbeat", {
    runtimes: { "claude-code": { available: true, version: "2.1.289", models: MODELS } },
    checkouts: Object.fromEntries([HARNESS_CHECKOUT, REPO].map((repo) => [`${live.project}/${repo}`, { path: `~/github/${repo}`, branch: "main" }])),
    free_slots: Math.max(live.worker.slots - runs.length, 0),
    runs,
    run_kinds: CHAT_RUN_KINDS,
    features: FEATURES,
  });
}

/** A worker of `account` serving `project` whose daemon takes chat runs: where a mention of theirs goes. */
export async function chatWorker(account: Account, project: string, name: string, slots = 1): Promise<LiveWorker> {
  const live = await liveWorker(account, project, name, slots, { report: { repos: [HARNESS_CHECKOUT, REPO], models: MODELS } });
  await chatHeartbeat(live);
  return live;
}

/** The daemon claims the chat run waiting for it, and its setup starts: the hub puts the agent's eyes on the message. */
export async function claimOnly(live: LiveWorker, runId: number): Promise<void> {
  const answer = (await workerPost(live, "/v1/worker/claim", { wait_s: 0 })) as { run: { id: number; kind: string } | null };
  if (answer.run?.id !== runId) throw new Error(`the worker claimed ${answer.run?.id ?? "nothing"}, not chat run #${runId}`);
  await chatHeartbeat(live, [runId]);
}

/**
 * A note of the daemon that starts a step of the run's setup (`chat_runs.SETUP_FIELD` of the API: checking, opening a
 * repo, starting): the thread shows that step under the message. `seq` is the daemon's number of the event, from 1.
 */
export async function setupNote(live: LiveWorker, runId: number, seq: number, setup: string, repo?: string): Promise<void> {
  const body = { text: `${setup} ${repo ?? ""}`.trim(), setup, ...(repo ? { repo } : {}) };
  await workerPost(live, `/v1/worker/runs/${runId}/events`, { events: [{ seq, at: new Date().toISOString(), kind: "system", body }] });
}

/** The agent started: the run is running. */
export async function agentStarts(live: LiveWorker, runId: number): Promise<void> {
  await workerPost(live, `/v1/worker/runs/${runId}/state`, { state: "running", session_id: "0199a3c1-0000-7000-8000-00000000c4a7" });
}

/** The daemon claims the chat run waiting for it and starts its agent: the hub puts the agent's eyes on the message. */
export async function claimChatRun(live: LiveWorker, runId: number): Promise<void> {
  await claimOnly(live, runId);
  await agentStarts(live, runId);
}

/** The agent's last message of a turn, its reply in the thread, while the run goes on to its next turn. */
export async function agentSays(live: LiveWorker, runId: number, text: string): Promise<void> {
  await workerPost(live, `/v1/worker/runs/${runId}/chat`, { text });
}

/** The end of the agent's turn: its last message, the reply in the thread, then the run done (the check mark). */
export async function agentAnswers(live: LiveWorker, runId: number, text: string): Promise<void> {
  await agentSays(live, runId, text);
  await workerPost(live, `/v1/worker/runs/${runId}/state`, { state: "verifying" });
  await workerPost(live, `/v1/worker/runs/${runId}/state`, { state: "done" });
}

/**
 * A chat run that asks its owner a decision (category triage, docs/workers.md, Failure triage): its message in the
 * thread, the decision, then the turn ends with the decision open and the run goes waiting.
 */
export async function agentAsks(live: LiveWorker, runId: number, text: string, question: string): Promise<number> {
  await workerPost(live, `/v1/worker/runs/${runId}/chat`, { text });
  const asked = (await workerPost(live, `/v1/worker/runs/${runId}/decisions`, {
    category: "triage",
    question,
    context: "The failure is timing related.",
    options: [
      { key: "retry", label: "Retry the flaky test" },
      { key: "skip", label: "Skip it" },
    ],
    recommended: "retry",
  })) as { id: number };
  await workerPost(live, `/v1/worker/runs/${runId}/state`, { state: "waiting" });
  return asked.id;
}

/** Someone who signed in to the hub once (so a chat can add them), with a grant on each of `projects`. */
export async function signedInPerson(prefix: string, projects: string[], maxLevel = "internal"): Promise<Account> {
  const account = newAccount(prefix);
  await machineToken(account);
  const admin = new HubAdmin();
  for (const project of projects) await admin.grant(project, account.login, "reader", maxLevel);
  return account;
}

/** `account`'s direct chat with agent `handle`, the one there is or a new one, as the web opens it. */
export async function openDirectByApi(account: Account, handle: string): Promise<Chat> {
  const api = await apiOf(account);
  return call(api.PUT("/v1/chats/direct/{handle}", { params: { path: { handle } } }));
}

/** Set a group chat's image, as a member does from its head. */
export async function putChatImageByApi(account: Account, chatId: number, image: Buffer): Promise<Chat> {
  const api = await apiOf(account);
  return call(api.PUT("/v1/chats/{chat_id}/image", { params: { path: { chat_id: chatId } }, body: { data: image.toString("base64") } }));
}

export async function createChatByApi(account: Account, body: { title: string; people?: string[]; agents?: string[] }): Promise<Chat> {
  const api = await apiOf(account);
  return call(api.POST("/v1/chats", { body: { people: [], agents: [], ...body } }));
}

export async function sendByApi(account: Account, chatId: number, body: string, replyTo: number | null = null): Promise<GroupMessage> {
  const api = await apiOf(account);
  return call(api.POST("/v1/chats/{chat_id}/messages", { params: { path: { chat_id: chatId } }, body: replyTo ? { body, reply_to: replyTo } : { body } }));
}

export async function reactByApi(account: Account, chatId: number, messageId: number, emoji: string): Promise<GroupMessage> {
  const api = await apiOf(account);
  return call(api.POST("/v1/chats/{chat_id}/messages/{message_id}/reactions", { params: { path: { chat_id: chatId, message_id: messageId } }, body: { emoji } }));
}

/** The chat and its latest messages, as `account` reads them. */
export async function chatPageOf(account: Account, chatId: number, thread?: number): Promise<ChatPage> {
  const api = await apiOf(account);
  return call(api.GET("/v1/chats/{chat_id}/messages", { params: { path: { chat_id: chatId }, query: thread ? { thread, limit: 200 } : { limit: 200 } } }));
}

export async function chatsOf(account: Account) {
  const api = await apiOf(account);
  return call(api.GET("/v1/chats"));
}

export function chatPath(id: number, message?: number): string {
  return message ? `/chats/${id}?message=${message}` : `/chats/${id}`;
}

/** The agent takes every message of the run's inbox, as the daemon hands them over: none wakes the session again. */
export async function takeInbox(live: LiveWorker, runId: number): Promise<void> {
  const { messages } = (await workerPost(live, `/v1/worker/runs/${runId}/inbox`, {})) as { messages: { id: number }[] };
  const last = messages.at(-1);
  if (last) await workerPost(live, `/v1/worker/runs/${runId}/inbox`, { ack: last.id });
}

/**
 * A session of @`handle` for `account` in a new chat, "Release talk" (docs/hub.md, Group chats): the mention of the
 * first of `asks`, then each other one as a reply in its thread, which goes on in the session as a new run once the
 * one before ended done. Every turn is claimed by `live` and takes its inbox; every one but the last answers and ends
 * done, the last runs on. The chat, and the runs' ids oldest first.
 */
export async function chatSession(account: Account, handle: string, live: LiveWorker, asks: string[]): Promise<{ chat: Chat; runs: number[] }> {
  const chat = await createChatByApi(account, { title: "Release talk", agents: [handle] });
  const runs: number[] = [];
  let root: number | null = null;
  for (const [index, ask] of asks.entries()) {
    const sent = await sendByApi(account, chat.id, `@${handle} ${ask}`, root);
    root ??= sent.id;
    const runId = sent.mentions[0]?.run_id;
    if (!runId) throw new Error(`the mention made no run: ${JSON.stringify(sent.mentions)}`);
    await claimChatRun(live, runId);
    await takeInbox(live, runId);
    if (index < asks.length - 1) await agentAnswers(live, runId, `Done: ${ask}`);
    runs.push(runId);
  }
  return { chat, runs };
}
