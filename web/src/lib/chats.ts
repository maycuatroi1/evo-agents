import type { Route } from "next";

import type { components } from "@/lib/api/schema";

/**
 * Group chats of the hub (docs/hub.md, Group chats; `evo_agents.hub.chats` of the API): people and the agents of
 * projects in one conversation. A message may answer another, which puts it in that message's thread (one level). An
 * `@name` in a person's message reaches a member of the chat: a person by login, an agent by handle, a project by its
 * name, which is the handle of its default agent; an agent reached that way gets the work, as a chat run. Reactions
 * are emoji a person puts, or that an agent's run puts by rule (eyes, check mark, cross). The hub decides everything
 * again on every write; what this module works out in the browser only shapes the page and spares a refusal.
 */
type Schemas = components["schemas"];
export type Chat = Schemas["Chat"];
export type ChatSummary = Schemas["ChatSummary"];
export type ChatPage = Schemas["ChatPage"];
export type GroupMessage = Schemas["GroupMessage"];
export type ChatAuthor = Schemas["ChatAuthor"];
export type ChatAgent = Schemas["ChatAgent"];
export type ChatPerson = Schemas["ChatPerson"];
export type ChatMention = Schemas["ChatMention"];
export type ChatReaction = Schemas["ChatReaction"];
export type ChatEvent = Schemas["ChatEvent"];
export type ChatEvents = Schemas["ChatEvents"];
export type ChatCreate = Schemas["ChatCreate"];

/** The API's limits (`chats.MAX_*`, `chat_runs.*`). */
export const MAX_TITLE_CHARS = 120;
export const MAX_MESSAGE_BYTES = 16 * 1024;
export const MAX_PEOPLE = 50;
export const MAX_AGENTS = 10;
export const MAX_EMOJI_KINDS = 20;
export const MAX_RUNS_PER_MESSAGE = 3;
export const MAX_BUDGET_USD = 1000;
export const BUDGET_HOURS = 24;
/** `chat_members.LOGIN_PATTERN`: a GitHub login. */
export const LOGIN = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}$/;
/** `runs.models.LINE`: one line of text, no control characters. */
export const ONE_LINE = /^[^\u0000-\u001f\u007f]+$/;

export { chatHref, chatsHref } from "@/lib/chat-keys";

/** A run's page, where a mention's work and an agent's message lead. */
export function chatRunHref(project: string, runId: number): Route {
  return `/p/${encodeURIComponent(project)}/runs/${runId}` as Route;
}

export function utf8Bytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

// Mentions, as `chats.mention_names` reads them.

const FENCE = /^[ \t]*(`{3,}|~{3,})[^\n]*\n[\s\S]*?(?:^[ \t]*\1[ \t]*$|(?![\s\S]))/gm;
const SPAN = /(?<!`)(`+)(?!`)[\s\S]+?(?<!`)\1(?!`)/g;
const MENTION = /(?<![\p{L}\p{N}_@./-])@([A-Za-z0-9][A-Za-z0-9-]{0,99})/gu;
const MAX_MENTIONS = 20;

const blank = (match: string) => " ".repeat(match.length);

/** The names `text` mentions, lowered, each once, in order, at most 20; code spans and fenced blocks left out. */
export function mentionNames(text: string): string[] {
  const plain = text.replace(FENCE, blank).replace(SPAN, blank);
  const names: string[] = [];
  for (const found of plain.matchAll(MENTION)) {
    const name = found[1].toLowerCase();
    if (!names.includes(name)) names.push(name);
    if (names.length === MAX_MENTIONS) break;
  }
  return names;
}

/** The `@name` being typed where the caret is: from the `@` to the caret, with what was typed after it. */
export type MentionQuery = { start: number; end: number; query: string };

const TYPING = /(?<![\p{L}\p{N}_@./-])@([A-Za-z0-9-]{0,100})$/u;

/** The mention the caret ends, when it stands right after `@` and the start of a name; null otherwise. */
export function mentionAt(text: string, caret: number): MentionQuery | null {
  const found = TYPING.exec(text.slice(0, caret));
  if (!found) return null;
  return { start: caret - found[0].length, end: caret, query: found[1].toLowerCase() };
}

/** `text` with the mention being typed replaced by `@name ` and where the caret goes after it. */
export function insertMention(text: string, at: MentionQuery, name: string): { text: string; caret: number } {
  const after = text.slice(at.end);
  const glue = after.startsWith(" ") ? "" : " ";
  const inserted = `@${name}${glue}`;
  return { text: text.slice(0, at.start) + inserted + after, caret: at.start + inserted.length + (glue ? 0 : 1) };
}

/** Whom an `@` may name in a chat: its agents (a project's default agent goes by the project's name) and its people. */
export type MentionTarget =
  | { kind: "agent"; name: string; label: string; agent: ChatAgent }
  | { kind: "project"; name: string; label: string; agent: ChatAgent }
  | { kind: "person"; name: string; label: string };

/** The members a mention reaches: the agents that take work (not deleted), then the people other than `me`. */
export function mentionTargets(chat: Pick<Chat, "agents" | "people">, me: string | null): MentionTarget[] {
  const agents = chat.agents.filter((agent) => !agent.deleted);
  const named = agents
    .filter((agent) => !agent.is_default)
    .sort((a, b) => a.handle.localeCompare(b.handle))
    .map((agent): MentionTarget => ({ kind: "agent", name: agent.handle, label: agent.name, agent }));
  const projects = agents
    .filter((agent) => agent.is_default)
    .sort((a, b) => a.handle.localeCompare(b.handle))
    .map((agent): MentionTarget => ({ kind: "project", name: agent.handle, label: agent.project, agent }));
  const people = chat.people
    .filter((person) => person.login.toLowerCase() !== me?.toLowerCase())
    .sort((a, b) => a.login.localeCompare(b.login))
    .map((person): MentionTarget => ({ kind: "person", name: person.login, label: person.login }));
  return [...named, ...projects, ...people];
}

/**
 * The targets whose name or label holds `query`, those that start with it first, in their kinds' order; at most
 * `limit`. An empty query keeps every target.
 */
export function suggestTargets(targets: readonly MentionTarget[], query: string, limit = 8): MentionTarget[] {
  const wanted = query.toLowerCase();
  const score = (target: MentionTarget) => {
    const name = target.name.toLowerCase();
    const label = target.label.toLowerCase();
    if (name.startsWith(wanted)) return 0;
    if (label.startsWith(wanted) || label.split(/\s+/).some((word) => word.startsWith(wanted))) return 1;
    if (name.includes(wanted) || label.includes(wanted)) return 2;
    return null;
  };
  return targets
    .map((target, index) => ({ target, index, rank: score(target) }))
    .filter((item): item is { target: MentionTarget; index: number; rank: number } => item.rank !== null)
    .sort((a, b) => a.rank - b.rank || a.index - b.index)
    .slice(0, limit)
    .map((item) => item.target);
}

/** The agents of the chat a person's message hands work to, in the order it names them (the hub takes the first 3). */
export function handedAgents(text: string, chat: Pick<Chat, "agents">): ChatAgent[] {
  const agents = new Map(chat.agents.filter((agent) => !agent.deleted).map((agent) => [agent.handle, agent]));
  return mentionNames(text).flatMap((name) => {
    const agent = agents.get(name);
    return agent ? [agent] : [];
  });
}

// Pull requests an agent's message links.

export type PullLink = { url: string; repo: string; number: number };

const PULL = /https:\/\/github\.com\/([A-Za-z0-9-]+\/[A-Za-z0-9._-]+)\/pull\/([0-9]+)/g;

/** The GitHub pull requests `body` links, each once, in order. */
export function pullLinks(body: string): PullLink[] {
  const seen = new Set<string>();
  const links: PullLink[] = [];
  for (const found of body.matchAll(PULL)) {
    const url = found[0];
    if (seen.has(url)) continue;
    seen.add(url);
    links.push({ url, repo: found[1], number: Number(found[2]) });
  }
  return links;
}

// The chat list.

/** The unread messages of every chat in the list. */
export function unreadTotal(chats: readonly Pick<ChatSummary, "unread">[]): number {
  return chats.reduce((sum, chat) => sum + chat.unread, 0);
}

/** The chats whose title or members hold `query`, ignoring case; every chat for an empty query. */
export function filterChats<T extends Pick<ChatSummary, "title" | "people" | "agents">>(chats: readonly T[], query: string): T[] {
  const wanted = query.trim().toLowerCase().replace(/^@/, "");
  if (!wanted) return [...chats];
  return chats.filter(
    (chat) =>
      chat.title.toLowerCase().includes(wanted) ||
      chat.people.some((login) => login.toLowerCase().includes(wanted)) ||
      chat.agents.some((handle) => handle.includes(wanted)),
  );
}

/** The logins of the people of the visitor's chats, other than `me` and those in `except`: whom to suggest adding. */
export function knownPeople(chats: readonly Pick<ChatSummary, "people">[], me: string | null, except: readonly string[] = []): string[] {
  const skip = new Set([...(me ? [me] : []), ...except].map((login) => login.toLowerCase()));
  const found = new Map<string, string>();
  for (const chat of chats) for (const login of chat.people) if (!skip.has(login.toLowerCase())) found.set(login.toLowerCase(), login);
  return [...found.values()].sort((a, b) => a.localeCompare(b));
}

/** Why a title is not one the hub takes; null when it is. */
export function titleProblem(title: string): "blank" | "long" | "line" | null {
  if (!title.trim()) return "blank";
  if ([...title].length > MAX_TITLE_CHARS) return "long";
  return ONE_LINE.test(title) ? null : "line";
}
