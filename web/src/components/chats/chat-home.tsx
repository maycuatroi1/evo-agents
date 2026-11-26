"use client";

import { ArrowLeft, Bot, CalendarCheck, FolderGit2, MessageSquareOff } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { chatAgentsQuery, type ChatHomeAgent } from "@/lib/chat-keys";
import { cn } from "@/lib/utils";

import { AgentChat } from "./agent-chat";
import type { AgentPanels } from "./agent-sheet";
import { ChatBackContext } from "./chat-header";
import { ChatView, type UseUnder } from "./chat-view";
import { HomeRail, HubMenuButton, TASK_PARAM } from "./home-rail";

/** The query parameter that names the agent chosen on Home, so a reload or a shared link keeps it. */
export const AGENT_PARAM = "agent";

/** Where the visitor's projects are listed: the Projects card of Today. */
export const TODAY_PROJECTS = "/today#projects" as Route;

/**
 * The agent Home opens on: the one the address names, when the visitor may still chat with it, else the first of the
 * hub's list, which puts the agent the visitor wrote to last first. Null with no agent.
 */
export function chosenAgent(agents: readonly ChatHomeAgent[], wanted: string | null): ChatHomeAgent | null {
  return agents.find((agent) => agent.handle === wanted) ?? agents[0] ?? null;
}

function taskOf(value: string | null): number | null {
  const id = value === null ? NaN : Number(value);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

/** Writes the agent and the task Home shows into the address, without a new history entry. */
function writeAddress(handle: string, task: number | null) {
  const url = new URL(window.location.href);
  url.searchParams.set(AGENT_PARAM, handle);
  if (task === null) url.searchParams.delete(TASK_PARAM);
  else url.searchParams.set(TASK_PARAM, String(task));
  window.history.replaceState(window.history.state, "", url);
}

/** How Home draws a chat of the rail (a task, a group chat) in its main pane; the app puts its decisions and work card. */
export type RenderTask = (chatId: number) => ReactNode;

const plainTask: RenderTask = (chatId) => <ChatView key={chatId} chatId={chatId} initialError={null} />;

/**
 * Home (`/`), the agent's workspace (web/DESIGN.md, Chat home): no top bar and no hub sidebar, but a rail of the agent
 * chosen (`HomeRail`: its tasks in Needs you, Running, This week and Earlier, its group chats) beside the main pane,
 * which shows the direct chat with the agent (`AgentChat`) or the task picked there (`renderTask`). The agents are the
 * ones the visitor may give work to (GET /v1/me/chat-agents); the one written to last is chosen first. `?agent=HANDLE`
 * names another and `?task=ID` the task open, both written without a new history entry. Under 768 px the rail is the
 * first screen and a task takes the whole screen, with a back control. With no agent the page says why and leads to
 * Projects; with chats off for the hub, it says so and leads to Today.
 */
export function ChatHome({
  initialError,
  chatsOn,
  useUnder,
  agentPanels,
  renderTask = plainTask,
  foot,
}: {
  initialError: ApiErrorInfo | null;
  chatsOn: boolean;
  useUnder?: UseUnder;
  agentPanels?: AgentPanels;
  renderTask?: RenderTask;
  foot?: ReactNode;
}) {
  const t = useTranslations("chatHome");
  const params = useSearchParams();
  const [wanted, setWanted] = useState<string | null>(() => params.get(AGENT_PARAM));
  const [task, setTask] = useState<number | null>(() => taskOf(params.get(TASK_PARAM)));
  // Under 768 px the rail and the main pane take turns: a task named in the address opens on its own.
  const [view, setView] = useState<"rail" | "main">(() => (taskOf(params.get(TASK_PARAM)) === null ? "rail" : "main"));
  const [compose, setCompose] = useState(0);
  const main = useRef<HTMLElement>(null);
  const state = useHubQuery({ ...chatAgentsQuery(browserApi), enabled: chatsOn }, initialError);

  // New task: the direct chat's composer takes the focus once the pane draws it.
  useEffect(() => {
    if (compose === 0) return;
    let frames = 0;
    let handle = 0;
    const focus = () => {
      const box = main.current?.querySelector<HTMLTextAreaElement>("textarea");
      if (box) box.focus();
      else if (frames++ < 30) handle = requestAnimationFrame(focus);
    };
    handle = requestAnimationFrame(focus);
    return () => cancelAnimationFrame(handle);
  }, [compose]);

  const back = useMemo(() => ({ label: t("rail.back"), onBack: () => setView("rail") }), [t]);

  if (!chatsOn) {
    return (
      <Lone title={t("title")} foot={foot}>
        <EmptyState icon={MessageSquareOff} title={t("off.title")} description={t("off.body")} testId="chat-home-off">
          <TodayLink label={t("off.today")} />
        </EmptyState>
      </Lone>
    );
  }

  return (
    <QueryView state={state} loading={<HomeLoading title={t("title")} />}>
      {(agents) => {
        const agent = chosenAgent(agents, wanted);
        if (!agent) {
          return (
            <Lone title={t("title")} foot={foot}>
              <EmptyState icon={Bot} title={t("none.title")} description={t("none.body")} testId="chat-home-none">
                <Button asChild>
                  <Link href={TODAY_PROJECTS} data-testid="chat-home-projects">
                    <FolderGit2 aria-hidden="true" />
                    {t("none.projects")}
                  </Link>
                </Button>
                <TodayLink label={t("none.today")} />
              </EmptyState>
            </Lone>
          );
        }
        const open = (next: number | null) => {
          setTask(next);
          setView("main");
          writeAddress(agent.handle, next);
        };
        const pick = (handle: string) => {
          setWanted(handle);
          setTask(null);
          writeAddress(handle, null);
        };
        return (
          <div className="flex h-dvh min-h-0 w-full min-w-0" data-testid="chat-home" data-view={view}>
            <div className={cn("h-full w-full min-w-0 shrink-0 md:w-72 md:border-r md:border-sidebar-border", view === "main" && "max-md:hidden")}>
              {/* The page's heading under 768 px, where the rail is a screen of its own; beside it the chat has its own. */}
              <h1 className="sr-only md:hidden">{t("title")}</h1>
              <HomeRail
                agents={agents}
                agent={agent}
                onPick={pick}
                task={task}
                onOpen={open}
                onNewTask={() => {
                  open(null);
                  setCompose((count) => count + 1);
                }}
                foot={foot}
              />
            </div>
            <section
              ref={main}
              aria-label={t("rail.main")}
              className={cn("flex h-full min-h-0 min-w-0 flex-1 flex-col p-3 md:p-4", view === "rail" && "max-md:hidden")}
              data-testid="home-main"
              data-task={task ?? undefined}
            >
              {/* The chat's head has the back control under 768 px, the direct chat's start its own. */}
              <ChatBackContext.Provider value={back}>
                {task !== null ? (
                  <div className="flex min-h-0 flex-1">{renderTask(task)}</div>
                ) : (
                  <AgentChat
                    agents={agents}
                    handle={agent.handle}
                    onPick={pick}
                    picker={false}
                    useUnder={useUnder}
                    agentPanels={agentPanels}
                    back={
                      <Button type="button" variant="ghost" size="sm" className="-ml-1 max-md:min-h-11 md:hidden" onClick={back.onBack} data-testid="home-back">
                        <ArrowLeft aria-hidden="true" />
                        {back.label}
                      </Button>
                    }
                    className="h-full"
                  />
                )}
              </ChatBackContext.Provider>
            </section>
          </div>
        );
      }}
    </QueryView>
  );
}

/**
 * A state that stands for the whole page: the page's heading for assistive technology, the hub's menu, the state in the
 * middle, and the rail's foot (the palette, the Inbox, Today, the account) at the end, as Home has no top bar.
 */
function Lone({ title, foot, children }: { title: string; foot: ReactNode; children: ReactNode }) {
  return (
    <div className="flex min-h-dvh w-full min-w-0 flex-col gap-4 p-3 md:p-4">
      <h1 className="sr-only">{title}</h1>
      <HubMenuButton className="self-start" />
      <div className="py-8">{children}</div>
      {foot ? <div className="mt-auto flex w-full max-w-72 flex-col gap-2 border-t border-border pt-3">{foot}</div> : null}
    </div>
  );
}

function TodayLink({ label }: { label: string }) {
  return (
    <Button asChild variant="outline">
      <Link href={"/today" as Route}>
        <CalendarCheck aria-hidden="true" />
        {label}
      </Link>
    </Button>
  );
}

function HomeLoading({ title }: { title: string }) {
  return (
    <div className="h-dvh w-full" data-testid="chat-home-loading">
      <h1 className="sr-only">{title}</h1>
    </div>
  );
}
