"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import { ReactFlowProvider } from "@xyflow/react";
import { TriangleAlert, X } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { useNow } from "@/hooks/use-now";
import { agentsQuery } from "@/lib/agent-queries";
import type { Agent } from "@/lib/agents";
import { browserApi } from "@/lib/api/browser";
import { whoamiQuery } from "@/lib/queries";
import { type SystemOut, specOf, type TraceOut } from "@/lib/systems";
import { edgeActivity, ghostNodes, graphQueries, liveTraceIds, metricsOf, type Numbers, nodeStates, type NodeState } from "@/lib/systems-observe";
import { systemMetricsQuery, systemTraceQuery, systemTracesQuery, systemWorkSummaryQuery } from "@/lib/systems-queries";
import { filterOf, nameOfNode, nodeNames, pathOf, type WorkFilter, workOf } from "@/lib/systems-work";

import { ObserveCanvas, type Observed } from "./observe-canvas";
import { useTraceStream } from "./use-trace-stream";
import { WorkDetail } from "./work-detail";
import { WorkList } from "./work-list";
import { WorkSummaryStrip } from "./work-summary";

const NO_AGENTS: readonly Agent[] = [];
const NO_TRACE: TraceOut | undefined = undefined;

/** The spans of the newest traces read, one list (stable while their data is). */
const allSpans = (results: { data?: TraceOut }[]) => results.flatMap((result) => result.data?.spans ?? []);

const EMPTY: Numbers = { count: 0, errors: 0, offDesign: 0, p50Seconds: null, usd: 0, tokens: 0 };

/** The numbers of the node selected, in full, under the name a person knows it by. */
function NodeDetail({ id, name, state, numbers, scoped, onClose }: { id: string; name: string; state: NodeState; numbers: Numbers; scoped: boolean; onClose: () => void }) {
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
      <h3 id="system-node-detail" className="truncate pr-8 text-sm font-semibold" title={name}>
        {name}
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
      {scoped ? null : <p className="text-xs text-pretty text-muted-foreground">{t("detail.scope")}</p>}
    </section>
  );
}

/** The address of Observe with `work` and `filter` set (none and all drop their parameter). */
function observeUrl(work: number | null, filter: WorkFilter): string {
  const url = new URL(window.location.href);
  if (work === null) url.searchParams.delete("work");
  else url.searchParams.set("work", String(work));
  if (filter === "all") url.searchParams.delete("filter");
  else url.searchParams.set("filter", filter);
  return `${url.pathname}${url.search}`;
}

/**
 * Observe mode of an agent system (web/DESIGN.md, Observe: work and timeline): the numbers of the last 24 hours, the
 * list of the system's work with its filters, the work selected (`?work=`) with its timeline by agent and hand-offs,
 * and the map of the saved revision, which draws the path of the work selected, or else what the newest work does.
 * The trace stream keeps it all current while the tab is in view (`useTraceStream`); nothing says the stream is live,
 * only that it failed. The filter (`?filter=`) and the work selected live in the address.
 */
function Observer({ project, system }: { project: string; system: SystemOut }) {
  const t = useTranslations("systems.observe");
  const slug = system.slug;
  const spec = useMemo(() => specOf(system.spec), [system.spec]);
  const params = useSearchParams();
  const selected = workOf(params.get("work"));
  const filter = filterOf(params.get("filter"));
  const [node, setNode] = useState<string | null>(null);
  const status = useTraceStream(project, slug);
  const traces = useQuery(systemTracesQuery(browserApi, project, slug));
  const summary = useQuery(systemWorkSummaryQuery(browserApi, project, slug));
  const metrics = useQuery(systemMetricsQuery(browserApi, project, slug));
  const agents = useQuery(agentsQuery(browserApi, project));
  const viewer = useQuery(whoamiQuery(browserApi));
  const work = useQuery({ ...systemTraceQuery(browserApi, project, slug, selected ?? 0), enabled: selected !== null });
  const ids = liveTraceIds(traces.data ?? []);
  const spans = useQueries({ queries: ids.map((id) => systemTraceQuery(browserApi, project, slug, id)), combine: allSpans });
  const now = useNow(true) ?? 0;
  const shown = selected === null ? NO_TRACE : work.data;

  const names = useMemo(() => new Map((agents.data ?? NO_AGENTS).filter((agent) => agent.deleted_at === null).map((agent) => [agent.handle, agent.name || `@${agent.handle}`])), [agents.data]);
  const kinds = useTranslations("systems.designer.kinds");
  // A node with neither label nor profile (the entry, a graph) goes by its kind, as on the canvas.
  const nodes = useMemo(() => {
    const out = nodeNames(spec, names);
    for (const one of spec.nodes) if (!one.label && !one.profile) out.set(one.id, kinds(one.kind));
    return out;
  }, [spec, names, kinds]);
  const nameOf = useCallback((id: string) => nameOfNode(id, nodes, names), [nodes, names]);

  const observed = useMemo<Observed>(() => {
    // The work selected draws its own path and numbers; without one, the newest work and the last 50 traces' numbers.
    const read = shown ? shown.spans : spans;
    const numbers = metricsOf(shown ? shown.metrics : metrics.data);
    const { edges, extra } = edgeActivity(spec, numbers, read, now);
    return {
      states: nodeStates(spec, read, now),
      numbers: numbers.nodes,
      queries: graphQueries(spec, read),
      traces: shown ? 1 : (traces.data?.length ?? 0),
      edges,
      extra,
      ghosts: ghostNodes(spec, extra),
      path: shown ? pathOf(spec, shown.spans) : null,
    };
  }, [spec, metrics.data, spans, now, traces.data?.length, shown]);

  const go = useCallback((next: number | null, nextFilter: WorkFilter, push: boolean) => {
    const url = observeUrl(next, nextFilter);
    if (push) window.history.pushState(null, "", url);
    else window.history.replaceState(null, "", url);
  }, []);
  const selectHref = useCallback((id: number) => (typeof window === "undefined" ? `?mode=observe&work=${id}` : observeUrl(id, filter)), [filter]);
  const onSelect = useCallback((id: number) => go(id, filter, true), [go, filter]);

  return (
    <div className="flex min-w-0 flex-col gap-4" data-testid="system-observe" data-revision={system.revision} data-stream={status} data-work={selected ?? ""}>
      {status === "failing" || status === "ended" ? (
        <p role="status" className="flex items-center gap-1.5 rounded-md border bg-attention-soft px-3 py-2 text-[13px] text-attention" data-testid="system-observe-stream">
          <TriangleAlert className="size-4 shrink-0" aria-hidden="true" />
          {status === "ended" ? t("streamEnded") : t("streamFailing")}
        </p>
      ) : null}
      <section aria-labelledby="system-work-summary-title" className="flex flex-col gap-2">
        <h2 id="system-work-summary-title" className="text-xs font-medium text-muted-foreground">
          {t("work.summary.label")}
        </h2>
        <WorkSummaryStrip summary={summary.data} failed={summary.isError} />
      </section>
      <WorkList
        project={project}
        slug={slug}
        name={system.name || slug}
        traces={traces.data}
        failed={traces.isError}
        onRetry={() => void traces.refetch()}
        login={viewer.data?.login ?? null}
        filter={filter}
        onFilter={(next) => go(selected, next, false)}
        selected={selected}
        selectHref={selectHref}
        onSelect={onSelect}
        names={names}
        budget={spec.limits?.trace_usd ?? null}
      />
      {selected !== null ? (
        <WorkDetail
          project={project}
          slug={slug}
          id={selected}
          trace={work.data}
          failed={work.isError}
          onRetry={() => void work.refetch()}
          onClose={() => go(null, filter, true)}
          now={now}
          names={names}
          nameOf={nameOf}
        />
      ) : null}
      <div className="flex h-[520px] min-h-0 flex-col overflow-hidden rounded-md border bg-card shadow-raised max-lg:h-auto lg:flex-row" data-testid="system-observe-map">
        <div className="system-graph relative h-[420px] min-w-0 flex-1 bg-background lg:h-auto" data-testid="system-observe-canvas" data-path={selected ?? ""}>
          <ObserveCanvas
            spec={spec}
            observed={observed}
            agents={agents.data ?? NO_AGENTS}
            selected={node}
            onSelect={setNode}
            label={selected === null ? t("work.mapAll") : t("work.map", { id: selected })}
          />
        </div>
        {node ? (
          <aside className="flex min-h-0 flex-col gap-3 overflow-y-auto border-t bg-card p-4 lg:w-[20rem] lg:border-t-0 lg:border-l" aria-label={t("panel")}>
            <NodeDetail
              id={node}
              name={nameOf(node)}
              state={observed.states.get(node) ?? "idle"}
              numbers={observed.numbers.get(node) ?? EMPTY}
              scoped={selected !== null}
              onClose={() => setNode(null)}
            />
          </aside>
        ) : null}
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
