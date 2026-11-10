"use client";

import { ChevronDown, RotateCw } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type ReactNode, useId, useState } from "react";

import { useMoney } from "@/components/data/facts";
import { NAME_LINK } from "@/components/data/identifier";
import { InlineError } from "@/components/feedback/notice";
import { type DecisionStatus, STATUS_LOOKS, StatusBadge } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useMediaQuery } from "@/hooks/use-media-query";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { answerable, threadHref, type WorkAsk, type WorkCard, type WorkDecision } from "@/lib/work";
import { cn } from "@/lib/utils";

import { AskStatus } from "./work-ask";
import { useWaitingLine, useWorkTime } from "./work-look";
import { useOpenThread } from "./open-thread";

/** Tailwind's md (48rem): from there the card opens with its details shown; below, folded to its figures. */
const OPEN_QUERY = "(min-width: 48rem)";

export type WorkCardState = { status: "pending" } | { status: "error"; error: ApiErrorInfo; retry: () => void } | { status: "success"; card: WorkCard };

/**
 * The work card pinned over a work chat's messages (web/DESIGN.md, Work card): what the work waits for, its runs, cost,
 * deepest hop and time against its revision's limits, and, unfolded, its goal and when it is done, the sub-tasks (each
 * row opens the thread of the agent asked) and the decisions. `answer` draws the answer form of an open decision that
 * asks the visitor, so it is answered in the card. From 768 px the card opens unfolded, below it folded to its figures.
 */
export function WorkPinned({ state, chatId, me, answer }: { state: WorkCardState; chatId: number; me: string | null; answer?: (decision: WorkDecision) => ReactNode }) {
  const t = useTranslations("chats.work.card");
  const wide = useMediaQuery(OPEN_QUERY);
  const [toggled, setToggled] = useState<boolean | null>(null);
  const ids = useId();
  const unfolded = toggled ?? wide;
  if (state.status === "pending") {
    return (
      <div className="flex flex-col gap-2 border-b px-3 py-3 sm:px-4" data-testid="work-card-loading">
        <Skeleton className="h-4 w-40 rounded-xs" />
        <Skeleton className="h-3 w-64 rounded-xs" />
      </div>
    );
  }
  if (state.status === "error") {
    const { status } = state.error;
    const text = status === 404 ? t("notFound") : status === 403 ? t("forbidden") : t("failed");
    return (
      <div className="flex flex-wrap items-start gap-2 border-b px-3 py-3 sm:px-4" data-testid="work-card-error" data-status={status}>
        <div className="min-w-0 flex-1">
          <InlineError text={text} detail={status === 404 || status === 403 ? null : state.error.message} requestId={state.error.requestId} />
        </div>
        {status !== 404 && status !== 403 ? (
          <Button type="button" variant="outline" size="sm" onClick={state.retry} data-testid="work-card-retry">
            <RotateCw aria-hidden="true" />
            {t("retry")}
          </Button>
        ) : null}
      </div>
    );
  }
  const { card } = state;
  return (
    <section aria-labelledby={`${ids}-title`} className="flex flex-col gap-2 border-b bg-surface-sunken/60 px-3 py-3 sm:px-4" data-testid="work-card" data-status={card.status}>
      <div className="flex min-w-0 items-center gap-2">
        <h2 id={`${ids}-title`} className="shrink-0 text-sm font-semibold">
          {t("title")}
        </h2>
        <WaitingText card={card} />
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="ml-auto shrink-0"
          aria-expanded={unfolded}
          aria-controls={`${ids}-body`}
          onClick={() => setToggled(!unfolded)}
          data-testid="work-card-toggle"
        >
          <span className="max-sm:sr-only">{unfolded ? t("hide") : t("show")}</span>
          <ChevronDown className={cn("transition-transform", unfolded && "rotate-180")} aria-hidden="true" />
        </Button>
      </div>
      <Figures card={card} />
      <div id={`${ids}-body`} hidden={!unfolded} className="flex max-h-[40vh] flex-col gap-3 overflow-y-auto" data-testid="work-card-body">
        <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-[13px] leading-[18px] sm:grid-cols-[max-content_minmax(0,1fr)]">
          <dt className="text-muted-foreground">{t("goal")}</dt>
          <dd className="[overflow-wrap:anywhere]" data-testid="work-card-goal">
            {card.goal ?? <span className="text-fg-subtle italic">{t("noGoal")}</span>}
          </dd>
          <dt className="text-muted-foreground max-sm:mt-1">{t("doneWhen")}</dt>
          <dd className="[overflow-wrap:anywhere]" data-testid="work-card-done-when">
            {card.done_when ?? <span className="text-fg-subtle italic">{t("noDoneWhen")}</span>}
          </dd>
        </dl>
        <SubTasks asks={card.asks} chatId={chatId} />
        <Decisions decisions={card.decisions} me={me} answer={answer} />
      </div>
    </section>
  );
}

function WaitingText({ card }: { card: WorkCard }) {
  const waitingLine = useWaitingLine();
  return (
    <p className="min-w-0 flex-1 truncate text-[13px] leading-[18px] text-muted-foreground" title={waitingLine(card)} data-testid="work-card-waiting">
      {waitingLine(card)}
    </p>
  );
}

function Figures({ card }: { card: WorkCard }) {
  const t = useTranslations("chats.work.card");
  const money = useMoney();
  const time = useWorkTime();
  const figures = [
    { key: "runs", label: t("runs"), value: t("of", { value: card.runs, max: card.trace_runs }) },
    { key: "cost", label: t("cost"), value: t("of", { value: money(card.usd), max: money(card.trace_usd) }) },
    { key: "hop", label: t("hop"), value: t("of", { value: card.hop ?? 0, max: card.max_hops }) },
    { key: "time", label: t("time"), value: time(card.seconds) },
  ];
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-4" data-testid="work-card-figures">
      {figures.map((figure) => (
        <div key={figure.key} className="flex min-w-0 flex-col">
          <dt className="text-xs text-muted-foreground">{figure.label}</dt>
          <dd className="truncate text-sm font-medium tabular-nums" data-testid={`work-card-${figure.key}`}>
            {figure.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function SubTasks({ asks, chatId }: { asks: WorkAsk[]; chatId: number }) {
  const t = useTranslations("chats.work.card");
  const money = useMoney();
  const openThread = useOpenThread();
  const ids = useId();
  return (
    <div className="flex flex-col gap-1">
      <h3 id={`${ids}-subtasks`} className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {t("subTasks")}
      </h3>
      {asks.length === 0 ? (
        <p className="text-[13px] text-fg-subtle" data-testid="work-card-no-subtasks">
          {t("noSubTasks")}
        </p>
      ) : (
        <Table aria-labelledby={`${ids}-subtasks`} className="text-[13px]" data-testid="work-card-subtasks">
          <TableHeader>
            <TableRow>
              <TableHead>{t("agent")}</TableHead>
              <TableHead>{t("status")}</TableHead>
              <TableHead className="max-sm:hidden">{t("brief")}</TableHead>
              <TableHead className="text-right">{t("cost")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {asks.map((ask, index) => {
              const thread = ask.thread;
              const here = thread !== null && thread.chat_id === chatId && openThread !== null;
              const name = ask.agent ? `@${ask.agent}` : t("refused");
              const label = t("openThreadOf", { agent: ask.agent ?? ask.from_agent });
              return (
                <TableRow key={`${ask.run_id ?? "none"}-${index}`} data-testid="work-subtask" data-agent={ask.agent ?? undefined}>
                  <TableCell className="align-top">
                    {here ? (
                      <button type="button" className={`${NAME_LINK} font-mono text-[13px]`} aria-label={label} onClick={() => openThread(thread.root_id)} data-testid="work-subtask-open">
                        {name}
                      </button>
                    ) : thread ? (
                      <Link href={threadHref(thread)} className={`${NAME_LINK} font-mono text-[13px]`} aria-label={label} data-testid="work-subtask-open">
                        {name}
                      </Link>
                    ) : (
                      <span className="font-mono">{name}</span>
                    )}
                    {ask.brief ? <p className="mt-0.5 line-clamp-2 text-xs whitespace-normal text-muted-foreground sm:hidden">{ask.brief}</p> : null}
                  </TableCell>
                  <TableCell className="align-top">
                    <AskStatus status={ask.status} />
                  </TableCell>
                  <TableCell className="max-w-[18rem] align-top whitespace-normal max-sm:hidden">
                    <span className="line-clamp-2 [overflow-wrap:anywhere]">{ask.brief ?? "-"}</span>
                  </TableCell>
                  <TableCell className="text-right align-top tabular-nums">{money(ask.usd)}</TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}
    </div>
  );
}

function Decisions({ decisions, me, answer }: { decisions: WorkDecision[]; me: string | null; answer?: (decision: WorkDecision) => ReactNode }) {
  const t = useTranslations("chats.work.card");
  if (decisions.length === 0) return null;
  return (
    <div className="flex flex-col gap-1.5">
      <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{t("decisions")}</h3>
      <ul className="flex flex-col gap-2" data-testid="work-card-decisions">
        {decisions.map((decision) => (
          <li key={`${decision.kind}-${decision.id}`} className="flex min-w-0 flex-col gap-1.5" data-testid="work-decision" data-kind={decision.kind} data-state={decision.state}>
            {answerable(decision, me) && answer ? (
              answer(decision)
            ) : (
              <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[13px] leading-[18px]">
                {decision.kind === "person" && decision.state in STATUS_LOOKS.decision ? (
                  <StatusBadge kind="decision" status={decision.state as DecisionStatus} />
                ) : (
                  <span className="text-xs font-medium text-muted-foreground">{t("orchestrator")}</span>
                )}
                <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">{decision.question ?? decision.rule ?? ""}</span>
                {decision.asked_of ? <span className="text-xs text-muted-foreground">{t("askedOf", { login: decision.asked_of })}</span> : null}
                {decision.answer ? <span className="basis-full text-muted-foreground [overflow-wrap:anywhere]">{t("answered", { answer: decision.answer })}</span> : null}
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
