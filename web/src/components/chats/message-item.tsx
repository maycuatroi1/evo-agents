"use client";

import { MessageSquareReply, Reply } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode, Ref } from "react";

import { SafeMarkdown } from "@/components/data/markdown";
import { Button } from "@/components/ui/button";
import { type Chat, type ChatAuthor, type GroupMessage, isMine } from "@/lib/chats";
import { cn } from "@/lib/utils";

import { AuthorAvatar } from "./faces";
import type { Quote } from "./line-model";
import { MessageHead, ReplyQuote, ThreadSummary } from "./message-parts";
import { MessageLinks } from "./message-links";
import { MessageProgress } from "./message-progress";
import { ReactionChips, ReactionPicker } from "./reactions";

export type MessageActions = {
  /** Put the visitor's emoji on the message, or take it off. */
  onReact: (message: GroupMessage, emoji: string, on: boolean) => void;
  /** Open the thread the message starts (a group chat). */
  onThread?: (root: number) => void;
  /** Answer the message from the chat's composer, in its thread (a direct chat): the agent's session goes on. */
  onReply?: (message: GroupMessage) => void;
  /** Show the message a reply quotes: in the line, or its thread when the page does not hold it. */
  onQuote?: (quote: Quote) => void;
  /** The message whose reaction the hub is writing now. */
  reacting: number | null;
  /** What the page puts under a message (the decisions its agent's run asked): the chat knows nothing of it. */
  under?: MessageSlot;
};

export type MessageSlot = (message: GroupMessage) => ReactNode;

type Props = MessageActions & {
  message: GroupMessage;
  chat: Pick<Chat, "agents">;
  me: string | null;
  /** The replies of the thread it starts, and the latest one held: what its summary says. Left out in a thread. */
  thread?: { count: number; last: GroupMessage | null; authors: ChatAuthor[] } | null;
  /** The message above is by the same author a moment before: no face and no name again. */
  runOn?: boolean;
  /** In a direct chat, what an answer answers when it does not sit right under it. */
  quote?: Quote | null;
  highlighted?: boolean;
  /** The element's id, unique in the page: the first message of a thread shows in the chat and in the thread. */
  domId?: string;
  ref?: Ref<HTMLLIElement>;
};

/** The side a message takes: the visitor's own on the right, anyone else's on the left after their face. */
const COLUMN = {
  mine: "items-end max-w-[85%] md:max-w-[min(42rem,calc(100%-4.5rem))]",
  theirs: "items-start max-w-[calc(100%-2.5rem)] md:max-w-[min(42rem,calc(100%-7rem))]",
};
/** React and Reply from 768 px: beside the message at its top, on the side away from its face, while it has the pointer or a key. */
const ACTIONS = {
  mine: "md:right-full md:mr-1",
  theirs: "md:left-full md:ml-1",
};

/**
 * One message of a chat as a chat app draws it (web/DESIGN.md, Chats): the visitor's own on the right in a `brand-soft`
 * bubble with no face; anyone else's on the left in a `muted` bubble after their face and name, shown once for messages
 * that run on. The text is Markdown that is never HTML; under the bubble, where it leads (the agent's run, the runs
 * its mentions handed work to, its pull requests), how far each run it handed work to is (`message-progress.tsx`), what
 * the page puts under it, its reactions, and for a message that starts a thread, how many replies it has and a way
 * into it. In a direct chat a reply that does not sit right under what it answers quotes it above its bubble. React and
 * Reply sit beside it from 768 px, shown while the pointer is over it or a key is in it, and under it on phones.
 */
export function MessageItem(props: Props) {
  const { message, chat, me, thread, runOn = false, quote = null, highlighted = false, domId, onReact, onThread, onReply, onQuote, reacting, under, ref } = props;
  const t = useTranslations("chats.message");
  const format = useFormatter();
  const mine = isMine(message.author, me);
  const side = mine ? "mine" : "theirs";
  const busy = reacting === message.id;
  const toggle = (emoji: string, on: boolean) => onReact(message, emoji, on);
  const replies = thread?.count ?? 0;
  const id = domId ?? `message-${message.id}`;
  const reply = onReply
    ? { label: t("replyDirect"), icon: <Reply aria-hidden="true" />, act: () => onReply(message) }
    : onThread && thread !== undefined
      ? { label: t("reply"), icon: <MessageSquareReply aria-hidden="true" />, act: () => onThread(message.id) }
      : null;
  return (
    <li
      ref={ref}
      id={id}
      tabIndex={-1}
      aria-labelledby={`${id}-head`}
      data-testid="chat-message"
      data-message-id={message.id}
      data-author={message.author.kind}
      data-mine={mine}
      data-run-on={runOn}
      data-highlighted={highlighted || undefined}
      className={cn(
        "group/message flex gap-2 rounded-md px-2 outline-none focus-visible:ring-2 focus-visible:ring-ring sm:px-3",
        mine && "justify-end",
        runOn ? "pt-0.5" : "pt-3",
      )}
    >
      {mine ? null : runOn ? <span className="w-8 shrink-0" aria-hidden="true" /> : <AuthorAvatar author={message.author} className="mt-0.5" />}
      <div className={cn("relative flex min-w-0 flex-col gap-1", COLUMN[side])}>
        <MessageHead id={`${id}-head`} message={message} me={me} mine={mine} runOn={runOn} />
        {quote && onQuote ? <ReplyQuote quote={quote} me={me} onShow={onQuote} /> : null}
        <div
          className={cn(
            "max-w-full min-w-0 rounded-lg px-3 py-2 transition-shadow",
            mine ? "bg-brand-soft" : "bg-muted",
            !runOn && (mine ? "rounded-tr-xs" : "rounded-tl-xs"),
            highlighted && "ring-2 ring-brand",
          )}
          title={runOn ? format.dateTime(new Date(message.created_at), { dateStyle: "medium", timeStyle: "short" }) : undefined}
          data-testid="message-bubble"
        >
          <SafeMarkdown
            className="leading-6 [&_:not(pre)>code]:bg-card [&_p]:my-0 [&_p+p]:mt-2 [&_pre]:my-2 [&_pre]:bg-card"
            testId="message-body"
          >
            {message.body}
          </SafeMarkdown>
        </div>
        <MessageLinks message={message} chat={chat} />
        <MessageProgress progress={message.progress} />
        {under?.(message)}
        {/* The reactions, then React and Reply: in the same row under 768 px; from 768 px those two sit beside the
            message at its top, shown while the pointer is over it, a key is in it or its picker is open. */}
        <div className={cn("flex flex-wrap items-center gap-1.5 md:contents", mine && "justify-end")}>
          <ReactionChips reactions={message.reactions} me={me} busy={busy} onToggle={toggle} />
          <div
            className={cn(
              "flex items-center gap-0.5",
              "md:pointer-events-none md:absolute md:top-0 md:rounded-sm md:border md:bg-popover md:p-0.5 md:opacity-0 md:shadow-popover md:transition-opacity",
              "md:group-focus-within/message:pointer-events-auto md:group-focus-within/message:opacity-100 md:group-hover/message:pointer-events-auto md:group-hover/message:opacity-100",
              "md:has-[[aria-expanded=true]]:pointer-events-auto md:has-[[aria-expanded=true]]:opacity-100",
              ACTIONS[side],
            )}
            data-testid="message-actions"
          >
            <ReactionPicker reactions={message.reactions} busy={busy} onToggle={toggle} />
            {reply ? (
              <Button type="button" variant="ghost" size="icon-sm" aria-label={reply.label} title={reply.label} onClick={reply.act} data-testid="message-reply">
                {reply.icon}
              </Button>
            ) : null}
          </div>
        </div>
        {thread && replies > 0 && onThread ? (
          <ThreadSummary root={message.id} count={replies} last={thread.last} authors={thread.authors} onOpen={onThread} />
        ) : null}
      </div>
    </li>
  );
}
