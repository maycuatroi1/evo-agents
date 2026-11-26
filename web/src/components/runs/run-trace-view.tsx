"use client";

import { ChevronLeft } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { AgentProfile } from "./agent-profile";
import { AgentTrace, type TraceContext, type TraceReveal } from "./agent-trace";
import type { RunEvent } from "./queries";
import type { useAgentProfile } from "./run-page-state";
import type { RunTask } from "./run-tasks";
import type { LogStatus } from "./use-run-log";

/**
 * The session card's Trace tab on a run's page: the Running now panel (`panel`) over the main agent's trace, or, while a
 * subagent's profile is open (`viewed`), that profile in their place. The main agent's trace stays mounted under a
 * profile, so it keeps its place and its open rows when the person comes back to it.
 */
export function RunTraceView({
  viewed,
  profile,
  panel,
  events,
  status,
  context,
  tasks,
  shown,
  fill,
  reveal,
}: {
  viewed: RunTask | null;
  profile: ReturnType<typeof useAgentProfile>;
  panel: ReactNode;
  events: readonly RunEvent[];
  status: LogStatus;
  context: TraceContext;
  tasks: readonly RunTask[];
  shown: boolean;
  fill: boolean | "always";
  reveal: TraceReveal | null;
}) {
  return (
    <>
      {viewed ? (
        <AgentProfile
          task={viewed}
          events={events}
          status={status}
          context={context}
          tasks={tasks}
          tab={profile.tab}
          onTab={profile.setTab}
          onBack={profile.close}
          shown={shown}
          fill={fill}
        />
      ) : null}
      <div className={cn("contents", viewed && "hidden")}>
        {panel}
        <AgentTrace events={events} status={status} context={context} shown={shown && !viewed} fill={fill} reveal={reveal} tasks={tasks} />
      </div>
    </>
  );
}

/** The head of a phone's session screen: Back, and the run, with the subagent whose profile is open. */
export function ScreenHead({ runId, agent, onBack }: { runId: number; agent: string | null; onBack: () => void }) {
  const t = useTranslations("runs.detail");
  return (
    <div className="-ml-2 flex items-center gap-2">
      {/* Over a subagent's profile, Back steps back to the main agent's trace first. */}
      <Button type="button" variant="ghost" size="icon" onClick={onBack} aria-label={agent ? t("agent.back") : t("preview.back", { id: runId })} data-testid="run-screen-back">
        <ChevronLeft aria-hidden="true" />
      </Button>
      <h1 className="min-w-0 truncate text-[15px] leading-[22px] font-semibold">
        {t("title")} <span className="tabular-nums">#{runId}</span>
        {agent ? <span className="font-normal text-muted-foreground">{`: ${agent}`}</span> : null}
      </h1>
    </div>
  );
}
