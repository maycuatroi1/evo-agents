"use client";

import { Activity, MessagesSquare, X } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { chatHref } from "@/lib/chat-keys";
import type { TraceOut } from "@/lib/systems";
import { systemTraceHref } from "@/lib/systems-queries";
import { handoffs as handoffsOf, workSeconds } from "@/lib/systems-work";

import { WorkHandoffs } from "./work-handoffs";
import { useUsd, useWorkDuration, WorkStatusPill } from "./work-status";
import { WorkTimeline } from "./work-timeline";

type Props = {
  project: string;
  slug: string;
  id: number;
  trace: TraceOut | undefined;
  failed: boolean;
  onRetry: () => void;
  onClose: () => void;
  now: number;
  names: ReadonlyMap<string, string>;
  nameOf: (node: string) => string;
};

/**
 * The work selected in Observe's list (web/DESIGN.md, Observe: work and timeline): its title, status, runs, cost and
 * time with its goal, links to its work chat and its trace, then its timeline by agent beside its hand-offs. The trace
 * stream reads it again each time it moves.
 */
export function WorkDetail({ project, slug, id, trace, failed, onRetry, onClose, now, names, nameOf }: Props) {
  const t = useTranslations("systems.observe.work");
  const duration = useWorkDuration();
  const usd = useUsd();
  const handoffs = useMemo(() => handoffsOf(trace?.spans ?? []), [trace?.spans]);
  const title = trace?.title || t("untitled", { id });
  return (
    <section aria-labelledby="system-work-detail-title" className="flex min-w-0 flex-col gap-4 rounded-md border bg-card p-4 shadow-raised" data-testid="system-work-detail" data-trace={id}>
      <header className="flex flex-wrap items-start gap-x-3 gap-y-2">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <h2 id="system-work-detail-title" className="flex min-w-0 flex-wrap items-center gap-2 text-base font-semibold">
            <span className="min-w-0 truncate" title={title}>
              {title}
            </span>
            <span className="text-xs font-normal text-fg-subtle tabular-nums">{t("detail.label", { id })}</span>
            {trace ? <WorkStatusPill status={trace.status} /> : null}
          </h2>
          {trace ? (
            <p className="text-xs text-muted-foreground tabular-nums">
              {t("detail.stats", { runs: t("runs", { count: trace.runs }), cost: usd(trace.usd), time: duration(workSeconds(trace, now)) })}
            </p>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {trace?.work_chat_id ? (
            <Button asChild variant="outline" size="sm">
              <Link href={chatHref(trace.work_chat_id)} data-testid="system-work-detail-chat">
                <MessagesSquare aria-hidden="true" />
                {t("openChat")}
              </Link>
            </Button>
          ) : null}
          <Button asChild variant="outline" size="sm">
            <Link href={systemTraceHref(project, slug, id)} data-testid="system-work-detail-trace">
              <Activity aria-hidden="true" />
              {t("openTrace")}
            </Link>
          </Button>
          <Button type="button" variant="ghost" size="icon-sm" aria-label={t("detail.close", { id })} onClick={onClose} data-testid="system-work-detail-close">
            <X aria-hidden="true" />
          </Button>
        </div>
      </header>
      {trace?.goal || trace?.done_when ? (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[13px]">
          {trace.goal ? (
            <>
              <dt className="text-muted-foreground">{t("detail.goal")}</dt>
              <dd className="text-pretty">{trace.goal}</dd>
            </>
          ) : null}
          {trace.done_when ? (
            <>
              <dt className="text-muted-foreground">{t("detail.doneWhen")}</dt>
              <dd className="text-pretty">{trace.done_when}</dd>
            </>
          ) : null}
        </dl>
      ) : null}
      {trace ? (
        <div className="grid min-w-0 gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
          <WorkTimeline timeline={trace.timeline ?? { runs: [], marks: [] }} now={now} names={names} />
          <WorkHandoffs handoffs={handoffs} nameOf={nameOf} />
        </div>
      ) : failed ? (
        <p className="flex flex-wrap items-center gap-3 text-[13px] text-danger" data-testid="system-work-detail-failed">
          {t("detail.failedToLoad")}
          <Button variant="outline" size="sm" onClick={onRetry}>
            {t("retry")}
          </Button>
        </p>
      ) : (
        <Skeleton className="h-32 w-full" />
      )}
    </section>
  );
}
