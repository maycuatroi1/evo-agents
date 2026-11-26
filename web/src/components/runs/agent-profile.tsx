"use client";

import { ArrowLeft, Bot, ChevronRight, FileText, ListTree, ScrollText } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useMemo, useState } from "react";

import { SafeMarkdown } from "@/components/data/markdown";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useNow } from "@/hooks/use-now";
import { cn } from "@/lib/utils";

import { AgentTrace, type TraceContext } from "./agent-trace";
import type { RunEvent } from "./queries";
import { type AgentTab } from "./run-page-state";
import { eventsOfAgent, type RunTask, reportAsk } from "./run-tasks";
import { holdsStepReport } from "./step-report-start";
import { useTraceDuration } from "./trace-look";
import { StepReportMessage } from "./trace-report-lazy";
import { TermBlock } from "./trace-term";
import type { LogStatus } from "./use-run-log";

/**
 * A subagent's profile, in the place of the main agent's trace (`?agent=<task id>`, run-page-state.ts): its name, type,
 * status and figures, and three tabs. Brief is what the main agent asked of it, as Markdown, with the raw `Agent` call
 * behind a button; Activity is its own events, read by the trace's rules; Result is its whole final report, or, until
 * it has one, what its brief asks it to report. On a phone it fills the session's screen.
 */

const PROSE = "max-w-[80ch] text-[15px] leading-6";

/** The tab a profile opens on when the address names none: its report once it has one, else what it does, else its brief. */
export function firstTab(task: RunTask, activity: number): AgentTab {
  if (task.result) return "result";
  return activity > 0 ? "activity" : "brief";
}

/** The `Agent` call that started the subagent, as its raw input, pretty JSON. */
function rawCall(events: readonly RunEvent[], call: string | null): string | null {
  if (!call) return null;
  for (const event of events) {
    if (event.kind !== "tool_call") continue;
    const body = event.body as Record<string, unknown>;
    if (body.toolCallId !== call) continue;
    try {
      return JSON.stringify(body.rawInput ?? body, null, 2);
    } catch {
      return null;
    }
  }
  return null;
}

function Brief({ task, raw }: { task: RunTask; raw: string | null }) {
  const t = useTranslations("runs.detail.agent");
  const [open, setOpen] = useState(false);
  return (
    <div className="flex min-w-0 flex-col gap-4" data-testid="agent-profile-brief">
      {task.prompt ? (
        <SafeMarkdown className={PROSE} testId="agent-profile-brief-text">
          {task.prompt}
        </SafeMarkdown>
      ) : (
        <p className="text-sm text-muted-foreground">{t("noBrief")}</p>
      )}
      {raw ? (
        <div className="flex min-w-0 flex-col gap-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="self-start text-muted-foreground max-md:min-h-11"
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
            data-testid="agent-profile-raw-toggle"
          >
            <ChevronRight className={cn("transition-transform duration-[var(--transition-duration-fast)]", open && "rotate-90")} aria-hidden="true" />
            {open ? t("hideRaw") : t("showRaw")}
          </Button>
          {open ? (
            <div className="overflow-hidden rounded-md border border-term-border">
              <TermBlock text={raw} wrap testId="agent-profile-raw" />
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function Result({ task, active }: { task: RunTask; active: boolean }) {
  const t = useTranslations("runs.detail.agent");
  const message = (text: string) => (
    <SafeMarkdown className={PROSE} testId="agent-profile-result-text">
      {text}
    </SafeMarkdown>
  );
  const prose = (text: string) => <SafeMarkdown className="text-[13px] leading-5">{text}</SafeMarkdown>;
  if (task.result) {
    return (
      <div className="min-w-0" data-testid="agent-profile-result">
        {holdsStepReport(task.result) ? <StepReportMessage text={task.result} message={message} prose={prose} webOf={() => null} /> : message(task.result)}
      </div>
    );
  }
  const ask = reportAsk(task.prompt);
  const running = active && task.status === "running";
  return (
    <div className="flex min-w-0 flex-col gap-4" data-testid="agent-profile-result" data-empty="true">
      <p className="text-sm text-muted-foreground" data-testid="agent-profile-no-result">
        {running ? t("noResultYet") : t("noResult")}
      </p>
      {ask ? (
        <section className="flex min-w-0 flex-col gap-2 rounded-lg border bg-muted/40 p-3" aria-label={t("askedFor")} data-testid="agent-profile-ask">
          <h3 className="text-[13px] leading-5 font-semibold text-foreground">{t("askedFor")}</h3>
          <SafeMarkdown className="text-[13px] leading-5">{ask}</SafeMarkdown>
        </section>
      ) : null}
    </div>
  );
}

export function AgentProfile({
  task,
  events,
  status,
  context,
  tasks,
  tab,
  onTab,
  onBack,
  shown,
  fill,
}: {
  task: RunTask;
  events: readonly RunEvent[];
  status: LogStatus;
  context: TraceContext;
  tasks: readonly RunTask[];
  /** The tab the address names, or null for the profile's first. */
  tab: AgentTab | null;
  onTab: (tab: AgentTab) => void;
  onBack: () => void;
  shown: boolean;
  fill: boolean | "always";
}) {
  const t = useTranslations("runs.detail.agent");
  const tTasks = useTranslations("runs.detail.tasks");
  const format = useFormatter();
  const short = useTraceDuration();
  const activity = useMemo(() => (task.callId ? eventsOfAgent(events, task.callId).length : 0), [events, task.callId]);
  const raw = useMemo(() => rawCall(events, task.callId), [events, task.callId]);
  const status_ = context.active || task.status !== "running" ? task.status : "stopped";
  const running = status_ === "running";
  const now = useNow(running);
  const end = task.endedAt ? Date.parse(task.endedAt) : now;
  const current = tab ?? firstTab(task, activity);
  const figures = [
    end !== null ? short(Math.max(0, end - Date.parse(task.startedAt))) : null,
    task.toolUses !== null ? tTasks("calls", { count: task.toolUses }) : null,
    task.tokens !== null ? tTasks("tokens", { tokens: format.number(task.tokens, { notation: "compact", maximumFractionDigits: 1 }) }) : null,
  ].filter((part): part is string => part !== null);
  const always = fill === "always";
  const nameId = useId();
  // The subagent's trace ends with the typing dots while it works, not while the main agent does.
  const own = useMemo(() => ({ ...context, working: running }), [context, running]);

  return (
    <section
      className={cn("flex min-w-0 flex-col gap-3 pt-3", always ? "min-h-0 flex-1" : fill && "xl:min-h-0 xl:flex-1")}
      aria-labelledby={nameId}
      data-testid="agent-profile"
      data-task-id={task.id}
      data-tab={current}
    >
      <div className="flex min-w-0 flex-col gap-2">
        {/* On a phone the screen's own back button steps back to the main agent. */}
        <Button type="button" variant="ghost" size="sm" className="-ml-2 self-start text-muted-foreground max-md:hidden" onClick={onBack} data-testid="agent-profile-back">
          <ArrowLeft aria-hidden="true" />
          {t("back")}
        </Button>
        <div className="flex min-w-0 items-start gap-2.5">
          <Bot className={cn("mt-0.5 size-5 shrink-0", running ? "text-running" : "text-brand")} aria-hidden="true" />
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <h3 id={nameId} className="text-base leading-6 font-semibold text-foreground [overflow-wrap:anywhere]" data-testid="agent-profile-name">
              {task.name}
            </h3>
            <p className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5 text-xs leading-5 text-muted-foreground">
              <span className={cn("font-medium", running ? "text-running" : status_ === "failed" ? "text-danger" : status_ === "completed" ? "text-success" : "")} data-testid="agent-profile-status">
                {t(`status.${status_}`)}
              </span>
              {task.agentType ? <span className="font-mono text-fg-subtle">{task.agentType}</span> : null}
              {task.background ? <span>{tTasks("background")}</span> : null}
              {figures.length ? <span className="tabular-nums">{figures.join(", ")}</span> : null}
            </p>
          </div>
        </div>
      </div>
      <Tabs value={current} onValueChange={(value) => onTab(value as AgentTab)} className={cn("gap-0", always ? "min-h-0 flex-1" : fill && "xl:min-h-0 xl:flex-1")}>
        <TabsList aria-label={t("tabs", { name: task.name })} className="shadow-[inset_0_-1px_0_var(--color-border)]">
          <TabsTrigger value="brief" className="h-11" data-testid="agent-profile-tab-brief">
            <FileText aria-hidden="true" />
            {t("brief")}
          </TabsTrigger>
          <TabsTrigger value="activity" className="h-11" data-testid="agent-profile-tab-activity">
            <ListTree aria-hidden="true" />
            {t("activity")}
          </TabsTrigger>
          <TabsTrigger value="result" className="h-11" data-testid="agent-profile-tab-result">
            <ScrollText aria-hidden="true" />
            {t("result")}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="brief" className={cn("overflow-y-auto pt-4 pb-3", always ? "min-h-0 flex-1" : fill && "xl:min-h-0 xl:flex-1")}>
          <Brief task={task} raw={raw} />
        </TabsContent>
        <TabsContent value="activity" className={cn("pt-1", always ? "flex min-h-0 flex-1 flex-col" : fill && "xl:flex xl:min-h-0 xl:flex-1 xl:flex-col")} data-testid="agent-profile-activity">
          {activity > 0 && task.callId ? (
            <AgentTrace events={events} status={status} context={own} shown={shown && current === "activity"} fill={fill} agent={task.callId} tasks={tasks} />
          ) : (
            <p className="py-4 text-sm text-muted-foreground" data-testid="agent-profile-no-activity">
              {task.callId ? t("noActivity") : t("noActivityId")}
            </p>
          )}
        </TabsContent>
        <TabsContent value="result" className={cn("overflow-y-auto pt-4 pb-3", always ? "min-h-0 flex-1" : fill && "xl:min-h-0 xl:flex-1")}>
          <Result task={task} active={context.active} />
        </TabsContent>
      </Tabs>
    </section>
  );
}
