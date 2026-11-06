"use client";

import { Bot, CalendarCheck, FolderGit2, MessageSquareOff } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { chatAgentsQuery, type ChatHomeAgent } from "@/lib/chat-keys";

import { AgentChat } from "./agent-chat";
import type { AgentPanels } from "./agent-sheet";
import type { UseUnder } from "./chat-view";

/** The query parameter that names the agent chosen on Home, so a reload or a shared link keeps it. */
export const AGENT_PARAM = "agent";

/** Where the visitor's projects are listed: the Projects card of Today. */
export const TODAY_PROJECTS = "/today#projects" as Route;

/** The page's height under the top bar, as a chat's page has it: the composer stays in sight. */
const HEIGHT = "h-[calc(100dvh-5.25rem)] min-h-[28rem] md:h-[calc(100dvh-6.25rem)]";

/**
 * The agent Home opens on: the one the address names, when the visitor may still chat with it, else the first of the
 * hub's list, which puts the agent the visitor wrote to last first. Null with no agent.
 */
export function chosenAgent(agents: readonly ChatHomeAgent[], wanted: string | null): ChatHomeAgent | null {
  return agents.find((agent) => agent.handle === wanted) ?? agents[0] ?? null;
}

/**
 * Home (`/`), the chat home (web/DESIGN.md, Chat home): a chat with one of the agents the visitor may give work to,
 * over every project of their grants (GET /v1/me/chat-agents), in the frame `AgentChat` draws. The agent written to
 * last is chosen first; `?agent=HANDLE` names another, and a pick writes it there without a new history entry. With no
 * agent the page says why and leads to Projects; with chats off for the hub, it says so and leads to Today, where the
 * work across projects is.
 */
export function ChatHome({
  initialError,
  chatsOn,
  useUnder,
  agentPanels,
}: {
  initialError: ApiErrorInfo | null;
  chatsOn: boolean;
  useUnder?: UseUnder;
  agentPanels?: AgentPanels;
}) {
  const t = useTranslations("chatHome");
  const params = useSearchParams();
  const [wanted, setWanted] = useState<string | null>(() => params.get(AGENT_PARAM));
  const state = useHubQuery({ ...chatAgentsQuery(browserApi), enabled: chatsOn }, initialError);

  if (!chatsOn) {
    return (
      <Lone title={t("title")}>
        <EmptyState icon={MessageSquareOff} title={t("off.title")} description={t("off.body")} testId="chat-home-off">
          <TodayLink label={t("off.today")} />
        </EmptyState>
      </Lone>
    );
  }

  const pick = (handle: string) => {
    setWanted(handle);
    const url = new URL(window.location.href);
    url.searchParams.set(AGENT_PARAM, handle);
    window.history.replaceState(window.history.state, "", url);
  };

  return (
    <QueryView state={state} loading={<HomeLoading title={t("title")} />}>
      {(agents) => {
        const agent = chosenAgent(agents, wanted);
        if (!agent) {
          return (
            <Lone title={t("title")}>
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
        return (
          <div className={HEIGHT} data-testid="chat-home">
            <AgentChat
              agents={agents}
              handle={agent.handle}
              onPick={pick}
              useUnder={useUnder}
              agentPanels={agentPanels}
              actions={
                <Button asChild variant="ghost" size="sm" className="max-sm:size-11 max-sm:px-0">
                  <Link href={"/today" as Route} data-testid="chat-home-today">
                    <CalendarCheck aria-hidden="true" />
                    <span className="max-sm:sr-only">{t("todayLink")}</span>
                  </Link>
                </Button>
              }
              className="h-full"
            />
          </div>
        );
      }}
    </QueryView>
  );
}

/** A state that stands for the whole page: the page's heading for assistive technology, the state in the middle. */
function Lone({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-4 py-8">
      <h1 className="sr-only">{title}</h1>
      {children}
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
    <div className={HEIGHT} data-testid="chat-home-loading">
      <h1 className="sr-only">{title}</h1>
    </div>
  );
}
