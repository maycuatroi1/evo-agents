"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Wrench } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";

import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { runPageHref } from "@/lib/loop-queries";
import type { TraceSpan } from "@/lib/systems";
import { systemTraceQuery } from "@/lib/systems-queries";
import { cn } from "@/lib/utils";

const STATUSES = ["running", "ok", "error", "refused"] as const;
type Status = (typeof STATUSES)[number];
const isStatus = (status: string): status is Status => (STATUSES as readonly string[]).includes(status);

const STATUS_TONE: Record<string, string> = {
  running: "text-running",
  ok: "text-success",
  error: "text-danger",
  refused: "text-danger",
};

/** The depth of each span under its trace's root, for the indent of its row. */
function depths(spans: readonly TraceSpan[]): Map<number, number> {
  const parent = new Map(spans.map((span) => [span.id, span.parent_id]));
  const out = new Map<number, number>();
  for (const span of spans) {
    let depth = 0;
    for (let at = span.parent_id; at !== null && at !== undefined && depth < 20; at = parent.get(at) ?? null) depth += 1;
    out.set(span.id, depth);
  }
  return out;
}

/**
 * One trace of a test, read while it runs: each run of an agent and each tool it called, indented under what started
 * it, with the node it ran as, its status, how long it took and what it cost; a run off the design (an edge the
 * system does not have) says so. The trace's page of its own, with its waterfall, is the Traces view's.
 */
export function TraceView({ project, slug, id, onBack }: { project: string; slug: string; id: number; onBack: () => void }) {
  const t = useTranslations("systems.trace");
  const format = useFormatter();
  const statusText = (status: string) => (isStatus(status) ? t(`status.${status}`) : status);
  const trace = useQuery({
    ...systemTraceQuery(browserApi, project, slug, id),
    refetchInterval: (query) => (query.state.data?.spans.some((span) => span.status === "running") ? 2000 : false),
  });
  const spans = trace.data?.spans ?? [];
  const depth = depths(spans);
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4" data-testid="system-trace" data-trace={id}>
      <button type="button" onClick={onBack} className="inline-flex w-fit items-center gap-1 text-sm text-brand hover:underline">
        <ArrowLeft className="size-4" aria-hidden="true" />
        {t("back")}
      </button>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="text-base font-semibold">{t("title", { id })}</h3>
        {trace.data ? (
          <span className="text-xs text-muted-foreground tabular-nums">
            {t("summary", {
              runs: trace.data.runs,
              errors: trace.data.errors,
              off: trace.data.off_design,
              usd: format.number(trace.data.usd, { style: "currency", currency: "USD", maximumFractionDigits: 4 }),
            })}
          </span>
        ) : null}
      </div>
      {trace.isPending ? (
        <Skeleton className="h-32 w-full" />
      ) : trace.isError ? (
        <p className="text-sm text-danger">{t("failed")}</p>
      ) : spans.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("empty")}</p>
      ) : (
        <ol className="flex flex-col divide-y rounded-md border" aria-label={t("spans")}>
          {spans.map((span) => (
            <li key={span.id} className="flex items-start gap-2 px-3 py-2 text-[13px]" style={{ paddingLeft: `${0.75 + (depth.get(span.id) ?? 0) * 1}rem` }} data-testid="system-span" data-status={span.status}>
              {span.tool ? <Wrench className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" /> : null}
              <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="truncate font-medium">
                  {span.tool ? span.tool : `@${span.agent}`}
                  {span.node ? <span className="font-normal text-muted-foreground">{` ${t("asNode", { node: span.node })}`}</span> : null}
                </span>
                <span className="text-xs text-muted-foreground">
                  <span className={cn("font-medium", STATUS_TONE[span.status])}>{statusText(span.status)}</span>
                  {span.seconds !== null ? `, ${t("seconds", { seconds: format.number(span.seconds, { maximumFractionDigits: 1 }) })}` : null}
                  {span.usd ? `, ${format.number(span.usd, { style: "currency", currency: "USD", maximumFractionDigits: 4 })}` : null}
                  {span.off_design ? <span className="text-attention">{`, ${t("offDesign")}`}</span> : null}
                </span>
                {span.reason ? <span className="text-xs text-pretty text-muted-foreground">{span.reason}</span> : null}
              </div>
              {span.run_id && span.project ? (
                <Link href={runPageHref(span.project, span.run_id)} className="shrink-0 text-xs text-brand hover:underline">
                  {t("run", { id: span.run_id })}
                </Link>
              ) : null}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
