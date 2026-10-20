"use client";

import Link from "next/link";
import { type DateTimeFormatOptions, useFormatter, useTimeZone, useTranslations } from "next-intl";
import { useId, useSyncExternalStore } from "react";

import { chatHref, type ChatLastMessage, chatName, type ChatSummary, isMine, listTime, type ListTime, summaryFace } from "@/lib/chats";
import { cn } from "@/lib/utils";

import { ChatAvatar } from "./chat-avatar";

// The browser's clock to the minute, for "today" and "this week" in the rows: null on the server and while the page
// hydrates (the rows say the date then, as both renders agree), then a reading a minute. One timer serves every row.
let minute = 0;
let timer: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) {
    minute = Date.now();
    timer = setInterval(() => {
      minute = Date.now();
      for (const notify of listeners) notify();
    }, 60_000);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

const reading = () => minute || (minute = Date.now());
const none = () => null;

/** The browser's clock, read once a minute; null on the server and during hydration. */
export function useMinuteClock(): number | null {
  return useSyncExternalStore(subscribe, reading, none);
}

const TIME_STYLE: Record<ListTime, DateTimeFormatOptions> = {
  time: { hour: "numeric", minute: "2-digit" },
  weekday: { weekday: "short" },
  date: { month: "short", day: "numeric" },
  year: { year: "numeric", month: "short", day: "numeric" },
};

/** "Author: text" of a chat's last message: the visitor as You, an agent by name, a person by login. */
function useLastLine() {
  const t = useTranslations("chats.list");
  return (last: ChatLastMessage | null, me: string | null): string => {
    if (!last) return t("noMessages");
    const author = isMine(last.author, me) ? t("you") : last.author.kind === "hub" ? t("hub") : (last.author.login ?? last.author.name);
    return t("lastMessage", { author, text: last.hidden || last.preview === null ? t("hiddenMessage") : last.preview });
  };
}

/**
 * A chat's row in the list (web/DESIGN.md, Chats): its face, its name (a direct chat's agent's), when its last message
 * came (the time today, the weekday this week, else the date), and that message as "Author: text", or that it is hidden
 * from the visitor. Unread: the name and the line in bold and the count of messages of others not read yet. The link's
 * name is the chat's name with that count; the last message describes it.
 */
export function ChatRow({ chat, active, me }: { chat: ChatSummary; active: boolean; me: string | null }) {
  const t = useTranslations("chats.list");
  const format = useFormatter();
  const timeZone = useTimeZone() ?? Intl.DateTimeFormat().resolvedOptions().timeZone;
  const now = useMinuteClock();
  const lastLine = useLastLine();
  const ids = useId();
  const name = chatName(chat);
  const unread = chat.unread > 0;
  const when = chat.last_message?.created_at ?? chat.last_message_at ?? chat.created_at;
  const date = new Date(when);
  const style = now === null ? "date" : listTime(date, new Date(now), timeZone);
  const last = lastLine(chat.last_message, me);
  return (
    <li>
      <Link
        href={chatHref(chat.id)}
        aria-current={active ? "page" : undefined}
        aria-label={unread ? t("rowLabelUnread", { title: name, count: chat.unread }) : name}
        aria-describedby={`${ids}-last`}
        className={cn("flex min-h-16 items-center gap-3 rounded-sm px-2.5 py-2 transition-colors", active ? "bg-surface-selected" : "hover:bg-accent")}
        data-testid="chat-row"
        data-chat-id={chat.id}
        data-kind={chat.kind}
        data-unread={chat.unread}
      >
        <ChatAvatar face={summaryFace(chat)} size="lg" />
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="flex min-w-0 items-baseline gap-2">
            <span className={cn("min-w-0 flex-1 truncate text-sm text-foreground", unread ? "font-semibold" : "font-medium")} data-testid="chat-row-name">
              {name}
            </span>
            <time
              dateTime={when}
              title={format.dateTime(date, { dateStyle: "medium", timeStyle: "short" })}
              className={cn("shrink-0 text-xs tabular-nums", unread ? "font-medium text-foreground" : "text-fg-subtle")}
              data-testid="chat-row-time"
            >
              {format.dateTime(date, TIME_STYLE[style])}
            </time>
          </span>
          <span className="flex min-w-0 items-center gap-2">
            <span
              id={`${ids}-last`}
              className={cn(
                "min-w-0 flex-1 truncate text-[13px] leading-[18px]",
                unread ? "font-medium text-foreground" : "text-muted-foreground",
                (chat.last_message?.hidden || !chat.last_message) && "italic",
              )}
              data-testid="chat-row-last"
            >
              {last}
            </span>
            {unread ? (
              <span
                aria-hidden="true"
                className="inline-flex h-4.5 min-w-4.5 shrink-0 items-center justify-center rounded-full bg-attention-soft px-1.25 text-[11px] leading-none font-medium text-attention tabular-nums"
                data-testid="chat-row-unread"
              >
                {chat.unread > 99 ? "99+" : chat.unread}
              </span>
            ) : null}
          </span>
        </span>
      </Link>
    </li>
  );
}
