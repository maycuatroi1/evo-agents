"use client";

import { MessagesSquare, Plus, Search } from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
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
import { type ChatSummary, filterChats, unreadTotal } from "@/lib/chats";
import { cn } from "@/lib/utils";

import { AgentStripSkeleton } from "./agent-strip-skeleton";
import { ChatRow } from "./chat-row";
import { useMe } from "./hooks";

// The form loads when New chat is first pressed: the list carries only its button.
const NewChatDialog = dynamic(() => import("./new-chat-dialog").then((module) => module.NewChatDialog), { ssr: false });
// The row of agents loads beside the list: its agents are read in the browser anyway, and its rounds stand in meanwhile.
const AgentStrip = dynamic(() => import("./agent-strip").then((module) => module.AgentStrip), { ssr: false, loading: () => <AgentStripSkeleton /> });

/** A filter shows over the list once it holds more than this many chats. */
const FILTER_FROM = 6;

/**
 * The visitor's chats (web/DESIGN.md, Chats): New chat at the top, the row of agents to chat with one to one under it
 * (`AgentStrip`), then the chats, the latest active first, each with its face, name, last message and the messages of
 * others not read yet (`ChatRow`). The chat shown is marked. Read every 10 seconds while shown; a chat's own page keeps
 * its count at zero while it is in view.
 */
export function ChatList({ activeId, initialError, className }: { activeId: number | null; initialError: ApiErrorInfo | null; className?: string }) {
  const t = useTranslations("chats.list");
  const state = useHubQuery({ ...chatsQuery(browserApi), refetchInterval: LIST_LIVE_MS }, initialError, { live: activeId === null });
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState("");
  const chats = state.status === "success" ? state.data : null;
  const Heading = activeId === null ? "h1" : "h2";
  const unread = chats ? unreadTotal(chats) : 0;
  const shown = chats?.find((chat) => chat.id === activeId);
  const current = shown?.kind === "direct" ? (shown.direct_agent?.handle ?? null) : null;
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
      <AgentStrip current={current} />
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
  const me = useMe();
  if (chats.length === 0) {
    return <NoResults title={t("noMatch")} filters={[{ label: t("filterChip"), value: filter }]} onClear={onClear} testId="chats-no-match" />;
  }
  return (
    <ul className="flex flex-col p-1.5" aria-label={t("caption")} data-testid="chats-rows">
      {chats.map((chat) => (
        <ChatRow key={chat.id} chat={chat} active={chat.id === activeId} me={me} />
      ))}
    </ul>
  );
}

function ListLoading() {
  return (
    <div className="flex flex-col gap-1 p-1.5" data-testid="chats-loading">
      {[0, 1, 2, 3].map((row) => (
        <div key={row} className="flex min-h-16 items-center gap-3 px-2.5 py-2">
          <Skeleton className="size-10 shrink-0 rounded-full" />
          <div className="flex min-w-0 flex-1 flex-col gap-1.5">
            <Skeleton className="h-4 w-2/3 rounded-xs" />
            <Skeleton className="h-3 w-5/6 rounded-xs" />
          </div>
        </div>
      ))}
    </div>
  );
}
