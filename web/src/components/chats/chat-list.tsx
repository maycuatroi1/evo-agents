"use client";

import { MessagesSquare, Plus, Search } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";

import { InFrame } from "@/components/data/data-card";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, NoResults } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { chatsQuery, LIST_LIVE_MS } from "@/lib/chat-queries";
import { chatHref, type ChatSummary, filterChats, unreadTotal } from "@/lib/chats";
import { cn } from "@/lib/utils";

import { useMe } from "./hooks";

// The form loads when New chat is first pressed: the list carries only its button.
const NewChatDialog = dynamic(() => import("./new-chat-dialog").then((module) => module.NewChatDialog), { ssr: false });

/** A filter shows over the list once it holds more than this many chats. */
const FILTER_FROM = 6;

/**
 * The visitor's chats (web/DESIGN.md, Chats), the latest active first: each one's title, when its last message came,
 * who is in it and the messages of others not read yet, in an `attention` count. The chat shown is marked. Read every
 * 10 seconds while shown; a chat's own page keeps its count at zero while it is in view. New chat sits at the top.
 */
export function ChatList({ activeId, initialError, className }: { activeId: number | null; initialError: ApiErrorInfo | null; className?: string }) {
  const t = useTranslations("chats.list");
  const state = useHubQuery({ ...chatsQuery(browserApi), refetchInterval: LIST_LIVE_MS }, initialError, { live: activeId === null });
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState("");
  const chats = state.status === "success" ? state.data : null;
  const Heading = activeId === null ? "h1" : "h2";
  const unread = chats ? unreadTotal(chats) : 0;
  return (
    <section
      aria-labelledby="chats-list-title"
      className={cn("min-h-0 flex-col overflow-hidden rounded-md border bg-card shadow-raised", className)}
      data-testid="chats-list"
    >
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-4 py-3">
        <Heading id="chats-list-title" className={cn("min-w-0 font-semibold tracking-tight", activeId === null ? "text-xl" : "text-[15px] leading-[22px]")}>
          {t("title")}
        </Heading>
        {unread > 0 ? (
          <Badge variant="warning" className="tabular-nums" data-testid="chats-unread-total">
            {t("unreadTotal", { count: unread })}
          </Badge>
        ) : null}
        <Button size="sm" variant={activeId === null ? "default" : "outline"} className="ml-auto" onClick={() => setCreating(true)} data-testid="chats-new">
          <Plus aria-hidden="true" />
          {t("new")}
        </Button>
      </header>
      {chats && chats.length > FILTER_FROM ? (
        <div className="relative border-b px-3 py-2">
          <Search className="pointer-events-none absolute top-1/2 left-5.5 size-4 -translate-y-1/2 text-fg-subtle" aria-hidden="true" />
          <Input
            type="search"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder={t("filter")}
            aria-label={t("filter")}
            className="pl-8"
            data-testid="chats-filter"
          />
        </div>
      ) : null}
      <InFrame>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <QueryView state={state} loading={<ListLoading />}>
            {(data) =>
              data.length === 0 ? (
                <EmptyState icon={MessagesSquare} title={t("emptyTitle")} description={t("emptyBody")} testId="chats-empty">
                  <Button onClick={() => setCreating(true)} data-testid="chats-empty-new">
                    <Plus aria-hidden="true" />
                    {t("new")}
                  </Button>
                </EmptyState>
              ) : (
                <Rows chats={filterChats(data, filter)} activeId={activeId} filter={filter} onClear={() => setFilter("")} />
              )
            }
          </QueryView>
        </div>
      </InFrame>
      {creating ? <NewChatDialog open={creating} onOpenChange={setCreating} /> : null}
    </section>
  );
}

function Rows({ chats, activeId, filter, onClear }: { chats: ChatSummary[]; activeId: number | null; filter: string; onClear: () => void }) {
  const t = useTranslations("chats.list");
  const format = useFormatter();
  const me = useMe()?.toLowerCase() ?? null;
  if (chats.length === 0) {
    return <NoResults title={t("noMatch")} filters={[{ label: t("filterChip"), value: filter }]} onClear={onClear} testId="chats-no-match" />;
  }
  return (
    <ul className="flex flex-col p-1.5" aria-label={t("caption")} data-testid="chats-rows">
      {chats.map((chat) => {
        const active = chat.id === activeId;
        const when = chat.last_message_at ?? chat.created_at;
        const members = [...chat.agents.map((handle) => `@${handle}`), ...chat.people.filter((login) => login.toLowerCase() !== me)];
        const label = chat.unread > 0 ? t("rowLabelUnread", { title: chat.title, count: chat.unread }) : undefined;
        return (
          <li key={chat.id}>
            <Link
              href={chatHref(chat.id)}
              aria-current={active ? "page" : undefined}
              aria-label={label}
              className={cn(
                "flex min-h-14 flex-col gap-0.5 rounded-sm px-3 py-2 transition-colors",
                active ? "bg-surface-selected" : "hover:bg-accent",
              )}
              data-testid="chat-row"
              data-chat-id={chat.id}
              data-unread={chat.unread}
            >
              <span className="flex min-w-0 items-center gap-2">
                <span className={cn("min-w-0 flex-1 truncate text-sm", chat.unread > 0 ? "font-semibold text-foreground" : "font-medium text-foreground")}>
                  {chat.title}
                </span>
                {chat.unread > 0 ? (
                  <span
                    aria-hidden="true"
                    className="inline-flex h-4.5 min-w-4.5 shrink-0 items-center justify-center rounded-full bg-attention-soft px-1.25 text-[11px] leading-none font-medium text-attention tabular-nums"
                    data-testid="chat-row-unread"
                  >
                    {chat.unread > 99 ? "99+" : chat.unread}
                  </span>
                ) : null}
              </span>
              <span className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
                <span className="min-w-0 flex-1 truncate">{members.join(", ")}</span>
                <time dateTime={when} className="shrink-0 text-fg-subtle tabular-nums">
                  {format.dateTime(new Date(when), { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}
                </time>
              </span>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

function ListLoading() {
  return (
    <div className="flex flex-col gap-2 p-3" data-testid="chats-loading">
      {[0, 1, 2, 3].map((row) => (
        <div key={row} className="flex flex-col gap-1.5 px-1 py-1.5">
          <Skeleton className="h-4 w-2/3 rounded-xs" />
          <Skeleton className="h-3 w-5/6 rounded-xs" />
        </div>
      ))}
    </div>
  );
}
