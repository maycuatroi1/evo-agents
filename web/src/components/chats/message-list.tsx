"use client";

import { ArrowDown, History, MessagesSquare } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { Fragment, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import type { Chat, GroupMessage } from "@/lib/chats";

import { type Feed, replyCount, threadTail } from "./feed-model";
import { useHomeTask } from "./home-task-context";
import { lineMessages, quotes, runsOn } from "./line-model";
import { type MessageActions, MessageItem, READING_COLUMN } from "./message-item";
import { TaskCard } from "./task-card";
import { type DirectItem, directTaskLine } from "./task-model";
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
 * up. Earlier messages load on demand, a page at a time. The messages sit in the reading column (`READING_COLUMN`),
 * centred in the log however wide the window is. On Home (`HomeTaskContext`) a direct chat's thread that opened work
 * keeps the person's messages, one task card a task (`TaskCard`, which opens it beside the rail) and the coordinator's
 * answer once the work ended: the rest is the task's (`directTaskLine`).
 */
export function MessageList({ chat, me, feed, highlight, loadOlder, onReact, onThread, onReply, onQuote, reacting, under }: Props) {
  const t = useTranslations("chats.messages");
  const format = useFormatter();
  const direct = chat.kind === "direct";
  const openTask = useHomeTask();
  const all = lineMessages(feed, chat.kind);
  const items: DirectItem[] = direct && openTask ? directTaskLine(all) : all.map((message) => ({ kind: "message", message }));
  const messages = items.map((item) => item.message);
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
            {direct ? <p className="max-w-md text-[13px] leading-[18px] text-pretty text-muted-foreground">{t("directEmptyBody", { name: agent })}</p> : null}
          </div>
        ) : (
          <ol className={`flex flex-col ${READING_COLUMN}`} data-testid="chat-messages">
            {messages.map((message, index) => {
              const before = messages[index - 1];
              const day = dayOf(message);
              const newDay = !before || dayOf(before) !== day;
              const quote = quoted?.get(message.id) ?? null;
              const item = items[index];
              return (
                <Fragment key={message.id}>
                  {newDay ? (
                    <li className="flex items-center gap-3 px-3 pt-4 pb-1 text-xs font-medium text-fg-subtle" data-testid="chat-day">
                      <span aria-hidden="true" className="h-px flex-1 bg-border" />
                      <time dateTime={message.created_at}>{format.dateTime(new Date(message.created_at), { dateStyle: "full" })}</time>
                      <span aria-hidden="true" className="h-px flex-1 bg-border" />
                    </li>
                  ) : null}
                  {item.kind === "task" && openTask ? (
                    <li className="flex px-3 py-1.5 sm:pl-[3.25rem]" data-testid="chat-task">
                      <TaskCard work={item.work} onOpen={openTask} />
                    </li>
                  ) : (
                    <MessageItem
                      message={message}
                      chat={chat}
                      me={me}
                      thread={direct ? undefined : { count: replyCount(feed, message.id), ...threadTail(feed, message.id) }}
                      runOn={!quote && runsOn(before, message, !newDay)}
                      quote={quote}
                      highlighted={highlight === message.id}
                      onReact={onReact}
                      onThread={direct ? undefined : onThread}
                      onReply={direct ? onReply : undefined}
                      onQuote={direct ? onQuote : undefined}
                      reacting={reacting}
                      under={under}
                    />
                  )}
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
