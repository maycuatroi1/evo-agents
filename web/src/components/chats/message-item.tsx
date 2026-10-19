"use client";

import { MessageSquareReply } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode, Ref } from "react";

import { SafeMarkdown } from "@/components/data/markdown";
import { Button } from "@/components/ui/button";
import type { Chat, ChatAuthor, GroupMessage } from "@/lib/chats";
import { cn } from "@/lib/utils";

import { AuthorAvatar, AuthorName } from "./author";
import { MessageLinks } from "./message-links";
import { MessageProgress } from "./message-progress";
import { ReactionChips, ReactionPicker } from "./reactions";

export type MessageActions = {
  /** Put the visitor's emoji on the message, or take it off. */
  onReact: (message: GroupMessage, emoji: string, on: boolean) => void;
  /** Open the thread the message starts. */
  onThread?: (root: number) => void;
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
  highlighted?: boolean;
  /** The element's id, unique in the page: the first message of a thread shows in the chat and in the thread. */
  domId?: string;
  ref?: Ref<HTMLLIElement>;
};

/**
 * One message of a chat (web/DESIGN.md, Chats): the author's face, name and time; the text as Markdown that is never
 * HTML; where it leads (the agent's run, the runs its mentions handed work to, its pull requests); how far each run it
 * handed work to is while that run is on its way (`message-progress.tsx`); its reactions; and
 * for a message that starts a thread, how many replies it has, who wrote them, and a way into it. React and Reply in
 * thread sit at its top right, shown while the pointer is over it or a key is in it, always on touch screens.
 */
export function MessageItem({ message, chat, me, thread, highlighted = false, domId, onReact, onThread, reacting, under, ref }: Props) {
  const t = useTranslations("chats.message");
  const format = useFormatter();
  const created = new Date(message.created_at);
  const busy = reacting === message.id;
  const toggle = (emoji: string, on: boolean) => onReact(message, emoji, on);
  const replies = thread?.count ?? 0;
  const id = domId ?? `message-${message.id}`;
  return (
    <li
      ref={ref}
      id={id}
      tabIndex={-1}
      aria-labelledby={`${id}-head`}
      data-testid="chat-message"
      data-message-id={message.id}
      data-author={message.author.kind}
      data-highlighted={highlighted || undefined}
      className={cn(
        "group/message relative flex gap-3 rounded-md px-2 py-2 outline-none transition-colors hover:bg-surface-hover/60 focus-visible:ring-2 focus-visible:ring-ring sm:px-3",
        highlighted && "bg-surface-selected ring-1 ring-brand/30 hover:bg-surface-selected",
      )}
    >
      <AuthorAvatar author={message.author} className="mt-0.5" />
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <p id={`${id}-head`} className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5 md:pr-16">
          <AuthorName author={message.author} me={me} />
          <time
            dateTime={message.created_at}
            title={format.dateTime(created, { dateStyle: "medium", timeStyle: "short" })}
            className="text-xs whitespace-nowrap text-fg-subtle tabular-nums"
          >
            {format.dateTime(created, { timeStyle: "short" })}
          </time>
        </p>
        <SafeMarkdown className="leading-6 [&_p]:my-0 [&_p+p]:mt-2" testId="message-body">
          {message.body}
        </SafeMarkdown>
        <MessageLinks message={message} chat={chat} />
        <MessageProgress progress={message.progress} />
        {under?.(message)}
        {/* The reactions, then React and Reply in thread: in the same row under 768 px; from 768 px those two sit over
            the message's top right corner, shown while the pointer is over it, a key is in it or its picker is open. */}
        <div className="flex flex-wrap items-center gap-1.5 md:contents">
          <ReactionChips reactions={message.reactions} me={me} busy={busy} onToggle={toggle} />
          <div
            className={cn(
              "-ml-1 flex items-center gap-0.5",
              "md:pointer-events-none md:absolute md:top-1.5 md:right-2 md:ml-0 md:rounded-sm md:border md:bg-popover md:p-0.5 md:opacity-0 md:shadow-popover md:transition-opacity",
              "md:group-focus-within/message:pointer-events-auto md:group-focus-within/message:opacity-100 md:group-hover/message:pointer-events-auto md:group-hover/message:opacity-100",
              "md:has-[[aria-expanded=true]]:pointer-events-auto md:has-[[aria-expanded=true]]:opacity-100",
            )}
            data-testid="message-actions"
          >
            <ReactionPicker reactions={message.reactions} busy={busy} onToggle={toggle} />
            {onThread && thread !== undefined ? (
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label={t("reply")}
                title={t("reply")}
                onClick={() => onThread(message.id)}
                data-testid="message-reply"
              >
                <MessageSquareReply aria-hidden="true" />
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

/** "3 replies": the faces of who wrote them and when the last came, opening the thread. */
function ThreadSummary({
  root,
  count,
  last,
  authors,
  onOpen,
}: {
  root: number;
  count: number;
  last: GroupMessage | null;
  authors: ChatAuthor[];
  onOpen: (root: number) => void;
}) {
  const t = useTranslations("chats.message");
  const format = useFormatter();
  return (
    <button
      type="button"
      onClick={() => onOpen(root)}
      className="-ml-1 inline-flex w-fit max-w-full cursor-pointer items-center gap-2 rounded-sm px-1 py-0.5 text-left text-xs transition-colors hover:bg-accent max-md:min-h-11"
      data-testid="thread-summary"
      data-replies={count}
    >
      <span className="flex -space-x-1.5" aria-hidden="true">
        {authors.slice(0, 3).map((author) => (
          <AuthorAvatar key={`${author.kind}:${author.login ?? author.handle ?? author.name}`} author={author} size="sm" className="ring-2 ring-card" />
        ))}
      </span>
      <span className="font-medium text-brand">{t("replies", { count })}</span>
      {last ? (
        <span className="truncate text-fg-subtle">{t("lastReply", { time: format.dateTime(new Date(last.created_at), { timeStyle: "short" }) })}</span>
      ) : null}
    </button>
  );
}
