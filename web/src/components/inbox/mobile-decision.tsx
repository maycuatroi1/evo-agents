"use client";

import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  Bot,
  ChevronRight,
  CircleCheck,
  CircleX,
  Info,
  ListChecks,
  type LucideIcon,
  MessageSquare,
  RotateCw,
  SearchX,
  User,
} from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, type Ref, type RefObject, useEffect, useMemo, useState } from "react";

import { useLiveQuery } from "@/components/live/live-context";
import { LiveIndicator } from "@/components/live/live-indicator";
import { planHref, stepHref } from "@/components/plans/links";
import { KIND_ICON, useTraceDuration } from "@/components/runs/trace-look";
import { recentEventsQuery, runHref, runQuery } from "@/components/runs/queries";
import { buildTrace } from "@/components/runs/trace-model";
import { ApiErrorState, LoadingState, StatePanel } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { SheetClose, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Ago } from "@/components/workers/ago";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { agentDigest, type DigestLine } from "./agent-digest";
import { DecisionCategoryBadge } from "./badges";
import {
  AnswerFields,
  AnswerHint,
  AnswerSummary,
  AskedTiming,
  ClosedNote,
  DecisionStatePill,
  FoldedContext,
  NoFormNote,
  OptionList,
  TakeOverButton,
  useAnswerForm,
} from "./decision-view";
import { useInboxViewer, useParksAt } from "./hooks";
import { InboxBell } from "./inbox-bell";
import { type AnswerAccess, answerAccess } from "./model";
import { type Decision, decisionQuery } from "./queries";
import { type DecisionTarget, useTargetDecision } from "./target-decision";

/**
 * The kit's MobileDecision: a decision answered on a phone, as a screen of its own over the Inbox (`/inbox?decision=ID`
 * under 768 px, in the Inbox's DecisionSheet). A 52 px top bar with Back to Inbox, the run, whether the page is current
 * and the bell; the question as the screen's h1, the options with 14 px of padding and the note in 16 px text, so the
 * phone does not zoom into it; what the agent did so far, folded; and a bar at the foot, above the phone's safe area,
 * with Take over and Send answer at 44 px. Only the middle scrolls, so the bar stays in reach of the thumb.
 */

/** A link inside a line of text on the screen: the text colour with a quiet underline, brand under the pointer. */
const TEXT_LINK =
  "rounded-xs font-medium text-foreground underline decoration-foreground/30 underline-offset-2 hover:text-brand hover:decoration-current";

/** The 52 px top bar: Back to Inbox, the run, the LiveIndicator and the bell. */
function ScreenTop({ decision }: { decision: Decision | null }) {
  const t = useTranslations("inbox.screen");
  const tDecision = useTranslations("inbox.decision");
  return (
    <div className="flex h-13 shrink-0 items-center gap-1 border-b bg-card px-1" data-testid="decision-screen-top">
      <SheetClose asChild>
        <Button type="button" variant="ghost" size="icon" className="size-11" aria-label={t("back")} title={t("back")} data-testid="decision-back">
          <ArrowLeft className="size-5" aria-hidden="true" />
        </Button>
      </SheetClose>
      {decision ? (
        <Link
          href={runHref(decision.project, decision.run_id)}
          className="block min-w-0 truncate rounded-xs px-1 text-base leading-11 font-semibold text-foreground tabular-nums"
          data-testid="decision-screen-run"
        >
          {tDecision("runLink", { id: decision.run_id })}
        </Link>
      ) : null}
      <div className="ml-auto flex shrink-0 items-center">
        <LiveIndicator />
        <InboxBell />
      </div>
    </div>
  );
}

/** The middle of the screen, the only part that scrolls. */
function ScreenScroll({ id, children }: { id: number; children: ReactNode }) {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain" data-testid="decision-panel" data-decision-id={id}>
      <div className="flex flex-col gap-4 px-4 pt-4 pb-6">{children}</div>
    </div>
  );
}

/**
 * The decision's state, its category, where it comes from and when it was asked (or who answered), then the question as
 * the screen's h1, which names the dialog, and while it is open the agent's context at 15 px.
 */
function ScreenLead({
  decision,
  access,
  parksAt,
  headingRef,
}: {
  decision: Decision;
  access: AnswerAccess;
  parksAt: string | null;
  headingRef: Ref<HTMLHeadingElement>;
}) {
  const t = useTranslations("inbox.decision");
  const open = decision.state === "open";
  return (
    <>
      <div className="flex flex-col gap-1.5" data-testid="decision-head">
        <div className="flex flex-wrap items-center gap-2">
          <DecisionStatePill decision={decision} access={access} size="lg" />
          <DecisionCategoryBadge category={decision.category} />
        </div>
        <p className="text-sm leading-5 text-muted-foreground [overflow-wrap:anywhere]" data-testid="decision-where">
          <span className="font-mono">{decision.project}</span>
          {" / "}
          <Link href={planHref(decision.project, decision.plan_id)} className={cn(TEXT_LINK, "font-mono font-normal")} data-testid="decision-plan-link">
            {decision.plan_id}
          </Link>
          {decision.step_key !== null ? (
            <>
              {", "}
              <Link href={stepHref(decision.project, decision.plan_id, decision.step_key)} className={TEXT_LINK} data-testid="decision-step-link">
                {t("stepLink", { key: decision.step_key })}
              </Link>
            </>
          ) : null}
        </p>
        {open ? (
          <AskedTiming decision={decision} parksAt={parksAt} className="text-[13px] leading-[18px] text-fg-subtle" />
        ) : decision.state === "answered" ? (
          <p className="text-[13px] leading-[18px] text-fg-subtle" data-testid="decision-answered-by">
            {t.rich("answeredBy", {
              login: decision.answered_by ?? decision.owner,
              who: (chunks) => <span className="font-mono text-foreground">{chunks}</span>,
            })}
            {decision.answered_at ? (
              <>
                {","} <Ago value={decision.answered_at} never="-" />
              </>
            ) : null}
          </p>
        ) : null}
      </div>
      <SheetTitle
        asChild
        className="rounded-xs text-xl leading-7 font-semibold whitespace-pre-line text-pretty text-foreground outline-none [overflow-wrap:anywhere] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
      >
        <h1 ref={headingRef} tabIndex={-1} data-testid="decision-question">
          {decision.question.trim()}
        </h1>
      </SheetTitle>
      {open && decision.context ? <FoldedContext text={decision.context} screen /> : null}
    </>
  );
}

/** "Agent", "Shell", "You": who did a line's thing, and the icon beside it. */
function useDigestLine(owner: string) {
  const t = useTranslations("runs.detail.trace");
  const duration = useTraceDuration();
  const { login } = useInboxViewer();
  return (line: DigestLine): { icon: LucideIcon; tone: "danger" | "success" | null; who: string; text: ReactNode; meta: string | null } => {
    switch (line.type) {
      case "agent":
        return {
          icon: Bot,
          tone: null,
          who: t("agent"),
          text: line.text || (line.thoughtMs !== null && line.thoughtMs >= 1000 ? t("thought", { duration: duration(line.thoughtMs) }) : t("thoughtBrief")),
          meta: null,
        };
      case "tool":
        return {
          icon: KIND_ICON[line.kind],
          tone: line.failed ? "danger" : null,
          who: line.name ?? t(`kind.${line.kind}`),
          text: line.arg ? <span className="font-mono text-[13px]">{line.arg}</span> : null,
          meta: line.exitCode !== null && line.exitCode !== 0 ? t("exit", { code: line.exitCode }) : line.status === "failed" ? t("status.failed") : line.ms !== null ? duration(line.ms) : null,
        };
      case "plan":
        return { icon: ListChecks, tone: null, who: t("plan"), text: t("planCount", { done: line.done, total: line.total }), meta: null };
      case "user": {
        const you = line.from !== null && line.from === login;
        const who = you ? t("you") : (line.from ?? t("someone"));
        const head = line.decisionId === null ? who : you ? t("youAnswered", { id: line.decisionId }) : t("answered", { login: who, id: line.decisionId });
        return { icon: User, tone: null, who: head, text: line.text, meta: null };
      }
      case "system":
        return {
          icon: line.tone === "ok" ? CircleCheck : line.tone === "error" ? CircleX : Info,
          tone: line.tone === "ok" ? "success" : line.tone === "error" ? "danger" : null,
          who: "",
          text: line.text,
          meta: null,
        };
      case "ask":
        return { icon: MessageSquare, tone: null, who: login === owner ? t("askedYou") : t("asked", { login: owner }), text: line.question, meta: null };
    }
  };
}

function DigestItem({ line, owner }: { line: DigestLine; owner: string }) {
  const format = useFormatter();
  const view = useDigestLine(owner)(line);
  const Icon = view.icon;
  const at = new Date(line.at);
  return (
    <li className="grid grid-cols-[16px_minmax(0,1fr)_auto] items-start gap-x-2.5" data-testid="decision-so-far-item" data-type={line.type}>
      <Icon
        className={cn("mt-0.5 size-4", view.tone === "danger" ? "text-danger" : view.tone === "success" ? "text-success" : "text-fg-subtle")}
        aria-hidden="true"
      />
      <p className="line-clamp-2 min-w-0 text-sm leading-5 text-muted-foreground [overflow-wrap:anywhere]">
        {view.who ? <span className="font-medium text-foreground">{view.who}</span> : null}
        {view.who && view.text ? " " : null}
        {view.text}
        {view.meta ? <span className={cn("text-fg-subtle tabular-nums", view.tone === "danger" && "text-danger")}>{`, ${view.meta}`}</span> : null}
      </p>
      <time dateTime={line.at} title={format.dateTime(at, { dateStyle: "full", timeStyle: "long" })} className="mt-0.5 font-mono text-xs leading-4 text-fg-subtle tabular-nums">
        {format.dateTime(at, { timeStyle: "short" })}
      </time>
    </li>
  );
}

/**
 * "What the agent did so far", folded: opened, it reads the run's latest events and lists the last five things its
 * Trace shows (`agentDigest`), oldest first, then links to the run's page for the whole trace. Nothing is read until it
 * opens.
 */
export function AgentSoFar({ decision }: { decision: Decision }) {
  const t = useTranslations("inbox.screen");
  const [opened, setOpened] = useState(false);
  const run = useQuery({ ...runQuery(browserApi, decision.project, decision.run_id), enabled: opened });
  const lastSeq = run.data?.last_seq ?? null;
  const events = useQuery({
    ...recentEventsQuery(browserApi, decision.project, decision.run_id, lastSeq ?? 0),
    enabled: opened && lastSeq !== null && lastSeq > 0,
  });
  const lines = useMemo(() => (events.data ? agentDigest(buildTrace(events.data.events), decision.id) : null), [events.data, decision.id]);
  const failed = run.isError || events.isError;

  let body: ReactNode;
  if (lastSeq === 0 || (lines !== null && lines.length === 0)) {
    body = <p className="text-sm leading-5 text-muted-foreground" data-testid="decision-so-far-empty">{t("soFarEmpty")}</p>;
  } else if (lines !== null) {
    body = (
      <ol className="flex flex-col gap-2.5" aria-label={t("soFarList")} data-testid="decision-so-far-list">
        {lines.map((line) => (
          <DigestItem key={line.key} line={line} owner={decision.owner} />
        ))}
      </ol>
    );
  } else if (failed) {
    body = (
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-danger" data-testid="decision-so-far-error">
        <span>{t("soFarError")}</span>
        <Button type="button" variant="outline" size="sm" onClick={() => void (run.isError ? run.refetch() : events.refetch())}>
          <RotateCw aria-hidden="true" />
          {t("soFarRetry")}
        </Button>
      </div>
    );
  } else {
    body = (
      <LoadingState>
        <div className="flex flex-col gap-3">
          {[0, 1, 2].map((index) => (
            <div key={index} className="flex items-center gap-2.5">
              <Skeleton className="size-4 rounded-xs" />
              <Skeleton className="h-3.5 flex-1 rounded-xs" />
            </div>
          ))}
        </div>
      </LoadingState>
    );
  }

  return (
    <details
      className="group/sofar overflow-hidden rounded-md border bg-card"
      onToggle={(event) => {
        if (event.currentTarget.open) setOpened(true);
      }}
      data-testid="decision-so-far"
    >
      <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 px-4 py-2.5 text-sm font-medium text-foreground transition-colors select-none hover:bg-accent [&::-webkit-details-marker]:hidden">
        <ChevronRight
          className="size-4 shrink-0 text-fg-subtle transition-transform duration-base group-open/sofar:rotate-90 motion-reduce:transition-none"
          aria-hidden="true"
        />
        {t("soFar")}
      </summary>
      <div className="flex flex-col gap-3 border-t px-4 pt-3 pb-1">
        {opened ? body : null}
        <Link
          href={runHref(decision.project, decision.run_id)}
          className="inline-flex min-h-11 items-center self-start text-sm font-medium text-brand underline-offset-4 hover:text-brand-hover hover:underline"
          data-testid="decision-so-far-run"
        >
          {t("soFarOpen")}
        </Link>
      </div>
    </details>
  );
}

/** The owner's screen: the answer's fields in the middle, Take over and Send answer in the bar at the foot. */
function AnswerScreen({
  decision,
  access,
  parksAt,
  headingRef,
}: {
  decision: Decision;
  access: AnswerAccess;
  parksAt: string | null;
  headingRef: Ref<HTMLHeadingElement>;
}) {
  const t = useTranslations("inbox.answer");
  const form = useAnswerForm(decision);
  const formId = `${form.ids}-form`;
  return (
    <>
      <ScreenScroll id={decision.id}>
        <ScreenLead decision={decision} access={access} parksAt={parksAt} headingRef={headingRef} />
        <form id={formId} {...form.formProps} className="flex flex-col gap-3">
          <AnswerFields form={form} screen />
          <AnswerHint form={form} className="text-[13px] leading-[18px]" />
        </form>
        <AgentSoFar decision={decision} />
      </ScreenScroll>
      <div
        className="flex shrink-0 gap-2 border-t bg-card px-4 pt-3 pb-[calc(0.75rem+env(safe-area-inset-bottom,0px))]"
        data-testid="decision-bar"
      >
        {form.terminalHref ? <TakeOverButton decision={decision} href={form.terminalHref} className="h-11 flex-1 text-[15px]" /> : null}
        <Button type="submit" form={formId} busy={form.pending} className="h-11 flex-1 text-[15px]" data-testid="decision-send">
          {form.pending ? t("sending") : t("send")}
        </Button>
      </div>
    </>
  );
}

/**
 * A loaded decision on the screen: the owner's answer form while it is open, otherwise what was answered or why there
 * is no form. Once the owner's answer lands the form is gone; focus, which went with it, comes back to the question.
 */
function ScreenDecision({
  decision,
  knownParksAt,
  headingRef,
  contentRef,
}: {
  decision: Decision;
  knownParksAt: string | null | undefined;
  headingRef: RefObject<HTMLHeadingElement | null>;
  contentRef: RefObject<HTMLDivElement | null>;
}) {
  const { login } = useInboxViewer();
  const access = answerAccess(decision, login);
  const parksAt = useParksAt(decision, knownParksAt);
  useLiveQuery(decisionQuery(browserApi, decision.project, decision.id), true);
  useEffect(() => {
    const active = document.activeElement;
    if (active === null || active === document.body || active === contentRef.current) headingRef.current?.focus();
  }, [access, headingRef, contentRef]);

  if (access === "answer") return <AnswerScreen decision={decision} access={access} parksAt={parksAt} headingRef={headingRef} />;
  const open = decision.state === "open";
  return (
    <ScreenScroll id={decision.id}>
      <ScreenLead decision={decision} access={access} parksAt={parksAt} headingRef={headingRef} />
      {decision.state === "answered" ? <AnswerSummary decision={decision} /> : null}
      <ClosedNote decision={decision} />
      {open ? <OptionList decision={decision} screen /> : null}
      {open && (access === "notOwner" || access === "runGone") ? <NoFormNote decision={decision} access={access} /> : null}
      <AgentSoFar decision={decision} />
    </ScreenScroll>
  );
}

/** The screen in the Inbox's DecisionSheet under 768 px: its top bar, then the decision or why it is not there. */
export function DecisionScreen({
  target,
  headingRef,
  contentRef,
}: {
  target: DecisionTarget;
  headingRef: RefObject<HTMLHeadingElement | null>;
  contentRef: RefObject<HTMLDivElement | null>;
}) {
  const t = useTranslations("inbox.sheet");
  const { decision, notFound, error, retry } = useTargetDecision(target, headingRef, contentRef);
  const data = notFound ? undefined : decision.data;

  let body: ReactNode;
  if (notFound) {
    body = (
      <ScreenScroll id={target.id}>
        <StatePanel icon={SearchX} title={t("notFoundTitle", { id: target.id })} description={t("notFoundDescription")} testId="decision-not-found" />
      </ScreenScroll>
    );
  } else if (data) {
    body = <ScreenDecision decision={data} knownParksAt={target.parksAt} headingRef={headingRef} contentRef={contentRef} />;
  } else if (error) {
    body = (
      <ScreenScroll id={target.id}>
        <ApiErrorState error={error.info} onRetry={retry} />
      </ScreenScroll>
    );
  } else {
    body = (
      <ScreenScroll id={target.id}>
        <LoadingState>
          <div className="flex flex-col gap-4">
            <Skeleton className="h-6 w-40" />
            <Skeleton className="h-7 w-full" />
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-22 w-full" />
          </div>
        </LoadingState>
      </ScreenScroll>
    );
  }
  return (
    <>
      <ScreenTop decision={data ?? null} />
      {/* The question names the screen once it shows; until then, the decision's number. */}
      {data ? null : <SheetTitle className="sr-only">{t("title", { id: target.id })}</SheetTitle>}
      {body}
    </>
  );
}
