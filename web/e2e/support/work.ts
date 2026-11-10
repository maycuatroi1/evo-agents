import type { components } from "../../src/lib/api/schema";

import { createAgentByApi } from "./agents";
import { agentAnswers, agentAsks, chatsOf, chatWorker, claimChatRun, createChatByApi, sendByApi, takeInbox } from "./chats";
import { API_URL } from "./env";
import { type Account, uniqueName } from "./hub";
import type { LiveWorker } from "./runs";
import { chainSpec, putSystemByApi } from "./systems";
import { PROTOCOL_HEADER } from "./workers";

/**
 * The work chats of an agent system through the real API (docs/hub.md, Work chats and The work card): a system whose
 * entry agent is @lead and who asks @helper, each agent with a worker of its own pool. The person's mention of @lead
 * opens a trace; the specs are the fake workers, so @lead sets the work and asks @helper as the hub runtime would, and
 * @helper asks the person a decision. No model is asked anything.
 */
export type WorkCard = components["schemas"]["WorkCard"];

export type WorkSystem = {
  project: string;
  slug: string;
  name: string;
  lead: string;
  helper: string;
  leadBox: LiveWorker;
  helperBox: LiveWorker;
};

export type SeededWork = {
  /** The chat the person asked in, and the message that opened the trace there (the root thread). */
  rootChat: number;
  root: number;
  /** The work chat the hand-off opened. */
  workChat: number;
  traceId: number;
  title: string;
  leadRun: number;
  helperRun: number;
  /** The decision @helper asked the person, open; null when it asked none. */
  decision: number | null;
};

async function workerPost<T>(live: LiveWorker, path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${live.token}`,
      ...PROTOCOL_HEADER,
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`POST ${path}: ${response.status} ${await response.text()}`);
  return (await response.json()) as T;
}

/** System "Northwind desk" of `project`: @lead, its entry agent, talks to @helper; a worker in each agent's pool. */
export async function workSystem(admin: Account, project: string): Promise<WorkSystem> {
  const lead = uniqueName("lead");
  const helper = uniqueName("helper");
  await createAgentByApi(admin, project, {
    handle: lead,
    name: "Lumen lead",
    color: "#2563eb",
    permissions: {},
  });
  await createAgentByApi(admin, project, {
    handle: helper,
    name: "Lumen helper",
    color: "#16a34a",
    permissions: {},
  });
  const slug = uniqueName("desk");
  const name = "Northwind desk";
  await putSystemByApi(
    admin,
    project,
    slug,
    chainSpec(name, [
      { handle: lead, version: 1 },
      { handle: helper, version: 1 },
    ]),
  );
  const leadBox = await chatWorker(admin, project, uniqueName("lead-box"), 4, lead);
  const helperBox = await chatWorker(admin, project, uniqueName("helper-box"), 4, helper);
  return { project, slug, name, lead, helper, leadBox, helperBox };
}

/**
 * A work of `system` for `person`: their mention of @lead in a new chat, @lead's run sets the work and asks @helper
 * (which opens the work chat), and @helper's run starts and, with `decide`, asks the person a decision and waits.
 */
export async function seedWork(person: Account, system: WorkSystem, title: string, { decide = true }: { decide?: boolean } = {}): Promise<SeededWork> {
  const chat = await createChatByApi(person, {
    title: `Ask ${title}`,
    agents: [system.lead],
  });
  const root = await sendByApi(person, chat.id, `@${system.lead} ${title.toLowerCase()} for lumenops.example`);
  const leadRun = root.mentions[0]?.run_id;
  if (!leadRun) throw new Error(`the mention made no run: ${JSON.stringify(root.mentions)}`);
  await claimChatRun(system.leadBox, leadRun);
  await takeInbox(system.leadBox, leadRun);
  const asked = await workerPost<{
    run_id: number | null;
    refusal: string | null;
  }>(system.leadBox, `/v1/worker/runs/${leadRun}/agent/asks`, {
    handle: system.helper,
    body: "Please draft the refund replies for the open tickets.",
    goal: "Draft the refund replies",
  });
  if (!asked.run_id) throw new Error(`the ask made no run: ${asked.refusal}`);
  const card = await workerPost<WorkCard>(system.leadBox, `/v1/worker/runs/${leadRun}/work`, {
    title,
    goal: "Every refund ticket of Northwind has a reply.",
    done_when: "No refund ticket is left without a reply.",
  });
  if (card.work_chat_id === null) throw new Error("the hand-off opened no work chat");
  await claimChatRun(system.helperBox, asked.run_id);
  await takeInbox(system.helperBox, asked.run_id);
  const decision = !decide
    ? null
    : await agentAsks(system.helperBox, asked.run_id, "Two tickets ask for more than the policy allows.", "Refund the two tickets above the policy limit?");
  return {
    rootChat: chat.id,
    root: root.id,
    workChat: card.work_chat_id,
    traceId: card.trace_id,
    title,
    leadRun,
    helperRun: asked.run_id,
    decision,
  };
}

/** The work ends: @helper answers and its run ends done, then @lead's (a work seeded without a decision). */
export async function finishWork(system: WorkSystem, work: SeededWork): Promise<void> {
  await agentAnswers(system.helperBox, work.helperRun, "The refund replies are drafted.");
  await agentAnswers(system.leadBox, work.leadRun, "Every refund ticket has a reply.");
}

/** The work as `person`'s list of chats says it: null until the list carries it. */
export async function listedWork(person: Account, workChat: number) {
  return (await chatsOf(person)).find((chat) => chat.id === workChat)?.work ?? null;
}
