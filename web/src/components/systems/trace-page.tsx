"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity, ChevronLeft } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo } from "react";

import { PageHeader } from "@/components/shell/page-header";
import { StatePanel } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useNow } from "@/hooks/use-now";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import type { TraceSpan } from "@/lib/systems";
import { waterfall } from "@/lib/systems-observe";
import { systemObserveHref, systemTraceQuery } from "@/lib/systems-queries";

import { spanKind, TraceWaterfall } from "./trace-waterfall";
import { useTraceStream } from "./use-trace-stream";

type AgentRow = { agent: string; runs: number; messages: number; tools: number; queries: number; usd: number; errors: number };

/** The work of each agent in a trace: its runs, the messages it sent, its tool calls and kg queries, its cost. */
export function byAgent(spans: readonly TraceSpan[]): AgentRow[] {
  const rows = new Map<string, AgentRow>();
  for (const span of spans) {
    const row = rows.get(span.agent) ?? { agent: span.agent, runs: 0, messages: 0, tools: 0, queries: 0, usd: 0, errors: 0 };
    const kind = spanKind(span);
    if (kind === "run") {
      row.runs += 1;
      row.usd += span.usd;
    } else if (kind === "kg") row.queries += 1;
    else if (kind === "message") row.messages += 1;
    else row.tools += 1;
    if (span.status === "error" || span.status === "refused") row.errors += 1;
    rows.set(span.agent, row);
  }
  return [...rows.values()];
}

/**
 * The page of one trace of an agent system (web/DESIGN.md, Agent systems): what it was (the system, its revision, who
 * opened it and when, its totals), the work of each agent in it, and the waterfall of its spans. It follows the
 * system's trace stream while the tab is in view, so a trace still running grows on the page.
 */
export function TracePage({ project, slug, id }: { project: string; slug: string; id: number }) {
  const t = useTranslations("systems.traceView");
  const format = useFormatter();
  useTraceStream(project, slug);
  const trace = useQuery(systemTraceQuery(browserApi, project, slug, id));
  const spans = useMemo(() => trace.data?.spans ?? [], [trace.data]);
  const running = spans.some((span) => span.status === "running");
  // The clock ticks while a span runs, so its bar grows; an ended trace needs none.
  const now = useNow(running) ?? 0;
  const bars = useMemo(() => waterfall(spans, now), [spans, now]);
  const agents = useMemo(() => byAgent(spans), [spans]);
  const usd = (value: number) => format.number(value, { style: "currency", currency: "USD", maximumFractionDigits: 4 });
  const tokens = spans.reduce((sum, span) => sum + span.input_tokens + span.output_tokens, 0);

  return (
    <div className="flex flex-col gap-4" data-testid="system-trace-page" data-trace={id}>
      <Link href={systemObserveHref(project, slug)} className="inline-flex w-fit items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ChevronLeft className="size-4" aria-hidden="true" />
        {t("back", { slug })}
      </Link>
      {trace.isPending ? (
        <Skeleton className="h-64 w-full" />
      ) : trace.isError ? (
        <StatePanel icon={Activity} tone={isApiError(trace.error) && trace.error.kind === "not_found" ? undefined : "danger"} title={t("failedTitle", { id })} description={isApiError(trace.error) ? trace.error.info.message : t("failed")} testId="system-trace-error" />
      ) : (
        <>
          <PageHeader
            title={t("title", { id })}
            leading={<Activity className="size-5 text-muted-foreground" aria-hidden="true" />}
            tags={
              <>
                <Badge variant="secondary" className="font-mono">
                  {trace.data.system}
                </Badge>
                <Badge variant="outline">{t("revision", { revision: trace.data.revision })}</Badge>
              </>
            }
            sub={t("opened", { who: trace.data.opened_by ?? "-", when: format.dateTime(new Date(trace.data.started_at), { dateStyle: "medium", timeStyle: "medium" }) })}
          />
          <p className="text-[13px] text-muted-foreground tabular-nums" data-testid="system-trace-summary">
            {t("summary", { runs: trace.data.runs, errors: trace.data.errors, off: trace.data.off_design, tokens, usd: usd(trace.data.usd) })}
          </p>
          <section className="flex flex-col gap-2" aria-labelledby="trace-agents">
            <h2 id="trace-agents" className="text-sm font-semibold">
              {t("agents")}
            </h2>
            <div className="overflow-x-auto rounded-md border">
              <table className="w-full text-[13px]" data-testid="system-trace-agents">
                <thead className="bg-surface-sunken text-xs text-muted-foreground">
                  <tr>
                    {(["agent", "runs", "messages", "tools", "queries", "cost"] as const).map((column) => (
                      <th key={column} scope="col" className="px-3 py-1.5 text-left font-medium first:w-1/3">
                        {t(`columns.${column}`)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {agents.map((row) => (
                    <tr key={row.agent} data-testid="system-trace-agent" data-agent={row.agent}>
                      <th scope="row" className="px-3 py-1.5 text-left font-medium">
                        {`@${row.agent}`}
                        {row.errors ? <span className="ml-2 text-xs font-normal text-danger">{t("errorCount", { count: row.errors })}</span> : null}
                      </th>
                      <td className="px-3 py-1.5 tabular-nums">{row.runs}</td>
                      <td className="px-3 py-1.5 tabular-nums">{row.messages}</td>
                      <td className="px-3 py-1.5 tabular-nums">{row.tools}</td>
                      <td className="px-3 py-1.5 tabular-nums">{row.queries}</td>
                      <td className="px-3 py-1.5 tabular-nums">{usd(row.usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          <section className="flex flex-col gap-2" aria-labelledby="trace-waterfall">
            <h2 id="trace-waterfall" className="text-sm font-semibold">
              {t("waterfall")}
            </h2>
            {spans.length ? <TraceWaterfall spans={spans} bars={bars} chatId={trace.data.chat_id ?? null} /> : <p className="text-sm text-muted-foreground">{t("empty")}</p>}
          </section>
        </>
      )}
    </div>
  );
}
