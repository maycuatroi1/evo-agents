"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Inbox, MessageCircleQuestionMark } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { DecisionView } from "@/components/inbox/decision-view";
import { type Decision, decisionQuery } from "@/components/inbox/queries";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { decisionHref, isActiveState, LIVE_REFRESH_MS, openDecisionsQuery, type Run } from "./queries";

/**
 * A plan run's open decisions on its page, above the log: "Waiting for your decision" with the same answer form as the
 * Inbox for the run's owner, the question, context and options for anyone else. The open decisions are read every
 * 5 seconds while the run is active. A decision answered here stays in the banner with its answer until the visitor
 * leaves, so the answer and where it went remain on screen after the run moves on.
 */
export function RunDecisions({ run, owner }: { run: Run; owner: boolean }) {
  const t = useTranslations("runs.detail.decision");
  const format = useFormatter();
  const ids = useId();
  const active = run.kind === "plan" && isActiveState(run.state);
  const decisions = useQuery({
    ...openDecisionsQuery(browserApi, run.project, run.id),
    enabled: active,
    refetchInterval: LIVE_REFRESH_MS,
  });
  const [answeredHere, setAnsweredHere] = useState<Decision[]>([]);
  const waitingState = run.state === "waiting" || run.state === "parked";

  const kept = new Set(answeredHere.map((decision) => decision.id));
  const open = active ? (decisions.data?.decisions ?? []).filter((decision) => !kept.has(decision.id)) : [];
  // In the order the agent asked them; those answered here after them.
  const shown = [...[...open].sort((a, b) => a.id - b.id), ...answeredHere];
  if (shown.length === 0 && !(active && waitingState)) return null;

  const since = run.state === "waiting" ? run.waiting_since : run.state === "parked" ? run.parked_at : null;
  const when = since ? format.dateTime(new Date(since), { dateStyle: "medium", timeStyle: "short" }) : "none";
  const why =
    run.state === "parked" ? (owner ? "parkedYou" : "parked") : run.state === "waiting" ? (owner ? "waitingYou" : "waiting") : owner ? "openYou" : "open";
  const title =
    open.length === 0
      ? answeredHere.length > 0
        ? t("titleAnswered")
        : owner
          ? t("titleYou")
          : t("titleOther", { login: run.dispatched_by })
      : owner
        ? t("titleYou")
        : t("titleOther", { login: run.dispatched_by });
  const first = open[0] ?? null;
  const tone = open.length > 0 || (active && waitingState) ? "border-attention/30 bg-attention-soft/50" : "border-success/20 bg-success-soft/30";

  return (
    <section
      aria-labelledby={`${ids}-title`}
      className={cn("flex min-w-0 flex-col gap-4 rounded-md border p-4 md:p-5", tone)}
      data-testid="run-decisions"
      data-state={run.state}
      data-open={open.length}
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 flex-col gap-1.5">
          <h2 id={`${ids}-title`} className="flex items-center gap-2 text-[15px] leading-[22px] font-semibold">
            <MessageCircleQuestionMark className="size-5 shrink-0" aria-hidden="true" />
            {title}
            {open.length > 1 ? (
              <span className="rounded-full bg-card px-2 text-xs font-medium tabular-nums" data-testid="run-decisions-count">
                {t("count", { count: open.length })}
              </span>
            ) : null}
          </h2>
          {open.length > 0 || (active && waitingState) ? (
            <p className="text-sm text-pretty" data-testid="run-decision-note">
              {t(why, { since: when, login: run.dispatched_by })}
            </p>
          ) : null}
        </div>
        {first ? (
          <Link
            href={decisionHref(first.id)}
            className="inline-flex min-h-7 shrink-0 items-center gap-1.5 text-sm font-medium text-brand underline-offset-4 hover:underline"
            data-testid="run-decision-link"
          >
            <Inbox className="size-4" aria-hidden="true" />
            {t("inInbox")}
            <ArrowRight className="size-3.5" aria-hidden="true" />
          </Link>
        ) : null}
      </div>
      {shown.length > 0 ? (
        <ol className="flex flex-col gap-4">
          {shown.map((decision) => (
            <li key={decision.id} className="rounded-md border bg-card shadow-raised p-4" data-testid="run-decision">
              <LiveDecision
                decision={decision}
                answeredHere={kept.has(decision.id)}
                onAnswered={(answered) => setAnsweredHere((list) => [...list.filter((item) => item.id !== answered.id), answered])}
              />
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
 * One decision of the banner: as the run's open decisions list it, and once answered here, as the hub holds it now (so
 * whether the agent got the answer shows when it does). One component either way, so its notice of the answer stays.
 */
function LiveDecision({
  decision,
  answeredHere,
  onAnswered,
}: {
  decision: Decision;
  answeredHere: boolean;
  onAnswered: (decision: Decision) => void;
}) {
  const live = useQuery({ ...decisionQuery(browserApi, decision.project, decision.id), initialData: decision, enabled: answeredHere });
  return <DecisionView decision={answeredHere ? live.data : decision} where="run" onAnswered={onAnswered} />;
}
