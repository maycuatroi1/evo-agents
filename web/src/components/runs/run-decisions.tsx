"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Inbox } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { DecisionCard } from "@/components/inbox/decision-view";
import { type Decision, decisionQuery } from "@/components/inbox/queries";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { decisionHref, isActiveState, LIVE_REFRESH_MS, openDecisionsQuery, type Run } from "./queries";

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
            <li key={decision.id} data-testid="run-decision">
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
