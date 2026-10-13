"use client";

import { queryOptions, useQueries, useQuery } from "@tanstack/react-query";
import { ArrowRight, Inbox } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useCallback, useId, useMemo, useState } from "react";

import { DecisionCard } from "@/components/inbox/decision-view";
import { type Decision, decisionQuery } from "@/components/inbox/queries";
import { browserApi } from "@/lib/api/browser";
import { call } from "@/lib/api/client";
import type { ApiSource } from "@/lib/queries";
import { cn } from "@/lib/utils";

import {
  CHAT_REFRESH_MS,
  decisionAnchor,
  decisionHref,
  isActiveState,
  LIVE_REFRESH_MS,
  openDecisionsQuery,
  type Run,
  type RunChat,
} from "./queries";

/**
 * A plan run's open decisions on its page, at the top of the side column (above the log below the xl breakpoint): the
 * kit's DecisionCard, the same as the Inbox's sheet, with the answer form for the run's owner and the question, context
 * and options for anyone else. The open decisions are read every 5 seconds while the run is active. A decision answered
 * here, or found answered when the hub refused an answer with 409, stays with its answer until the visitor leaves, so the
 * answer and where it went remain on screen after the run moves on. `onTakeOver` shows the page's Terminal tab.
 */
export function RunDecisions({
  run,
  owner,
  onTakeOver,
  className,
}: {
  run: Run;
  owner: boolean;
  onTakeOver?: () => void;
  className?: string;
}) {
  const t = useTranslations("runs.detail.decision");
  const format = useFormatter();
  const ids = useId();
  const active = run.kind === "plan" && isActiveState(run.state);
  const decisions = useQuery({
    ...openDecisionsQuery(browserApi, run.project, run.id),
    enabled: active,
    refetchInterval: LIVE_REFRESH_MS,
  });
  const [settledHere, setSettledHere] = useState<Decision[]>([]);
  const waitingState = run.state === "waiting" || run.state === "parked";
  const keep = (decision: Decision) => setSettledHere((list) => [...list.filter((item) => item.id !== decision.id), decision]);

  const kept = new Set(settledHere.map((decision) => decision.id));
  const open = active ? (decisions.data?.decisions ?? []).filter((decision) => !kept.has(decision.id)) : [];
  // In the order the agent asked them; those answered here after them.
  const shown = [...[...open].sort((a, b) => a.id - b.id), ...settledHere];
  if (shown.length === 0 && !(active && waitingState)) return null;

  const since = run.state === "waiting" ? run.waiting_since : run.state === "parked" ? run.parked_at : null;
  const when = since ? format.dateTime(new Date(since), { dateStyle: "medium", timeStyle: "short" }) : "none";
  const why =
    run.state === "parked" ? (owner ? "parkedYou" : "parked") : run.state === "waiting" ? (owner ? "waitingYou" : "waiting") : owner ? "openYou" : "open";
  const title =
    open.length === 0 && settledHere.length > 0 ? t("titleAnswered") : owner ? t("titleYou") : t("titleOther", { login: run.dispatched_by });
  const first = open[0] ?? null;

  return (
    <section
      aria-labelledby={`${ids}-title`}
      className={cn("flex min-w-0 flex-col gap-3", className)}
      data-testid="run-decisions"
      data-state={run.state}
      data-open={open.length}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 id={`${ids}-title`} className="flex items-center gap-2 text-[15px] leading-[22px] font-semibold">
          {title}
          {open.length > 1 ? (
            <span className="rounded-full bg-attention-soft px-2 text-xs leading-5 font-medium text-attention tabular-nums" data-testid="run-decisions-count">
              {t("count", { count: open.length })}
            </span>
          ) : null}
        </h2>
        {first ? (
          <Link
            href={decisionHref(first.id)}
            className="ml-auto inline-flex min-h-7 shrink-0 items-center gap-1.5 text-[13px] font-medium text-brand underline-offset-4 hover:text-brand-hover hover:underline"
            data-testid="run-decision-link"
          >
            <Inbox className="size-3.5" aria-hidden="true" />
            {t("inInbox")}
            <ArrowRight className="size-3.5" aria-hidden="true" />
          </Link>
        ) : null}
      </div>
      {open.length > 0 || (active && waitingState) ? (
        <p className="text-[13px] leading-[18px] text-pretty text-muted-foreground" data-testid="run-decision-note">
          {t(why, { since: when, login: run.dispatched_by })}
        </p>
      ) : null}
      {shown.length > 0 ? (
        <ol className="flex flex-col gap-3">
          {shown.map((decision) => (
            <li key={decision.id} id={decisionAnchor(decision.id)} tabIndex={-1} className="scroll-mt-20 rounded-md" data-testid="run-decision">
              <LiveDecision decision={decision} settledHere={kept.has(decision.id)} onSettled={keep} onTakeOver={onTakeOver} />
            </li>
          ))}
        </ol>
      ) : decisions.isSuccess ? null : (
        // Read again every 5 seconds; a decision answered meanwhile leaves the list empty, and the note above suffices.
        <p className="text-sm text-muted-foreground" data-testid="run-decisions-loading">
          {decisions.isError ? t("failed") : t("loading")}
        </p>
      )}
    </section>
  );
}

/**
 * One decision of the column: as the run's open decisions list it, and once answered here (or found answered after a
 * 409), as the hub holds it now, so whether the agent got the answer shows when it does. One component either way, so
 * the card keeps its place.
 */
function LiveDecision({
  decision,
  settledHere,
  onSettled,
  onTakeOver,
}: {
  decision: Decision;
  settledHere: boolean;
  onSettled: (decision: Decision) => void;
  onTakeOver?: () => void;
}) {
  const live = useQuery({ ...decisionQuery(browserApi, decision.project, decision.id), initialData: decision, enabled: settledHere });
  return (
    <DecisionCard
      decision={settledHere ? live.data : decision}
      where="run"
      onAnswered={onSettled}
      onConflict={onSettled}
      onTakeOver={onTakeOver}
    />
  );
}

/** The most decisions a run's chat reads of one run, `MAX_LIST` of the API's list. */
const MAX_CHAT_DECISIONS = 200;

/** Every decision of one run, in any state, newest first: an author run's chat shows them all. */
const runDecisionsQuery = (api: ApiSource, project: string, runId: number) =>
  queryOptions({
    queryKey: ["projects", project, "decisions", { run: runId }] as const,
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/decisions", {
          params: { path: { project }, query: { run_id: runId, limit: MAX_CHAT_DECISIONS } },
          signal,
        }),
      ),
  });

export type ChatDecisions = {
  /** The decisions of the chat's runs, in the order asked. */
  list: Decision[];
  /** How many of them wait for an answer. */
  open: number;
  /** A decision as an answer here (or a 409) left it, until the list says it is no longer open. */
  keep: (decision: Decision) => void;
};

/**
 * The decisions of an author run's chat (docs/workers.md, The chat of an author run): those of the run and of each run
 * the chat goes over (a parked run and the run that resumes it), read as often as the chat while it has not ended. The
 * answers are the decisions' own: the chat leaves them out of its messages.
 */
export function useChatDecisions(run: Pick<Run, "id" | "project">, chat: RunChat | undefined): ChatDecisions {
  const ids = useMemo(() => [...new Set([run.id, ...(chat ? [chat.run_id, ...chat.messages.map((message) => message.run_id)] : [])])].sort((a, b) => a - b), [run.id, chat]);
  const live = chat !== undefined && chat.status !== "ended";
  const lists = useQueries({
    queries: ids.map((id) => ({ ...runDecisionsQuery(browserApi, run.project, id), enabled: chat !== undefined, refetchInterval: live ? CHAT_REFRESH_MS : false })),
  });
  const [kept, setKept] = useState<ReadonlyMap<number, Decision>>(new Map());
  const keep = useCallback((decision: Decision) => setKept((map) => new Map(map).set(decision.id, decision)), []);
  const list = lists
    .flatMap((query) => query.data?.decisions ?? [])
    .map((decision) => (decision.state === "open" ? (kept.get(decision.id) ?? decision) : decision))
    .sort((a, b) => a.id - b.id);
  return { list, open: list.filter((decision) => decision.state === "open").length, keep };
}

/**
 * One decision in an author run's chat, under the agent's message that ended the turn which asked it: the kit's
 * DecisionCard as a plan run's side column shows it, the answer form for the run's owner (the agent's pick chosen) and
 * the question, context and options for anyone else; once answered, the compact block with the answer. Its anchor is
 * what the trace's "Asked you" opens.
 */
export function ChatDecision({ decision, onSettled }: { decision: Decision; onSettled: (decision: Decision) => void }) {
  return (
    <div
      id={decisionAnchor(decision.id)}
      tabIndex={-1}
      className="min-w-0 scroll-mt-4 rounded-md sm:ml-10 sm:max-w-[46rem]"
      data-testid="run-chat-decision"
      data-decision-id={decision.id}
      data-state={decision.state}
    >
      <DecisionCard decision={decision} where="run" onAnswered={onSettled} onConflict={onSettled} />
    </div>
  );
}
