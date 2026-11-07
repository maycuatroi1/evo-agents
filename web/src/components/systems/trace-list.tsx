"use client";

import { Activity } from "lucide-react";
import Link from "next/link";
import { useFormatter, useNow, useTranslations } from "next-intl";

import { Skeleton } from "@/components/ui/skeleton";
import type { TraceSummary } from "@/lib/systems";
import { systemTraceHref } from "@/lib/systems-queries";

type Props = { project: string; slug: string; traces: readonly TraceSummary[] | undefined; failed: boolean };

/**
 * The recent traces of a system, the newest first, each a link to its page: who opened it and when, its runs, its
 * errors and crossings off the design (named only when there are some) and what it cost. The stream adds a trace
 * here as it starts and moves it as it goes on.
 */
export function TraceList({ project, slug, traces, failed }: Props) {
  const t = useTranslations("systems.observe");
  const format = useFormatter();
  const now = useNow({ updateInterval: 30_000 });
  return (
    <section className="flex min-h-0 flex-col gap-2" aria-labelledby="system-traces-title" data-testid="system-traces">
      <h3 id="system-traces-title" className="flex items-center gap-2 text-sm font-semibold">
        <Activity className="size-4 text-muted-foreground" aria-hidden="true" />
        {t("recentTraces")}
      </h3>
      {traces === undefined ? (
        failed ? (
          <p className="text-[13px] text-danger">{t("tracesFailed")}</p>
        ) : (
          <Skeleton className="h-24 w-full" />
        )
      ) : traces.length === 0 ? (
        <p className="text-[13px] text-pretty text-muted-foreground" data-testid="system-traces-empty">
          {t("noTraces")}
        </p>
      ) : (
        <ol className="flex flex-col divide-y rounded-md border">
          {traces.map((trace) => (
            <li key={trace.id} data-testid="system-trace-row" data-trace={trace.id}>
              <Link href={systemTraceHref(project, slug, trace.id)} className="flex flex-col gap-0.5 px-3 py-2 text-[13px] hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
                <span className="flex items-baseline gap-2">
                  <span className="font-medium">{t("traceTitle", { id: trace.id })}</span>
                  <span className="truncate text-xs text-muted-foreground">{trace.opened_by ? `@${trace.opened_by}` : null}</span>
                  <time className="ml-auto shrink-0 text-xs text-muted-foreground" dateTime={trace.last_at} title={format.dateTime(new Date(trace.last_at), { dateStyle: "medium", timeStyle: "medium" })}>
                    {format.relativeTime(new Date(trace.last_at), now)}
                  </time>
                </span>
                <span className="text-xs text-muted-foreground tabular-nums">
                  {t("traceRuns", { runs: trace.runs })}
                  {trace.errors ? <span className="text-danger">{`, ${t("traceErrors", { count: trace.errors })}`}</span> : null}
                  {trace.off_design ? <span className="text-attention">{`, ${t("traceOff", { count: trace.off_design })}`}</span> : null}
                  {`, ${format.number(trace.usd, { style: "currency", currency: "USD", maximumFractionDigits: 4 })}`}
                </span>
              </Link>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
