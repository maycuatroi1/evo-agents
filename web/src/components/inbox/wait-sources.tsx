"use client";

import { Bot, CalendarClock, ExternalLink, GitPullRequest, Radio, SquareCheckBig, Workflow, type LucideIcon } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import Link from "next/link";
import type { ReactNode } from "react";

import { projectHref } from "@/components/shell/nav";
import { useWaitSpan } from "@/components/status/wait-pill";
import { Ago } from "@/components/data/ago";
import { threadHref } from "@/lib/work";
import {
  type AgentAnswerWait,
  type EventWait,
  judgesSpoke,
  type MergeCheckWait,
  type PlanStepWait,
  type WaitingOn,
  type WaitKind,
} from "@/lib/waits";

import { WorkflowCard } from "./wait-workflow";

const LINK = "inline-flex min-h-7 items-center gap-1.5 text-[13px] font-medium text-brand underline-offset-4 hover:text-brand-hover hover:underline max-md:min-h-11";

export const SOURCE_ICON: Record<WaitKind, LucideIcon> = {
  workflow_run: Workflow,
  plan_step: SquareCheckBig,
  agent_answer: Bot,
  merge_check: GitPullRequest,
  time: CalendarClock,
  event: Radio,
};

/**
 * The source card of a wait (web/DESIGN.md, The wait panel): what the run waits for, as the hub read it, in words and
 * links, one card for each kind of `waiting_on`. None of them shows the wait's JSON; that stays folded under "The wait
 * as the hub keeps it". A wait the hub said nothing more of is a time.
 */
export function WaitSource({ on, wakeAt, project, now }: { on: WaitingOn | null; wakeAt: string | null; project: string; now: number | null }) {
  const kind: WaitKind = on?.kind ?? "time";
  const t = useTranslations("wait.source");
  const Icon = SOURCE_ICON[kind];
  return (
    <section className="flex min-w-0 flex-col gap-3 rounded-md border bg-card p-3 sm:p-4" aria-label={t(`title.${kind}`)} data-testid="wait-source" data-kind={kind}>
      <p className="flex items-center gap-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">
        <Icon className="size-4 shrink-0" aria-hidden="true" />
        {t(`title.${kind}`)}
      </p>
      {kind === "workflow_run" && on?.workflow_run ? (
        <WorkflowCard run={on.workflow_run} now={now} />
      ) : kind === "plan_step" && on?.plan_step ? (
        <PlanStepCard wait={on.plan_step} project={project} />
      ) : kind === "agent_answer" && on?.agent_answer ? (
        <AgentAnswerCard wait={on.agent_answer} project={project} />
      ) : kind === "merge_check" && on?.merge_check ? (
        <MergeCheckCard check={on.merge_check} />
      ) : kind === "event" && on?.event ? (
        <EventCard event={on.event} />
      ) : (
        <TimeCard at={on?.time?.wake_at ?? wakeAt} now={now} />
      )}
    </section>
  );
}

function Line({ children, testId }: { children: ReactNode; testId?: string }) {
  return (
    <p className="text-[15px] leading-[22px] font-semibold [overflow-wrap:anywhere]" data-testid={testId}>
      {children}
    </p>
  );
}

function Quiet({ children, testId }: { children: ReactNode; testId?: string }) {
  return (
    <p className="text-[13px] leading-[18px] text-muted-foreground [overflow-wrap:anywhere]" data-testid={testId}>
      {children}
    </p>
  );
}

function runPath(project: string, id: number) {
  return projectHref(project, `runs/${id}`);
}

/** A step of another plan: the plan and step with its title, its status and the status wanted, and the run on it. */
function PlanStepCard({ wait, project }: { wait: PlanStepWait; project: string }) {
  const t = useTranslations("wait.source.plan");
  const step = projectHref(project, `plans/${encodeURIComponent(wait.plan)}/steps/${encodeURIComponent(wait.step)}`);
  return (
    <div className="flex flex-col gap-2" data-testid="wait-plan-step">
      <Line>
        {t("step", { plan: wait.plan, step: wait.step })}
        {wait.title ? <span className="font-normal">{`: ${wait.title}`}</span> : null}
      </Line>
      <Quiet testId="wait-plan-status">{t("status", { status: wait.status ?? t("unknown"), wanted: wait.wanted ?? "done" })}</Quiet>
      {wait.run ? (
        <Quiet>
          <Link href={runPath(project, wait.run.id)} className={LINK}>
            {t("run", { id: wait.run.id, state: wait.run.state })}
          </Link>
        </Quiet>
      ) : null}
      <Link href={step} className={LINK} data-testid="wait-plan-link">
        {t("open", { step: wait.step })}
      </Link>
    </div>
  );
}

/** The answer of another agent: whom, its run and state, the question asked, and the work chat it is in. */
function AgentAnswerCard({ wait, project }: { wait: AgentAnswerWait; project: string }) {
  const t = useTranslations("wait.source.agent");
  const runProject = wait.project ?? project;
  return (
    <div className="flex flex-col gap-2" data-testid="wait-agent-answer">
      <Line>
        {wait.handle ? `@${wait.handle}` : t("someone")}
        {wait.run ? (
          <>
            {", "}
            <Link href={runPath(runProject, wait.run.id)} className="font-normal text-brand underline-offset-4 hover:underline">
              {t("run", { id: wait.run.id })}
            </Link>
          </>
        ) : null}
      </Line>
      {wait.run ? <Quiet testId="wait-agent-state">{t("state", { state: wait.run.state })}</Quiet> : null}
      {wait.last_activity_at ? <Quiet>{t.rich("active", { time: () => <Ago value={wait.last_activity_at} never="-" /> })}</Quiet> : null}
      {wait.question ? (
        <div className="flex flex-col gap-1">
          <p className="text-xs font-medium text-muted-foreground">{t("question")}</p>
          <blockquote className="border-l-2 pl-3 text-[13px] leading-[18px] whitespace-pre-wrap [overflow-wrap:anywhere]" data-testid="wait-agent-question">
            {wait.question}
          </blockquote>
        </div>
      ) : null}
      {wait.chat_id !== null && wait.thread_id !== null ? (
        <Link href={threadHref({ chat_id: wait.chat_id, root_id: wait.thread_id })} className={LINK} data-testid="wait-agent-chat">
          {t("openChat")}
        </Link>
      ) : null}
    </div>
  );
}

/** The merge check of a pull request: the pull request at its commit, the check's state and its judges. */
function MergeCheckCard({ check }: { check: MergeCheckWait }) {
  const t = useTranslations("wait.source.merge");
  const { spoke, total } = judgesSpoke(check);
  return (
    <div className="flex flex-col gap-2" data-testid="wait-merge-check">
      <Line>
        {t("pr", { number: check.number, sha: check.head_sha.slice(0, 7) })}
        {check.title ? <span className="font-normal">{`: ${check.title}`}</span> : null}
      </Line>
      <Quiet testId="wait-merge-state">
        {t(`state.${check.state}`)}
        {total > 0 ? `, ${t("judges", { spoke, total })}` : null}
      </Quiet>
      {check.reason ? <Quiet>{check.reason}</Quiet> : null}
      {check.judges.length > 0 ? (
        <ul className="flex flex-wrap gap-1.5">
          {check.judges.map((judge, index) => (
            <li key={`${judge.round}-${index}`} className="rounded-full border px-2 py-0.5 text-xs text-muted-foreground">
              {t("judge", { round: judge.round, state: judge.state })}
              {judge.model ? `, ${judge.model}` : null}
            </li>
          ))}
        </ul>
      ) : null}
      <span className="text-xs text-muted-foreground font-mono">{check.repo}</span>
      {check.url ? (
        <a href={check.url} target="_blank" rel="noreferrer" className={LINK} data-testid="wait-merge-link">
          {t("open")}
          <ExternalLink className="size-3.5" aria-hidden="true" />
        </a>
      ) : null}
    </div>
  );
}

/** A time: when, in the visitor's time zone and in UTC, and how long is left. */
function TimeCard({ at, now }: { at: string | null; now: number | null }) {
  const t = useTranslations("wait.source.time");
  const format = useFormatter();
  const span = useWaitSpan();
  if (!at) return <Quiet>{t("none")}</Quiet>;
  const when = new Date(at);
  const left = now === null ? null : when.getTime() - now;
  return (
    <div className="flex flex-col gap-2" data-testid="wait-time">
      <Line>
        {t("until", { time: format.dateTime(when, { dateStyle: "medium", timeStyle: "short" }), utc: format.dateTime(when, { timeStyle: "short", timeZone: "UTC" }) })}
      </Line>
      {left !== null ? <Quiet testId="wait-time-left">{left > 0 ? t("left", { duration: span(left) }) : t("came")}</Quiet> : null}
    </div>
  );
}

/** An event of the hub: its name (or its kind when the hub has no words for it) and what its payload must contain. */
function EventCard({ event }: { event: EventWait }) {
  const t = useTranslations("wait.source.event");
  return (
    <div className="flex flex-col gap-2" data-testid="wait-event">
      <Line>{event.name ?? event.kind}</Line>
      <Quiet>
        <span className="font-mono">{event.kind}</span>
        {", "}
        {t("notYet")}
      </Quiet>
      {event.contains.length > 0 ? (
        <ul className="flex flex-wrap gap-1.5" aria-label={t("contains")} data-testid="wait-event-pairs">
          {event.contains.map((pair) => (
            <li key={pair.key} className="rounded-full border bg-muted px-2 py-0.5 font-mono text-xs [overflow-wrap:anywhere]">
              {pair.key}={pair.value}
            </li>
          ))}
        </ul>
      ) : null}
      {event.name === null ? <Quiet>{t("unnamed")}</Quiet> : null}
    </div>
  );
}
