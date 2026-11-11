"use client";

import { Check } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import Link from "next/link";
import { type ReactNode, type Ref, useId } from "react";

import { Ago } from "@/components/data/ago";
import { projectHref } from "@/components/shell/nav";
import { useWaitSpan, WaitPill } from "@/components/status/wait-pill";
import { useNow } from "@/hooks/use-now";
import { cn } from "@/lib/utils";
import { actionsState, isOver, type WaitKind, waitSubject, workflowProgress } from "@/lib/waits";

import { AnsweredBy } from "./answered-by";
import { DecisionCategoryBadge } from "./badges";
import { ClosedNote } from "./decision-notes";
import { useInboxViewer } from "./hooks";
import { answerAccess, runLinkWords } from "./model";
import type { Decision } from "./queries";
import { WaitActions } from "./wait-actions";
import { WaitSource } from "./wait-sources";

const HEAD_LINK =
  "rounded-xs font-medium text-foreground underline decoration-foreground/30 underline-offset-2 hover:text-brand hover:decoration-current";

/**
 * A wait as its panel (web/DESIGN.md, The wait panel): what the run waits for and the agent's reason, a timeline from
 * the ask to the agent going on, the source card of what it waits for, when it wakes the agent and at the latest when,
 * and the wait as the hub keeps it, folded. A wait asks nobody anything, so nothing in it is `attention`: its head is
 * neutral, its pill says what it waits for, and the run's owner gets Go on now and Stop waiting as secondary buttons,
 * each behind a confirmation. DecisionCard shows a wait this way wherever it sits: a run's page, the Inbox's sheet,
 * Home's sheet and an author run's chat.
 */
export function WaitCard({
  decision,
  where,
  headingRef,
  flush = false,
  parkedAt = null,
  onAnswered,
  onConflict,
}: {
  decision: Decision;
  where: "inbox" | "run";
  headingRef?: Ref<HTMLHeadingElement>;
  flush?: boolean;
  /** When the run parked, if the caller knows it (the run's page). */
  parkedAt?: string | null;
  onAnswered?: (decision: Decision) => void;
  onConflict?: (decision: Decision) => void;
}) {
  const t = useTranslations("wait");
  const ids = useId();
  const { login } = useInboxViewer();
  const access = answerAccess(decision, login);
  const open = decision.state === "open";
  const now = useNow(open);
  const on = decision.waiting_on ?? null;
  const kind: WaitKind = on?.kind ?? "time";
  const wakeAt = decision.wake_at ?? null;
  const Heading = where === "inbox" ? "h2" : "h3";
  const headline = open
    ? t(`headline.${kind}`)
    : decision.state !== "answered"
      ? t("headline.closed")
      : decision.answered_by === null
        ? t("headline.came")
        : decision.answer_option === "stop"
          ? t("headline.stopped")
          : decision.answered_by === login
            ? t("headline.youWoke")
            : t("headline.woke", { login: decision.answered_by });

  return (
    <article
      aria-labelledby={`${ids}-headline`}
      className={cn("flex min-w-0 flex-col", flush ? "" : "overflow-hidden rounded-md border bg-card shadow-raised")}
      data-testid="decision"
      data-wait={kind}
      data-decision-id={decision.id}
      data-state={decision.state}
    >
      <WaitHead decision={decision} where={where} />
      <div className="flex flex-col gap-4 px-4 pt-4 pb-4">
        <div className="flex flex-col gap-1.5">
          <Heading
            id={`${ids}-headline`}
            ref={headingRef}
            tabIndex={headingRef ? -1 : undefined}
            className="rounded-xs text-[15px] leading-[22px] font-semibold text-pretty outline-none [overflow-wrap:anywhere] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
            data-testid="wait-headline"
          >
            {headline}
          </Heading>
          <p className="text-[13px] leading-[19px] whitespace-pre-line text-pretty [overflow-wrap:anywhere]" data-testid="wait-reason">
            <span className="text-muted-foreground">{t("reason")} </span>
            {decision.question.trim()}
          </p>
          {open ? <p className="text-[13px] leading-[18px] text-pretty text-muted-foreground" data-testid="wait-lead">{t("lead")}</p> : null}
        </div>
        <WaitTimeline decision={decision} kind={kind} parkedAt={parkedAt} now={now} />
        <WaitSource on={on} wakeAt={wakeAt} project={decision.project} now={now} />
        <WaitBoxes decision={decision} kind={kind} now={now} />
        <ClosedNote decision={decision} />
        {open && access !== "answer" ? (
          <p className="text-[13px] leading-[18px] text-pretty text-muted-foreground" data-testid="wait-nobody">
            {t("nobody", { owner: decision.asked_of ?? decision.owner })}
          </p>
        ) : null}
        <details className="group rounded-md border" data-testid="wait-raw">
          <summary className="flex min-h-9 cursor-pointer items-center px-3 text-[13px] font-medium text-muted-foreground hover:text-foreground max-md:min-h-11">
            {t("raw")}
          </summary>
          <pre className="max-h-72 overflow-auto border-t bg-surface-sunken p-3 font-mono text-xs leading-[18px] whitespace-pre-wrap [overflow-wrap:anywhere]">
            {JSON.stringify({ waiting_on: on, wake_at: wakeAt, wake_on: decision.wake_on ?? null }, null, 2)}
          </pre>
        </details>
      </div>
      {access === "answer" ? <WaitActions key={decision.id} decision={decision} flush={flush} onAnswered={onAnswered} onConflict={onConflict} /> : null}
    </article>
  );
}

/** The panel's head, neutral: the pill naming what the run waits for, the category, where it comes from, and when. */
function WaitHead({ decision, where }: { decision: Decision; where: "inbox" | "run" }) {
  const t = useTranslations("inbox.decision");
  const step = decision.plan_id !== null && decision.step_key !== null ? projectHref(decision.project, `plans/${encodeURIComponent(decision.plan_id)}/steps/${encodeURIComponent(decision.step_key)}`) : null;
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 border-b bg-surface-sunken px-4 py-2.5 text-xs text-muted-foreground" data-testid="decision-head">
      {decision.state === "open" ? <WaitPill subject={waitSubject(decision.waiting_on, decision.wake_at ?? null)} className="max-w-full" /> : null}
      <DecisionCategoryBadge category={decision.category} />
      {where === "inbox" ? (
        <>
          <Link href={projectHref(decision.project, `runs/${decision.run_id}`)} className={cn(HEAD_LINK, "tabular-nums")} data-testid="decision-run-link">
            {t(runLinkWords(decision), { id: decision.run_id })}
          </Link>
          <span className="min-w-0 font-mono [overflow-wrap:anywhere]">{decision.plan_id ? `${decision.project} / ${decision.plan_id}` : decision.project}</span>
        </>
      ) : null}
      {step ? (
        <Link href={step} className={HEAD_LINK} data-testid="decision-step-link">
          {t("stepLink", { key: decision.step_key ?? "" })}
        </Link>
      ) : null}
      {decision.state === "answered" ? (
        <span className="ml-auto" data-testid="decision-answered-by">
          <AnsweredBy decision={decision} />
          {decision.answered_at ? (
            <>
              {","} <Ago value={decision.answered_at} never="-" />
            </>
          ) : null}
        </span>
      ) : (
        <span className="ml-auto" data-testid="decision-timing">
          {t.rich("asked", { time: () => <Ago value={decision.asked_at} never="-" /> })}
        </span>
      )}
    </div>
  );
}

type NodeState = "done" | "current" | "pending";

function Node({ state, title, sub, last = false, testId }: { state: NodeState; title: string; sub: ReactNode; last?: boolean; testId: string }) {
  return (
    <li className="relative flex min-w-0 flex-1 gap-3 sm:flex-col sm:gap-2" data-testid={testId} data-state={state} aria-current={state === "current" ? "step" : undefined}>
      <span className="flex shrink-0 flex-col items-center sm:flex-row">
        <span
          className={cn(
            "flex size-5 shrink-0 items-center justify-center rounded-full border-2",
            state === "done" ? "border-foreground bg-foreground text-background" : state === "current" ? "border-running bg-running-soft" : "border-dashed border-border-strong bg-card",
          )}
          aria-hidden="true"
        >
          {state === "done" ? <Check className="size-3" strokeWidth={3} /> : state === "current" ? <span className="size-2 animate-pulse rounded-full bg-running motion-reduce:animate-none" /> : null}
        </span>
        {last ? null : <span className="min-h-4 w-0.5 flex-1 bg-border sm:h-0.5 sm:min-h-0 sm:w-auto sm:flex-1" aria-hidden="true" />}
      </span>
      <span className="flex min-w-0 flex-col pb-3 sm:pb-0 sm:pr-2">
        <span className="text-[13px] leading-[18px] font-medium">{title}</span>
        <span className="text-xs leading-[16px] text-pretty text-muted-foreground [overflow-wrap:anywhere]">{sub}</span>
      </span>
    </li>
  );
}

/** Asked, Parked, the live source, the agent goes on: where the wait stands, as four nodes. */
function WaitTimeline({ decision, kind, parkedAt, now }: { decision: Decision; kind: WaitKind; parkedAt: string | null; now: number | null }) {
  const t = useTranslations("wait.timeline");
  const format = useFormatter();
  const span = useWaitSpan();
  const open = decision.state === "open";
  const parked = parkedAt !== null || decision.run_state === "parked";
  const run = decision.waiting_on?.workflow_run ?? null;
  const progress = run ? workflowProgress(run, now) : null;
  const sourceSub = progress
    ? progress.elapsedMs !== null
      ? t("sourceSpan", { state: t(`actions.${progress.state}`), elapsed: span(progress.elapsedMs) })
      : t(`actions.${progress.state}`)
    : open
      ? t("notYet")
      : decision.answered_by === null && decision.state === "answered"
        ? t("came")
        : t("over");
  const sourceDone = !open || (progress !== null && isOver(actionsState(run?.status ?? null, run?.conclusion ?? null)));
  const latest = decision.wake_at ? format.dateTime(new Date(decision.wake_at), { dateStyle: "medium", timeStyle: "short" }) : null;
  const goesOn = open
    ? latest
      ? t("goesOnWhen", { time: latest })
      : t("goesOnCome")
    : decision.state !== "answered"
      ? t("ended")
      : decision.answered_by === null
        ? t("wokeHub")
        : decision.answer_option === "stop"
          ? t("saidStop", { login: decision.answered_by })
          : t("saidNow", { login: decision.answered_by });
  return (
    <ol className="flex flex-col sm:flex-row sm:gap-2" aria-label={t("label")} data-testid="wait-timeline">
      <Node
        state="done"
        title={t("asked")}
        sub={decision.step_key !== null ? t("askedStep", { step: decision.step_key }) : t("askedAgent")}
        testId="wait-node-asked"
      />
      <Node state={parked ? "done" : open ? "current" : "pending"} title={t("parked")} sub={parked ? t("parkedNote") : open ? t("parksSoon") : t("notParked")} testId="wait-node-parked" />
      <Node state={sourceDone ? "done" : parked || !open ? "current" : "pending"} title={t(`source.${kind}`, { name: run?.workflow ?? run?.title ?? "" })} sub={sourceSub} testId="wait-node-source" />
      <Node state={open ? "pending" : "done"} title={open ? t("goesOn") : t("wentOn")} sub={goesOn} last testId="wait-node-goes-on" />
    </ol>
  );
}

/** When the hub wakes the agent, and at the latest when; once it woke, when and by whom. */
function WaitBoxes({ decision, kind, now }: { decision: Decision; kind: WaitKind; now: number | null }) {
  const t = useTranslations("wait.boxes");
  const format = useFormatter();
  const span = useWaitSpan();
  const on = decision.waiting_on;
  const when =
    kind === "plan_step" && on?.plan_step
      ? t("when.plan_step", { plan: on.plan_step.plan, step: on.plan_step.step, wanted: on.plan_step.wanted ?? "done" })
      : kind === "agent_answer" && on?.agent_answer?.run
        ? t("when.agent_run", { run: on.agent_answer.run.id })
        : kind === "event" && on?.event
          ? t("when.event", { name: on.event.name ?? on.event.kind })
          : t(`when.${kind}`);
  const at = decision.wake_at ? new Date(decision.wake_at) : null;
  const left = at && now !== null ? at.getTime() - now : null;
  const answered = decision.state === "answered";
  return (
    <dl className="grid gap-2 sm:grid-cols-2" data-testid="wait-boxes">
      <Box title={t("wakesWhen")} testId="wait-wakes-when">
        {when}
      </Box>
      {answered && decision.answered_at ? (
        <Box title={t("woke")} testId="wait-woke">
          {format.dateTime(new Date(decision.answered_at), { dateStyle: "medium", timeStyle: "short" })}
          {", "}
          {decision.answered_by === null ? t("byHub") : t("byLogin", { login: decision.answered_by })}
        </Box>
      ) : at ? (
        <Box title={t("atLatest")} testId="wait-at-latest">
          {t("latest", { time: format.dateTime(at, { dateStyle: "medium", timeStyle: "short" }), utc: format.dateTime(at, { timeStyle: "short", timeZone: "UTC" }) })}
          {left !== null && left > 0 ? <span className="text-muted-foreground tabular-nums">{`, ${t("in", { duration: span(left) })}`}</span> : null}
        </Box>
      ) : null}
    </dl>
  );
}

function Box({ title, children, testId }: { title: string; children: ReactNode; testId: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 rounded-md bg-surface-sunken px-3 py-2" data-testid={testId}>
      <dt className="text-xs text-muted-foreground">{title}</dt>
      <dd className="text-[13px] leading-[18px] [overflow-wrap:anywhere]">{children}</dd>
    </div>
  );
}
