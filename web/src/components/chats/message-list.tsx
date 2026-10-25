"use client";

import { ArrowDown, History, MessagesSquare } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { Fragment, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import type { Chat, GroupMessage } from "@/lib/chats";

import { type Feed, replyCount, threadTail } from "./feed-model";
import { continuesRun, lineMessages, quotes, runsOn } from "./line-model";
import { type MessageActions, MessageItem } from "./message-item";
import { useFollow } from "./use-follow";

type Props = MessageActions & {
  chat: Chat;
  me: string | null;
  feed: Feed;
  /** The message a link points at: shown in view and marked once. */
  highlight: number | null;
  loadOlder: () => Promise<void>;
};

/**
 * The chat's line, oldest first, under a line for each day (web/DESIGN.md, Chats): in a group chat the messages that
 * are no reply, each with its thread's summary; in a direct chat every message, the agent's answers under what they
 * answer, a reply quoting what it answers when another message came between. Messages of one author a moment apart
 * run on under one face and name. A `role="log"` region (polite) that scrolls on its own and opens at the newest
 * message; it follows new ones while the visitor reads at the end, and offers Jump to the latest when they scrolled
 * up. Earlier messages load on demand, a page at a time.
 */
export function MessageList({ chat, me, feed, highlight, loadOlder, onReact, onThread, onReply, onQuote, reacting, under }: Props) {
  const t = useTranslations("chats.messages");
  const format = useFormatter();
  const direct = chat.kind === "direct";
  const messages = lineMessages(feed, chat.kind);
  const quoted = direct ? quotes(messages, feed) : null;
  const [older, setOlder] = useState<"idle" | "loading" | "failed">("idle");
  const { region, behind, toEnd } = useFollow(messages.length, messages[0]?.id ?? null, feed.lastSeq);

  useEffect(() => {
    if (highlight === null) return;
    const element = document.getElementById(`message-${highlight}`);
    if (!element) return;
    element.scrollIntoView({ block: "center" });
    element.focus({ preventScroll: true });
  }, [highlight]);

  const earlier = async () => {
    setOlder("loading");
    try {
      await loadOlder();
      setOlder("idle");
    } catch {
      setOlder("failed");
    }
  };

  const dayOf = (message: GroupMessage) => format.dateTime(new Date(message.created_at), { year: "numeric", month: "2-digit", day: "2-digit" });
  const agent = chat.agents[0]?.name ?? chat.title;
  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={region}
        role="log"
        aria-live="polite"
        aria-label={t("log", { title: chat.title })}
        tabIndex={0}
        className="h-full overflow-y-auto overscroll-contain px-2 pt-1 pb-3 outline-none [overflow-anchor:none] focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset sm:px-3"
        data-testid="chat-log"
      >
        {feed.more ? (
          <div className="flex flex-col items-center gap-1.5 pt-2 pb-1">
            <Button type="button" variant="outline" size="sm" busy={older === "loading"} onClick={() => void earlier()} data-testid="chat-older">
              <History aria-hidden="true" />
              {older === "loading" ? t("loadingOlder") : t("older")}
            </Button>
            {older === "failed" ? (
              <p role="alert" className="text-xs text-danger" data-testid="chat-older-error">
                {t("olderFailed")}
              </p>
            ) : null}
          </div>
        ) : null}
        {messages.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 px-6 py-10 text-center" data-testid="chat-empty">
            <MessagesSquare className="size-5 text-muted-foreground" aria-hidden="true" />
            <p className="text-sm font-medium text-foreground">{t("emptyTitle")}</p>
            <p className="max-w-md text-[13px] leading-[18px] text-pretty text-muted-foreground">{direct ? t("directEmptyBody", { name: agent }) : t("emptyBody")}</p>
          </div>
        ) : (
          <ol className="flex flex-col" data-testid="chat-messages">
            {messages.map((message, index) => {
              const before = messages[index - 1];
              const day = dayOf(message);
              const newDay = !before || dayOf(before) !== day;
              const quote = quoted?.get(message.id) ?? null;
              return (
                <Fragment key={message.id}>
                  {newDay ? (
                    <li className="flex items-center gap-3 px-3 pt-4 pb-1 text-xs font-medium text-fg-subtle" data-testid="chat-day">
                      <span aria-hidden="true" className="h-px flex-1 bg-border" />
                      <time dateTime={message.created_at}>{format.dateTime(new Date(message.created_at), { dateStyle: "full" })}</time>
                      <span aria-hidden="true" className="h-px flex-1 bg-border" />
                    </li>
                  ) : null}
                  <MessageItem
                    message={message}
                    chat={chat}
                    me={me}
                    thread={direct ? undefined : { count: replyCount(feed, message.id), ...threadTail(feed, message.id) }}
                    runOn={!quote && runsOn(before, message, !newDay)}
                    sameRun={continuesRun(before, message)}
                    quote={quote}
                    highlighted={highlight === message.id}
                    onReact={onReact}
                    onThread={direct ? undefined : onThread}
                    onReply={direct ? onReply : undefined}
                    onQuote={direct ? onQuote : undefined}
                    reacting={reacting}
                    under={under}
                  />
                </Fragment>
              );
            })}
          </ol>
        )}
      </div>
      {behind ? (
        <Button type="button" variant="outline" size="sm" className="absolute bottom-3 left-1/2 -translate-x-1/2 shadow-popover" onClick={toEnd} data-testid="chat-latest">
          <ArrowDown aria-hidden="true" />
          {t("latest")}
        </Button>
      ) : null}
    </div>
  );
}
