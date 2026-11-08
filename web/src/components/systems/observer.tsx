"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import { ReactFlowProvider } from "@xyflow/react";
import { TriangleAlert, X } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { useNow } from "@/hooks/use-now";
import { agentsQuery } from "@/lib/agent-queries";
import type { Agent } from "@/lib/agents";
import { browserApi } from "@/lib/api/browser";
import { type SystemOut, specOf, type TraceOut } from "@/lib/systems";
import { edgeActivity, ghostNodes, graphQueries, liveTraceIds, metricsOf, type Numbers, nodeStates, type NodeState } from "@/lib/systems-observe";
import { systemMetricsQuery, systemTraceQuery, systemTracesQuery } from "@/lib/systems-queries";

import { DESIGNER_FRAME } from "./designer-frame";
import { ObserveCanvas, type Observed } from "./observe-canvas";
import { TraceList } from "./trace-list";
import { useTraceStream } from "./use-trace-stream";

const NO_AGENTS: readonly Agent[] = [];

/** The spans of the newest traces read, one list (stable while their data is). */
const allSpans = (results: { data?: TraceOut }[]) => results.flatMap((result) => result.data?.spans ?? []);

const EMPTY: Numbers = { count: 0, errors: 0, offDesign: 0, p50Seconds: null, usd: 0, tokens: 0 };

/** The numbers of the node selected, in full. */
function NodeDetail({ id, state, numbers, onClose }: { id: string; state: NodeState; numbers: Numbers; onClose: () => void }) {
  const t = useTranslations("systems.observe");
  const format = useFormatter();
  const rows: [string, string][] = [
    [t("detail.state"), t(`states.${state}`)],
    [t("detail.runs"), format.number(numbers.count)],
    [t("detail.errors"), format.number(numbers.errors)],
    [t("detail.offDesign"), format.number(numbers.offDesign)],
    [t("detail.p50"), numbers.p50Seconds === null ? "-" : t("seconds", { seconds: format.number(numbers.p50Seconds, { maximumFractionDigits: 1 }) })],
    [t("detail.tokens"), format.number(numbers.tokens)],
    [t("detail.usd"), format.number(numbers.usd, { style: "currency", currency: "USD", maximumFractionDigits: 4 })],
  ];
  return (
    <section className="relative flex flex-col gap-2 rounded-md border bg-surface-sunken p-3" aria-labelledby="system-node-detail" data-testid="system-node-detail" data-node={id}>
      <h3 id="system-node-detail" className="truncate pr-8 text-sm font-semibold">
        {id}
      </h3>
      <Button type="button" variant="ghost" size="icon-sm" className="absolute top-2 right-2" aria-label={t("closeDetail")} onClick={onClose}>
        <X aria-hidden="true" />
      </Button>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[13px]">
        {rows.map(([term, value]) => (
          <div key={term} className="contents">
            <dt className="text-muted-foreground">{term}</dt>
            <dd className="text-right tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
      <p className="text-xs text-pretty text-muted-foreground">{t("detail.scope")}</p>
    </section>
  );
}

/**
 * Observe mode of an agent system (web/DESIGN.md, Agent systems): the saved revision on the canvas with what goes
 * through it now, and the recent traces beside it. The trace stream keeps both current while the tab is in view
 * (`useTraceStream`); nothing says the stream is live, only that it failed.
 */
function Observer({ project, system }: { project: string; system: SystemOut }) {
  const t = useTranslations("systems.observe");
  const slug = system.slug;
  const spec = useMemo(() => specOf(system.spec), [system.spec]);
  const [selected, setSelected] = useState<string | null>(null);
  const status = useTraceStream(project, slug);
  const traces = useQuery(systemTracesQuery(browserApi, project, slug));
  const metrics = useQuery(systemMetricsQuery(browserApi, project, slug));
  const agents = useQuery(agentsQuery(browserApi, project));
  const ids = liveTraceIds(traces.data ?? []);
  const spans = useQueries({ queries: ids.map((id) => systemTraceQuery(browserApi, project, slug, id)), combine: allSpans });
  const now = useNow(true) ?? 0;

  const observed = useMemo<Observed>(() => {
    const numbers = metricsOf(metrics.data);
    const { edges, extra } = edgeActivity(spec, numbers, spans, now);
    return {
      states: nodeStates(spec, spans, now),
      numbers: numbers.nodes,
      queries: graphQueries(spec, spans),
      traces: traces.data?.length ?? 0,
      edges,
      extra,
      ghosts: ghostNodes(spec, extra),
    };
  }, [spec, metrics.data, spans, now, traces.data?.length]);

  return (
    <div className={DESIGNER_FRAME} data-testid="system-observe" data-revision={system.revision} data-stream={status}>
      <div className="flex flex-wrap items-center gap-2 border-b bg-card px-3 py-2 text-xs text-muted-foreground">
        <span>{t("observing", { revision: system.revision })}</span>
        {status === "failing" || status === "ended" ? (
          <span role="status" className="ml-auto inline-flex items-center gap-1 text-attention" data-testid="system-observe-stream">
            <TriangleAlert className="size-3.5" aria-hidden="true" />
            {status === "ended" ? t("streamEnded") : t("streamFailing")}
          </span>
        ) : null}
      </div>
      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <div className="system-graph relative min-h-[320px] min-w-0 flex-1 bg-background" data-testid="system-observe-canvas">
          <ObserveCanvas spec={spec} observed={observed} agents={agents.data ?? NO_AGENTS} selected={selected} onSelect={setSelected} />
        </div>
        <aside className="flex min-h-0 flex-col gap-3 overflow-y-auto border-t bg-card p-4 max-lg:max-h-[55%] lg:w-[24rem] lg:border-t-0 lg:border-l" aria-label={t("panel")}>
          {selected ? (
            <NodeDetail id={selected} state={observed.states.get(selected) ?? "idle"} numbers={observed.numbers.get(selected) ?? EMPTY} onClose={() => setSelected(null)} />
          ) : null}
          <TraceList project={project} slug={slug} traces={traces.data} failed={traces.isError} />
        </aside>
      </div>
    </div>
  );
}

/** Observe mode, loaded on demand by designer-lazy.tsx with React Flow. */
export default function SystemObserver(props: { project: string; system: SystemOut }) {
  return (
    <ReactFlowProvider>
      <Observer {...props} />
    </ReactFlowProvider>
  );
}
