import type { ChatAuthor, ChatEvent, ChatImageChange, ChatPage, ChatProgress, ChatReaction, GroupMessage } from "@/lib/chats";

/**
 * A chat's messages as the page holds them: what the first page, older pages and threads read, kept current by the
 * chat's events (its stream, or reads of events while the stream cannot be used).
 *
 * Each read answers "as of" the chat's last event then (`chat.last_seq`), so an event at or before that seq is already
 * in what it read: a reaction event carries the count after the change, which makes applying it again harmless, and
 * a message already held is never replaced by its event. A thread's reply count is the count a read gave (`base`, as
 * of `asOf`) plus the replies the page has seen since; a message that arrived as an event starts at none, since its
 * replies all come after it.
 */
export type FeedEntry = { message: GroupMessage; base: number; asOf: number };

export type Feed = {
  entries: ReadonlyMap<number, FeedEntry>;
  /** The replies the page has seen, by the first message of their thread: reply id to its seq. */
  replies: ReadonlyMap<number, ReadonlyMap<number, number>>;
  /** The seq of the last event applied: where a read of events goes on. */
  lastSeq: number;
  /** The seq of the oldest message of the chat read, which an older page is read before; null before any. */
  oldestSeq: number | null;
  /** The chat has messages before the oldest read. */
  more: boolean;
};

export const EMPTY_FEED: Feed = { entries: new Map(), replies: new Map(), lastSeq: 0, oldestSeq: null, more: false };

function withReply(replies: ReadonlyMap<number, ReadonlyMap<number, number>>, message: GroupMessage) {
  const root = message.thread_root_id;
  if (root === null) return replies;
  const known = replies.get(root);
  if (known?.has(message.id)) return replies;
  const next = new Map(replies);
  next.set(root, new Map(known ?? []).set(message.id, message.seq));
  return next;
}

/** `feed` with what a read of messages answered: the messages it did not hold, as of the chat's last event then. */
export function readInto(feed: Feed, page: ChatPage, scope: "latest" | "older" | "thread"): Feed {
  const asOf = page.chat.last_seq;
  const entries = new Map(feed.entries);
  let replies = feed.replies;
  for (const message of page.messages) {
    const held = entries.get(message.id);
    // A thread read counts its first message's replies later than the read that brought it: take the newer count.
    if (!held || (scope === "thread" && message.thread_root_id === null && held.asOf < asOf)) {
      entries.set(message.id, { message, base: message.reply_count, asOf });
    }
    replies = withReply(replies, message);
  }
  const first = page.messages.length ? page.messages[0].seq : null;
  const main = scope !== "thread";
  return {
    entries,
    replies,
    lastSeq: scope === "latest" ? Math.max(feed.lastSeq, asOf) : feed.lastSeq,
    oldestSeq: main && first !== null ? Math.min(first, feed.oldestSeq ?? first) : feed.oldestSeq,
    more: main ? page.more : feed.more,
  };
}

/** The feed a chat's page opens with. */
export function feedOf(page: ChatPage): Feed {
  return readInto(EMPTY_FEED, page, "latest");
}

/** `feed` with a message it did not hold, sent by the visitor or brought by an event. */
export function withMessage(feed: Feed, message: GroupMessage): Feed {
  if (feed.entries.has(message.id)) return feed;
  const entries = new Map(feed.entries).set(message.id, { message, base: 0, asOf: message.seq });
  return { ...feed, entries, replies: withReply(feed.replies, message) };
}

const isMe = (by: ChatAuthor, me: string | null) => by.kind === "person" && !!me && by.login?.toLowerCase() === me.toLowerCase();

/** `names` with `name` in it (`added`) or left out; unchanged for no name. */
function toggled(names: readonly string[], name: string | null, added: boolean): string[] {
  if (!name) return [...names];
  if (!added) return names.filter((item) => item !== name);
  return names.includes(name) ? [...names] : [...names, name];
}

/** A message's reactions after an event put an emoji on it (`added`) or took one off. */
export function reacted(
  reactions: readonly ChatReaction[],
  change: { emoji: string; by: ChatAuthor; count: number },
  added: boolean,
  me: string | null,
): ChatReaction[] {
  const person = change.by.kind === "person" ? (change.by.login ?? null) : null;
  const agent = change.by.kind === "agent" ? (change.by.handle ?? null) : null;
  const index = reactions.findIndex((reaction) => reaction.emoji === change.emoji);
  const mine = isMe(change.by, me);
  if (index < 0) {
    if (!added || change.count <= 0) return [...reactions];
    return [...reactions, { emoji: change.emoji, count: change.count, mine, people: toggled([], person, true), agents: toggled([], agent, true) }];
  }
  if (change.count <= 0) return reactions.filter((_, at) => at !== index);
  const current = reactions[index];
  const next: ChatReaction = {
    ...current,
    count: change.count,
    people: toggled(current.people, person, added),
    agents: toggled(current.agents, agent, added),
    mine: mine ? added : current.mine,
  };
  return reactions.map((reaction, at) => (at === index ? next : reaction));
}

/** `feed` with the reactions a write of the visitor answered for one message. */
export function withReactions(feed: Feed, message: GroupMessage): Feed {
  const held = feed.entries.get(message.id);
  if (!held) return feed;
  const entries = new Map(feed.entries).set(message.id, { ...held, message: { ...held.message, reactions: message.reactions } });
  return { ...feed, entries };
}

const sameStep = (a: ChatProgress, b: ChatProgress) =>
  a.run_id === b.run_id && a.step === b.step && a.worker === b.worker && (a.repo ?? null) === (b.repo ?? null) && a.since === b.since;

/**
 * `feed` with the progress the chat's stream (or a read of its events) sent: that of every message of the chat whose
 * agent's run is on its way, so a message held that it leaves out has none now. A message read or brought after it
 * carries its own. Only the messages whose progress changed are replaced.
 */
export function withProgress(feed: Feed, progress: readonly ChatProgress[]): Feed {
  const byMessage = new Map<number, ChatProgress[]>();
  for (const item of progress) byMessage.set(item.message_id, [...(byMessage.get(item.message_id) ?? []), item]);
  let entries: Map<number, FeedEntry> | null = null;
  for (const [id, entry] of feed.entries) {
    const held = entry.message.progress ?? [];
    const next = byMessage.get(id) ?? [];
    if (held.length === next.length && held.every((item, at) => sameStep(item, next[at]))) continue;
    entries ??= new Map(feed.entries);
    entries.set(id, { ...entry, message: { ...entry.message, progress: next } });
  }
  return entries ? { ...feed, entries } : feed;
}

/** What applying events changed beyond the messages: whether a member was added or removed, and the chat's image now. */
export type Applied = { feed: Feed; members: boolean; image: ChatImageChange | null };

/** `feed` with the chat's events after its last one; an event at or before it is a repeat and changes nothing. */
export function applyEvents(feed: Feed, events: readonly ChatEvent[], me: string | null): Applied {
  let next = feed;
  let members = false;
  let image: ChatImageChange | null = null;
  for (const event of events) {
    if (event.seq <= next.lastSeq) continue;
    next = { ...next, lastSeq: event.seq };
    if (event.kind === "message" && event.message) {
      next = withMessage(next, event.message);
    } else if ((event.kind === "reaction_added" || event.kind === "reaction_removed") && event.reaction) {
      const change = event.reaction;
      const held = next.entries.get(change.message_id);
      if (!held) continue;
      const reactions = reacted(held.message.reactions, change, event.kind === "reaction_added", me);
      const entries = new Map(next.entries).set(change.message_id, { ...held, message: { ...held.message, reactions } });
      next = { ...next, entries };
    } else if (event.kind === "member_added" || event.kind === "member_removed") {
      members = true;
    } else if (event.kind === "image_changed" && event.image) {
      image = event.image; // the latest of the batch is the image now
    }
  }
  return { feed: next, members, image };
}

const bySeq = (a: GroupMessage, b: GroupMessage) => a.seq - b.seq;

/**
 * The messages that are no reply, oldest first, from the oldest the chat's pages read: what the chat itself shows. An
 * older message read only for its thread (a link to it) shows in the thread, not out of place above the others.
 */
export function mainMessages(feed: Feed): GroupMessage[] {
  const from = feed.oldestSeq ?? 0;
  return [...feed.entries.values()]
    .map((entry) => entry.message)
    .filter((message) => message.thread_root_id === null && message.seq >= from)
    .sort(bySeq);
}

/** A thread: its first message (when held) and its replies, oldest first. */
export function threadMessages(feed: Feed, root: number): GroupMessage[] {
  return [...feed.entries.values()]
    .map((entry) => entry.message)
    .filter((message) => message.id === root || message.thread_root_id === root)
    .sort(bySeq);
}

/** The replies of the thread `root` starts: the count a read gave, and those seen since. */
export function replyCount(feed: Feed, root: number): number {
  const entry = feed.entries.get(root);
  const seen = feed.replies.get(root);
  if (!entry) return seen?.size ?? 0;
  let later = 0;
  for (const seq of seen?.values() ?? []) if (seq > entry.asOf) later += 1;
  return entry.base + later;
}

/** The latest reply held of the thread `root` starts, and who wrote the replies held (each once, the latest first). */
export function threadTail(feed: Feed, root: number): { last: GroupMessage | null; authors: ChatAuthor[] } {
  const replies = threadMessages(feed, root).filter((message) => message.id !== root);
  const authors = new Map<string, ChatAuthor>();
  for (const reply of [...replies].reverse()) {
    const key = `${reply.author.kind}:${reply.author.login ?? reply.author.handle ?? reply.author.name}`;
    if (!authors.has(key)) authors.set(key, reply.author);
  }
  return { last: replies.at(-1) ?? null, authors: [...authors.values()] };
}
