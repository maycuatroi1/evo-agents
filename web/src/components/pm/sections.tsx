"use client";

import { CircleDashed, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { When } from "@/components/data/when";
import { StatusPill } from "@/components/status/status-badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { taskHref } from "@/lib/core-plugin-links";
import { cn } from "@/lib/utils";

import type { PmBlocked, PmDaily } from "./api";

type Escalated = PmDaily["escalate"][number];

/** A task's ref as a link to its page in the tasks plugin. */
export function TaskRef({ project, taskRef }: { project: string; taskRef: string }) {
  return (
    <Link
      href={taskHref(project, taskRef)}
      className="font-mono text-brand underline underline-offset-4 hover:text-brand-hover"
      data-testid="pm-task-link"
    >
      {taskRef}
    </Link>
  );
}

/** One row: the task's ref and title, then what the section says of it. */
function Row({ project, taskRef, title, children, testId }: { project: string; taskRef: string; title: string; children?: ReactNode; testId?: string }) {
  return (
    <li className="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-2" data-testid={testId}>
      <TaskRef project={project} taskRef={taskRef} />
      <span className="min-w-0 flex-1 basis-48 [overflow-wrap:anywhere]">{title}</span>
      {children ? <span className="text-[13px] text-muted-foreground">{children}</span> : null}
    </li>
  );
}

/**
 * A section of the report: its heading, how many it holds, and its rows, or the line that says it is empty. `hidden`
 * counts what the reader may not see, which only adds to the count.
 */
export function Section({ id, title, count, hidden, empty, children }: { id: string; title: string; count: number; hidden: number; empty: string; children: ReactNode }) {
  const t = useTranslations("pm");
  return (
    <Card data-testid={`pm-${id}`} aria-labelledby={`pm-${id}-title`} role="region">
      <CardHeader>
        <CardTitle id={`pm-${id}-title`} className="flex flex-wrap items-baseline gap-x-3 text-[15px] leading-[22px] font-semibold">
          <h2>{title}</h2>
          <span className="text-xs font-normal text-fg-subtle tabular-nums" data-testid={`pm-${id}-count`}>
            {t("count", { count })}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="min-w-0">
        {count === 0 ? <p className="text-[13px] text-muted-foreground" data-testid={`pm-${id}-empty`}>{empty}</p> : <ul className="divide-y">{children}</ul>}
        {hidden > 0 ? (
          <p className="pt-2 text-xs text-muted-foreground" data-testid={`pm-${id}-hidden`}>
            {t("hidden", { count: hidden })}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function DoneSection({ project, daily }: { project: string; daily: PmDaily }) {
  const t = useTranslations("pm.done");
  const count = daily.done_use_cases.length + daily.done_tasks.length;
  return (
    <Section id="done" title={t("title", { day: daily.previous_working_day })} count={count} hidden={daily.hidden.done_use_cases + daily.hidden.done_tasks} empty={t("empty")}>
      {daily.done_use_cases.map((item) => (
        <li key={item.code} className="flex flex-col gap-1 py-2" data-testid="pm-done-use-case">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="font-mono text-xs">{item.code}</span>
            <span className="min-w-0 flex-1 basis-48 font-medium [overflow-wrap:anywhere]">{item.title}</span>
            <TaskRef project={project} taskRef={item.task} />
          </div>
          {item.outcome ? <p className="text-[13px] whitespace-pre-line text-muted-foreground [overflow-wrap:anywhere]">{item.outcome}</p> : null}
        </li>
      ))}
      {daily.done_tasks.map((item) => (
        <Row key={item.ref} project={project} taskRef={item.ref} title={item.title} testId="pm-done-task">
          <When value={item.done_at} short />
        </Row>
      ))}
    </Section>
  );
}

function Percent({ value }: { value: number | null | undefined }) {
  const format = useFormatter();
  return <>{value == null ? "-" : format.number(value / 100, { style: "percent", maximumFractionDigits: 0 })}</>;
}

export function SprintSection({ daily }: { daily: PmDaily }) {
  const t = useTranslations("pm.sprint");
  const sprint = daily.sprint;
  return (
    <Section id="sprint" title={t("title")} count={sprint ? 1 : 0} hidden={daily.hidden.sprint} empty={t("empty")}>
      {sprint ? (
        <li className="flex flex-col gap-3 py-2" data-testid="pm-sprint-body">
          <p className="font-medium [overflow-wrap:anywhere]">{sprint.name}</p>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-[13px] sm:grid-cols-4">
            <Fact label={t("done")} value={<Percent value={sprint.done_percent} />} />
            <Fact label={t("time")} value={<Percent value={sprint.time_percent} />} />
            <Fact label={t("scope")} value={String(sprint.scope)} />
            <Fact label={t("changed")} value={t("changedValue", { added: sprint.added, removed: sprint.removed })} />
          </dl>
          {sprint.use_cases.length ? (
            <ul className="flex flex-col gap-1 text-[13px]" aria-label={t("useCases")}>
              {sprint.use_cases.map((item) => (
                <li key={item.code} className="flex flex-wrap items-baseline gap-x-3" data-testid="pm-sprint-use-case">
                  <span className="font-mono text-xs">{item.code}</span>
                  <span className="min-w-0 flex-1 basis-40 [overflow-wrap:anywhere]">{item.title}</span>
                  <span className="tabular-nums text-muted-foreground">{t("ratio", { done: item.done, total: item.total })}</span>
                </li>
              ))}
            </ul>
          ) : null}
        </li>
      ) : null}
    </Section>
  );
}

function Fact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-base font-semibold tabular-nums">{value}</dd>
    </div>
  );
}

export function LateSection({ project, daily }: { project: string; daily: PmDaily }) {
  const t = useTranslations("pm.late");
  return (
    <Section id="late" title={t("title")} count={daily.late.length} hidden={daily.hidden.late} empty={t("empty")}>
      {daily.late.map((item) => (
        <Row key={item.ref} project={project} taskRef={item.ref} title={item.title} testId="pm-late-row">
          {item.kind === "overdue" ? t("overdue", { due: item.due_on, days: item.days_late ?? 0 }) : t("atRisk", { due: item.due_on, days: item.days_left ?? 0 })}
          {item.assignees.length ? ` - ${item.assignees.join(", ")}` : ""}
        </Row>
      ))}
    </Section>
  );
}

export function ReviewSection({ project, daily }: { project: string; daily: PmDaily }) {
  const t = useTranslations("pm.review");
  return (
    <Section id="review" title={t("title")} count={daily.review.length} hidden={daily.hidden.review} empty={t("empty")}>
      {daily.review.map((item) => (
        <Row key={item.ref} project={project} taskRef={item.ref} title={item.title} testId="pm-review-row">
          {t("age", { days: item.age_days })}
          {item.qa.length ? ` - ${t("qa", { who: item.qa.join(", ") })}` : ""}
        </Row>
      ))}
    </Section>
  );
}

export function StaleSection({ project, daily }: { project: string; daily: PmDaily }) {
  const t = useTranslations("pm.stale");
  return (
    <Section id="stale" title={t("title")} count={daily.stale.length} hidden={daily.hidden.stale} empty={t("empty")}>
      {daily.stale.map((item) => (
        <Row key={item.ref} project={project} taskRef={item.ref} title={item.title} testId="pm-stale-row">
          {t("idle", { days: item.idle_days })}
          {item.assignees.length ? ` - ${item.assignees.join(", ")}` : ""}
        </Row>
      ))}
    </Section>
  );
}

/** Who or what a blocker waits on, with its age: the member, a task as a link, or the words given. */
function Waiting({ project, blocker }: { project: string; blocker: PmBlocked }) {
  const t = useTranslations("pm.blocked");
  return (
    <>
      {blocker.waiting_kind === "task" ? (
        <>
          {t("waitingTask")} <TaskRef project={project} taskRef={blocker.waiting_on} />
        </>
      ) : (
        <>
          {blocker.waiting_kind === "member" ? t("waitingMember") : t("waitingText")} <span className="font-medium text-foreground [overflow-wrap:anywhere]">{blocker.waiting_on}</span>
        </>
      )}
      {" - "}
      <span data-testid="pm-blocker-age">{t("age", { days: blocker.age_days })}</span>
    </>
  );
}

function BlockerRow({ project, blocker, id }: { project: string; blocker: PmBlocked | Escalated; id: string }) {
  const t = useTranslations("pm.blocked");
  return (
    <li className="flex flex-col gap-0.5 py-2" data-testid={id}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <TaskRef project={project} taskRef={blocker.ref} />
        <span className="min-w-0 flex-1 basis-48 [overflow-wrap:anywhere]">{blocker.title}</span>
        <span className="text-xs text-muted-foreground">{blocker.kind === "decision" ? t("byDecision") : t("byHand")}</span>
      </div>
      <p className={cn("text-[13px] text-muted-foreground")}>
        <Waiting project={project} blocker={blocker} />
      </p>
      {blocker.reason ? <p className="text-[13px] [overflow-wrap:anywhere]">{blocker.reason}</p> : null}
    </li>
  );
}

export function BlockedSection({ project, daily }: { project: string; daily: PmDaily }) {
  const t = useTranslations("pm.blocked");
  return (
    <Section id="blocked" title={t("title")} count={daily.blocked.length} hidden={daily.hidden.blocked} empty={t("empty")}>
      {daily.blocked.map((item) => (
        <BlockerRow key={`${item.ref}-${item.kind}-${item.since}-${item.waiting_on}`} project={project} blocker={item} id="pm-blocked-row" />
      ))}
    </Section>
  );
}

export function EscalateSection({ project, daily }: { project: string; daily: PmDaily }) {
  const t = useTranslations("pm.escalate");
  return (
    <Section id="escalate" title={t("title")} count={daily.escalate.length} hidden={daily.hidden.escalate} empty={t("empty")}>
      {daily.escalate.map((item) => (
        <BlockerRow key={`${item.ref}-${item.kind}-${item.since}-${item.waiting_on}`} project={project} blocker={item} id="pm-escalate-row" />
      ))}
    </Section>
  );
}

export function LoadSection({ project, daily }: { project: string; daily: PmDaily }) {
  const t = useTranslations("pm.load");
  return (
    <Section id="load" title={t("title")} count={daily.load.length} hidden={0} empty={t("empty")}>
      {daily.load.map((item) => (
        <li key={item.login} className="flex flex-col gap-1 py-2" data-testid="pm-load-row" data-overloaded={item.overloaded} data-empty={item.empty}>
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="font-medium [overflow-wrap:anywhere]">{item.login}</span>
            <span className="text-xs text-muted-foreground">{item.role}</span>
            {item.overloaded ? <StatusPill look={{ tone: "danger", icon: TriangleAlert }} label={t("overloaded")} data-testid="pm-overloaded" /> : null}
            {item.empty ? <StatusPill look={{ tone: "neutral", icon: CircleDashed }} label={t("free")} data-testid="pm-free" /> : null}
          </div>
          <p className="text-[13px] text-muted-foreground tabular-nums">
            {t("counts", { doing: item.doing, limit: item.wip_limit, review: item.review_as_qa, late: item.late })}
          </p>
          {item.doing_tasks.length ? (
            <p className="flex flex-wrap gap-x-3 text-[13px]">
              {item.doing_tasks.map((ref) => (
                <TaskRef key={ref} project={project} taskRef={ref} />
              ))}
            </p>
          ) : null}
        </li>
      ))}
    </Section>
  );
}
