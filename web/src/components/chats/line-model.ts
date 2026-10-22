import type { Chat, ChatAuthor, GroupMessage } from "@/lib/chats";

import { type Feed, mainMessages } from "./feed-model";

/**
 * The chat's line as the page draws it (web/DESIGN.md, Chats): which messages it shows, which ones run on from the one
 * above (the same author a moment later: no face and no name again), and in a direct chat which message an answer
 * answers when it does not sit right under it. Pure functions over the feed, which they never change.
 */

/** Messages of one author this close together (and on the same day) read as one run, the face and name shown once. */
export const RUN_ON_MS = 5 * 60_000;
/** Characters of the message a reply quotes, on its one line. */
export const QUOTE_CHARS = 120;

/** Who wrote a message, as one key: a person by login (any case), an agent by handle, the hub. */
export function authorKey(author: Pick<ChatAuthor, "kind" | "login" | "handle" | "name">): string {
  if (author.kind === "person") return `person:${(author.login ?? author.name).toLowerCase()}`;
  if (author.kind === "agent") return `agent:${author.handle ?? author.name}`;
  return author.kind;
}

/** The first message of the thread `message` is in: its own id when it starts one or stands alone. */
export const threadOf = (message: Pick<GroupMessage, "id" | "thread_root_id">) => message.thread_root_id ?? message.id;

const bySeq = (a: GroupMessage, b: GroupMessage) => a.seq - b.seq;

/**
 * What the chat itself shows, oldest first, from the oldest message its pages read. A group chat shows the messages
 * that are no reply (`mainMessages`: replies live in their thread); a direct chat shows every message, so the agent's
 * answers, which the hub posts in the thread of the message they answer, sit in the line under it.
 */
export function lineMessages(feed: Feed, kind: Chat["kind"]): GroupMessage[] {
  if (kind !== "direct") return mainMessages(feed);
  const from = feed.oldestSeq ?? 0;
  return [...feed.entries.values()]
    .map((entry) => entry.message)
    .filter((message) => message.seq >= from)
    .sort(bySeq);
}

/** `message` runs on from `before`: the same author, the same day (`sameDay`), within `RUN_ON_MS`. */
export function runsOn(before: GroupMessage | null | undefined, message: GroupMessage, sameDay: boolean): boolean {
  if (!before || !sameDay || authorKey(before.author) !== authorKey(message.author)) return false;
  const gap = Date.parse(message.created_at) - Date.parse(before.created_at);
  return gap >= 0 && gap <= RUN_ON_MS;
}

/** A reply of a direct chat that does not sit right under its thread: the message it answers, when the page holds it. */
export type Quote = { root: number; message: GroupMessage | null };

/**
 * The message each reply of a direct chat's `line` answers, for those whose message above is of another thread (two
 * tasks at once: an answer that came after the next question). It answers the latest message of its thread before it
 * by someone else (the agent answers the person's message, the person the agent's answer), else the thread's first;
 * null when the page holds neither. A reply right under its thread needs none: it reads as the answer already.
 */
export function quotes(line: readonly GroupMessage[], feed: Feed): Map<number, Quote> {
  const threads = new Map<number, GroupMessage[]>();
  for (const { message } of feed.entries.values()) {
    const root = threadOf(message);
    threads.set(root, [...(threads.get(root) ?? []), message]);
  }
  const found = new Map<number, Quote>();
  line.forEach((message, index) => {
    const root = message.thread_root_id;
    if (root === null) return;
    const above = line[index - 1];
    if (above && threadOf(above) === root) return;
    const earlier = (threads.get(root) ?? []).filter((item) => item.seq < message.seq).sort(bySeq);
    const author = authorKey(message.author);
    const answered = earlier.findLast((item) => authorKey(item.author) !== author) ?? earlier.find((item) => item.id === root) ?? null;
    found.set(message.id, { root, message: answered });
  });
  return found;
}

/**
 * `markdown` as one line of plain words, much as the list of chats previews a message (the hub's `chats.preview`): the
 * marks of headings, quotes, lists, rules, emphasis and code dropped, a link or an image as its text, at most `max`
 * characters with "…". Underscores inside a word stay, so `__init__.py` reads as written.
 */
export function preview(markdown: string, max = QUOTE_CHARS): string {
  const flat = markdown
    .replace(/^[ \t]*(?:`{3,}|~{3,})[^\n]*$/gm, " ")
    .replace(/^[ \t]*(?:[-*_][ \t]*){3,}$/gm, " ")
    .replace(/^[ \t]*(?:#{1,6}[ \t]+|>[ \t]?|[-*+][ \t]+(?:\[[ xX]\][ \t]+)?|\d{1,9}[.)][ \t]+)/gm, "")
    .replace(/!?\[([^\]\n]*)\]\([^)\n]*\)/g, "$1")
    .replace(/<((?:https?|mailto):[^>\s]+)>/g, "$1")
    .replace(/(`+)(.+?)\1/g, "$2")
    .replace(/(\*\*|~~)(\S(?:.*?\S)?)\1/g, "$2")
    .replace(/(^|[^\w*_])([*_])([^*_\s](?:[^*_]*?[^*_\s])?)\2(?![\w*_])/g, "$1$3")
    .replace(/\s+/g, " ")
    .trim();
  return flat.length <= max ? flat : `${flat.slice(0, max - 1).trimEnd()}…`;
}

/** One line of a message as `preview` makes it, or the names of the files it carries when it has no words. */
export function messagePreview(message: Pick<GroupMessage, "body" | "files">, max = QUOTE_CHARS): string {
  return preview(message.body, max) || preview((message.files ?? []).map((file) => file.name).join(", "), max);
}
