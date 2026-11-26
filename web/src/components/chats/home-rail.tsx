"use client";

import { useQuery } from "@tanstack/react-query";
import { Menu, MessageCircle, Plus, Search } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { type DateTimeFormatOptions, useFormatter, useTimeZone, useTranslations } from "next-intl";
import { type MouseEvent, type ReactNode, useId, useState } from "react";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useSidebar } from "@/components/ui/sidebar";
import { browserApi } from "@/lib/api/browser";
import { type ChatHomeAgent, chatsQuery, LIST_LIVE_MS } from "@/lib/chat-keys";
import { type ChatSummary, listTime, type ListTime, summaryFace } from "@/lib/chats";
import { EARLIER_SHOWN, GROUPS_SHOWN, homeRail, lastActive, RAIL_SECTIONS, type RailSection, railTitle } from "@/lib/home-rail";
import { cn } from "@/lib/utils";

import { AgentPicker } from "./agent-picker";
import { ChatAvatar } from "./chat-avatar";
import { useMinuteClock } from "./chat-row";
import { useWaitingLine, useWorkStatusText, WorkDot } from "./work-look";

/** The query parameter that names the task open on Home, so a reload or a shared link keeps it. */
export const TASK_PARAM = "task";

/** Home with agent `handle` chosen and, when given, chat `task` open. */
export function homeHref(handle: string, task: number | null = null): Route {
  const params = new URLSearchParams({ agent: handle });
  if (task !== null) params.set(TASK_PARAM, String(task));
  return `/?${params.toString()}` as Route;
}

const TIME_STYLE: Record<ListTime, DateTimeFormatOptions> = {
  time: { hour: "numeric", minute: "2-digit" },
  weekday: { weekday: "short" },
  date: { month: "short", day: "numeric" },
  year: { year: "numeric", month: "short", day: "numeric" },
};

/** A row's link opens its chat in Home's frame; a click with a modifier key opens it as the browser would. */
function opener(onOpen: () => void) {
  return (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    onOpen();
  };
}

const ROW = "flex min-h-11 w-full min-w-0 items-start gap-2.5 rounded-sm px-2 py-1.5 text-left transition-colors";
const ROW_STATE = "hover:bg-sidebar-accent aria-[current=page]:bg-surface-selected";

function Unread({ count }: { count: number }) {
  return (
    <span
      aria-hidden="true"
      className="mt-0.5 inline-flex h-4.5 min-w-4.5 shrink-0 items-center justify-center rounded-full bg-attention-soft px-1.25 text-[11px] leading-none font-medium text-attention tabular-nums"
      data-testid="home-row-unread"
    >
      {count > 99 ? "99+" : count}
    </span>
  );
}

function When({ at }: { at: number }) {
  const format = useFormatter();
  const timeZone = useTimeZone() ?? Intl.DateTimeFormat().resolvedOptions().timeZone;
  const now = useMinuteClock();
  const date = new Date(at);
  const style = now === null ? "date" : listTime(date, new Date(now), timeZone);
  return (
    <time dateTime={date.toISOString()} className="shrink-0 text-xs text-fg-subtle tabular-nums">
      {format.dateTime(date, TIME_STYLE[style])}
    </time>
  );
}

/**
 * A task's row: its state's dot, its title as the rail shows it (`railTitle`), when it was last active, and a line with
 * its state and what is left (Needs you) or what it waits for. A done task shows no count of unread messages.
 */
function TaskRow({ chat, section, href, active, onOpen }: { chat: ChatSummary; section: RailSection; href: Route; active: boolean; onOpen: () => void }) {
  const t = useTranslations("chatHome.rail");
  const statusText = useWorkStatusText();
  const waitingLine = useWaitingLine();
  const work = chat.work!;
  const title = railTitle(work.title);
  const unread = work.status === "done" ? 0 : chat.unread;
  const lineId = useId();
  const line = `${statusText(work.status)}: ${section === "needsYou" && work.left ? work.left : waitingLine(work)}`;
  return (
    <li>
      <Link
        href={href}
        onClick={opener(onOpen)}
        aria-current={active ? "page" : undefined}
        aria-label={unread > 0 ? t("rowUnread", { title, count: unread }) : title}
        aria-describedby={lineId}
        className={cn(ROW, ROW_STATE)}
        data-testid="home-task"
        data-chat-id={chat.id}
        data-status={work.status}
        data-section={section}
      >
        <WorkDot status={work.status} className="mt-1.5" />
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="flex min-w-0 items-baseline gap-2">
            <span
              className={cn("min-w-0 flex-1 truncate text-[13px] leading-[18px] text-foreground", unread > 0 ? "font-semibold" : "font-medium")}
              data-testid="home-task-title"
            >
              {title}
            </span>
            <When at={lastActive(chat)} />
          </span>
          <span id={lineId} className="truncate text-xs leading-4 text-muted-foreground" data-testid="home-task-line">
            {line}
          </span>
        </span>
        {unread > 0 ? <Unread count={unread} /> : null}
      </Link>
    </li>
  );
}

function GroupRow({ chat, href, active, onOpen }: { chat: ChatSummary; href: Route; active: boolean; onOpen: () => void }) {
  const t = useTranslations("chatHome.rail");
  const title = railTitle(chat.title);
  return (
    <li>
      <Link
        href={href}
        onClick={opener(onOpen)}
        aria-current={active ? "page" : undefined}
        aria-label={chat.unread > 0 ? t("rowUnread", { title, count: chat.unread }) : title}
        className={cn(ROW, ROW_STATE, "items-center")}
        data-testid="home-group"
        data-chat-id={chat.id}
        data-kind={chat.kind}
      >
        <ChatAvatar face={summaryFace(chat)} />
        <span className="min-w-0 flex-1 truncate text-[13px] leading-[18px] font-medium text-foreground">{title}</span>
        {chat.unread > 0 ? <Unread count={chat.unread} /> : null}
      </Link>
    </li>
  );
}

function Section({ id, title, children, testId }: { id: string; title: string; children: ReactNode; testId: string }) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-px" data-testid={testId}>
      <h2 id={id} className="px-2 pt-3 pb-1 text-[11px] leading-4 font-semibold tracking-[0.06em] text-fg-subtle uppercase">
        {title}
      </h2>
      <ul className="flex flex-col gap-px">{children}</ul>
    </section>
  );
}

/** The button that opens the hub's menu, the sidebar of every other page as a sheet; Ctrl or Cmd + B opens it too. */
export function HubMenuButton({ className }: { className?: string }) {
  const t = useTranslations("chatHome.rail");
  const { setOpenMobile } = useSidebar();
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      className={cn("shrink-0 max-md:size-11", className)}
      aria-label={t("menu")}
      aria-keyshortcuts="Control+B Meta+B"
      title={t("menu")}
      onClick={() => setOpenMobile(true)}
      data-testid="home-menu"
    >
      <Menu aria-hidden="true" />
    </Button>
  );
}

type RailProps = {
  agents: readonly ChatHomeAgent[];
  agent: ChatHomeAgent;
  onPick: (handle: string) => void;
  /** The chat open in the frame; null for the direct chat. */
  task: number | null;
  onOpen: (task: number | null) => void;
  onNewTask: () => void;
  foot?: ReactNode;
};

/**
 * Home's rail (web/DESIGN.md, Chat home), in place of the hub's sidebar: the agent picker and the hub's menu, New task,
 * the search of the tasks, the direct chat, then the agent's tasks in Needs you, Running, This week and Earlier (by the
 * viewer's calendar), the older ones behind a button, and the group and settle chats it is in; the page's `foot` at the
 * end (the palette, the account, the Inbox, Today). The tasks are the work chats of `GET /v1/chats` the agent is in.
 */
export function HomeRail({ agents, agent, onPick, task, onOpen, onNewTask, foot }: RailProps) {
  const t = useTranslations("chatHome.rail");
  const ids = useId();
  const timeZone = useTimeZone() ?? Intl.DateTimeFormat().resolvedOptions().timeZone;
  const now = useMinuteClock();
  const [query, setQuery] = useState("");
  const [older, setOlder] = useState(false);
  const list = useQuery({
    ...chatsQuery(browserApi),
    refetchInterval: LIST_LIVE_MS,
  });
  // Before the browser's clock is read (the server, hydration), the list's own latest activity stands for now.
  const at = now ?? Math.max(0, ...(list.data ?? []).map(lastActive));
  const rail = list.data ? homeRail(list.data, agent.handle, at, timeZone, query) : null;
  const tasks = rail ? RAIL_SECTIONS.reduce((sum, section) => sum + rail[section].length, 0) : 0;

  return (
    <nav
      aria-label={t("label", { name: agent.name })}
      className="flex h-full min-h-0 w-full flex-col bg-sidebar text-sidebar-foreground"
      data-testid="home-rail"
      data-agent={agent.handle}
    >
      <div className="flex flex-col gap-2 p-3 pb-2">
        <div className="flex min-w-0 items-center gap-1">
          <AgentPicker agents={agents} current={agent} onPick={onPick} className="min-w-0 flex-1" />
          <HubMenuButton />
        </div>
        <Button type="button" variant="outline" className="w-full justify-start max-md:min-h-11" onClick={onNewTask} data-testid="home-new-task">
          <Plus aria-hidden="true" />
          {t("newTask")}
        </Button>
        <div className="relative">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-fg-subtle" aria-hidden="true" />
          <Input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t("search")}
            aria-label={t("search")}
            className="pl-8 max-md:h-11"
            data-testid="home-rail-search"
          />
        </div>
      </div>
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-3 pb-3">
        <ul className="flex flex-col">
          <li>
            <Link
              href={homeHref(agent.handle)}
              onClick={opener(() => onOpen(null))}
              aria-current={task === null ? "page" : undefined}
              className={cn(ROW, ROW_STATE, "items-center")}
              data-testid="home-rail-direct"
            >
              <AgentAvatar agent={agent} size="sm" />
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="truncate text-[13px] leading-[18px] font-medium text-foreground">{t("direct")}</span>
                <span className="truncate text-xs leading-4 text-muted-foreground">{t("directLine", { name: agent.name })}</span>
              </span>
              <MessageCircle className="size-4 shrink-0 text-fg-subtle" aria-hidden="true" />
            </Link>
          </li>
        </ul>
        {!rail ? (
          <div className="flex flex-col gap-2 px-2 pt-4" data-testid="home-rail-loading">
            {[0, 1, 2, 3].map((row) => (
              <Skeleton key={row} className="h-9 w-full rounded-sm" />
            ))}
          </div>
        ) : (
          <>
            {RAIL_SECTIONS.map((section) => {
              const rows = section === "earlier" && !older ? rail.earlier.slice(0, EARLIER_SHOWN) : rail[section];
              if (rows.length === 0) return null;
              return (
                <Section key={section} id={`${ids}-${section}`} title={t(`sections.${section}`)} testId={`home-rail-${section}`}>
                  {rows.map((chat) => (
                    <TaskRow
                      key={chat.id}
                      chat={chat}
                      section={section}
                      href={homeHref(agent.handle, chat.id)}
                      active={task === chat.id}
                      onOpen={() => onOpen(chat.id)}
                    />
                  ))}
                </Section>
              );
            })}
            {!older && rail.earlier.length > EARLIER_SHOWN ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="mt-1 justify-start text-muted-foreground max-md:min-h-11"
                onClick={() => setOlder(true)}
                data-testid="home-rail-older"
              >
                {t("older")}
              </Button>
            ) : null}
            {tasks === 0 ? (
              <p className="px-2 pt-4 text-[13px] text-muted-foreground" data-testid="home-rail-empty">
                {query.trim() ? t("noMatch", { query: query.trim() }) : t("empty", { name: agent.name })}
              </p>
            ) : null}
            {rail.groups.length > 0 ? (
              <Section id={`${ids}-groups`} title={t("groups")} testId="home-rail-groups">
                {rail.groups.slice(0, GROUPS_SHOWN).map((chat) => (
                  <GroupRow key={chat.id} chat={chat} href={homeHref(agent.handle, chat.id)} active={task === chat.id} onOpen={() => onOpen(chat.id)} />
                ))}
              </Section>
            ) : null}
          </>
        )}
      </div>
      {foot ? <div className="flex flex-col gap-2 border-t border-sidebar-border p-3">{foot}</div> : null}
    </nav>
  );
}
