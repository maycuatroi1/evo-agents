"use client";

import { Activity, MessagesSquare, ServerCrash, Workflow } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { MouseEvent } from "react";
import { useMemo } from "react";

import { DataCard, DataToolbar } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { FacetGroup } from "@/components/data/facet-group";
import { NAME_LINK } from "@/components/data/identifier";
import { Meter, METER_TEXT } from "@/components/data/meter";
import { EmptyState, NoResults, StatePanel } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useNow } from "@/hooks/use-now";
import { chatHref } from "@/lib/chat-keys";
import type { TraceSummary } from "@/lib/systems";
import { systemTraceHref } from "@/lib/systems-queries";
import { agentName, budgetShare, budgetTone, filterCounts, matches, WORK_FILTERS, type WorkFilter, workSeconds } from "@/lib/systems-work";
import { cn } from "@/lib/utils";

import { useUsd, useWaitingText, useWorkDuration, WorkStatusPill } from "./work-status";

type Props = {
  project: string;
  slug: string;
  name: string;
  traces: readonly TraceSummary[] | undefined;
  failed: boolean;
  onRetry: () => void;
  login: string | null;
  filter: WorkFilter;
  onFilter: (filter: WorkFilter) => void;
  selected: number | null;
  /** The address that selects a work, for its link. */
  selectHref: (id: number) => string;
  onSelect: (id: number) => void;
  names: ReadonlyMap<string, string>;
  /** The budget of one work (`limits.trace_usd` of the spec), when it has one. */
  budget: number | null;
};

/** How long a work has run: to its last move, or on to now while it is open. */
function Elapsed({ trace }: { trace: TraceSummary }) {
  const now = useNow(trace.status === "running" || trace.status === "needs_person");
  const duration = useWorkDuration();
  return <span className="tabular-nums">{duration(workSeconds(trace, now ?? Date.parse(trace.last_at)))}</span>;
}

function Cost({ trace, budget }: { trace: TraceSummary; budget: number | null }) {
  const t = useTranslations("systems.observe.work");
  const usd = useUsd();
  const share = budgetShare(trace.usd, budget);
  if (share === null || budget === null) return <span className="tabular-nums">{usd(trace.usd)}</span>;
  const tone = budgetTone(share);
  return (
    <span className="inline-flex flex-col items-end gap-1">
      <span className={cn("tabular-nums", METER_TEXT[tone])}>{t("costOfBudget", { usd: usd(trace.usd), budget: usd(budget) })}</span>
      <Meter value={share * 100} tone={tone} label={t("budgetLabel", { id: trace.id })} className="w-20" />
    </span>
  );
}

/**
 * Observe's list of the system's work, the newest first (web/DESIGN.md, Observe: work and timeline): the filters All,
 * Needs you, Running, Failed and Done with their counts, then a row per work with its status, the agent it is at, what
 * it waits for, how long it has taken, its runs, its cost against the budget of a work, and links to its work chat and
 * its trace. The work's title selects it, which shows its timeline, hand-offs and path below.
 */
export function WorkList({ project, slug, name, traces, failed, onRetry, login, filter, onFilter, selected, selectHref, onSelect, names, budget }: Props) {
  const t = useTranslations("systems.observe.work");
  const usd = useUsd();
  const waitingText = useWaitingText(names);
  const counts = useMemo(() => filterCounts(traces ?? [], login), [traces, login]);
  const shown = useMemo(() => (traces ?? []).filter((trace) => matches(trace, filter, login)), [traces, filter, login]);
  const titleOf = (trace: TraceSummary) => trace.title || t("untitled", { id: trace.id });

  const columns = useMemo(() => {
    const helper = dataTableColumns<TraceSummary>();
    const select = (id: number) => (event: MouseEvent<HTMLAnchorElement>) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
      event.preventDefault();
      onSelect(id);
    };
    return helper.columns([
      helper.display({
        id: "work",
        header: () => t("columns.work"),
        meta: { primary: true },
        cell: (info) => {
          const trace = info.row.original;
          const current = trace.id === selected;
          return (
            <CellMain sub={`#${trace.id}${trace.opened_by ? `, @${trace.opened_by}` : ""}`}>
              <a
                href={selectHref(trace.id)}
                onClick={select(trace.id)}
                aria-current={current ? "true" : undefined}
                aria-label={`${titleOf(trace)}. ${t("select", { id: trace.id })}`}
                className={cn(NAME_LINK, "truncate", current && "font-semibold text-brand underline underline-offset-4")}
                title={titleOf(trace)}
                data-testid="system-work-link"
                data-trace={trace.id}
              >
                {titleOf(trace)}
              </a>
            </CellMain>
          );
        },
      }),
      helper.display({ id: "status", header: () => t("columns.status"), cell: (info) => <WorkStatusPill status={info.row.original.status} /> }),
      helper.display({
        id: "agent",
        header: () => t("columns.agent"),
        cell: (info) => <span className="block max-w-40 truncate">{info.row.original.agent ? agentName(info.row.original.agent, names) : t("noAgent")}</span>,
      }),
      helper.display({
        id: "waiting",
        header: () => t("columns.waiting"),
        cell: (info) => {
          const words = waitingText(info.row.original.waiting);
          return (
            <span className={cn("block max-w-56 truncate", info.row.original.waiting?.kind === "decision" && "text-attention")} title={words ?? undefined}>
              {words ?? t("waitsNothing")}
            </span>
          );
        },
      }),
      helper.display({ id: "time", header: () => t("columns.time"), meta: { numeric: true }, cell: (info) => <Elapsed trace={info.row.original} /> }),
      helper.display({ id: "runs", header: () => t("columns.runs"), meta: { numeric: true }, cell: (info) => info.row.original.runs }),
      helper.display({ id: "cost", header: () => t("columns.cost"), meta: { numeric: true }, cell: (info) => <Cost trace={info.row.original} budget={budget} /> }),
      helper.display({
        id: "open",
        header: () => t("columns.open"),
        cell: (info) => {
          const trace = info.row.original;
          return (
            <span className="flex items-center gap-1">
              {trace.work_chat_id ? (
                <Button asChild variant="ghost" size="sm">
                  <Link href={chatHref(trace.work_chat_id)} aria-label={t("openChatLabel", { id: trace.id })} data-testid="system-work-chat">
                    <MessagesSquare aria-hidden="true" />
                    {t("openChat")}
                  </Link>
                </Button>
              ) : null}
              <Button asChild variant="ghost" size="sm">
                <Link href={systemTraceHref(project, slug, trace.id)} aria-label={t("openTraceLabel", { id: trace.id })} data-testid="system-work-trace">
                  <Activity aria-hidden="true" />
                  {t("openTrace")}
                </Link>
              </Button>
            </span>
          );
        },
      }),
    ]);
    // titleOf and waitingText read only t and names, which the list already depends on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [t, names, selected, selectHref, onSelect, budget, project, slug]);

  const options = WORK_FILTERS.map((value) => ({ value: value === "all" ? null : value, label: t(`filters.${value}`), count: counts[value] }));
  let body;
  if (traces === undefined) {
    body = failed ? (
      <StatePanel icon={ServerCrash} tone="danger" title={t("failedToLoad")} testId="system-work-failed">
        <Button variant="outline" onClick={onRetry}>
          {t("retry")}
        </Button>
      </StatePanel>
    ) : (
      <div className="flex flex-col gap-2 p-4">
        {[0, 1, 2].map((row) => (
          <Skeleton key={row} className="h-9 w-full" />
        ))}
      </div>
    );
  } else if (traces.length === 0) {
    body = <EmptyState icon={Workflow} title={t("empty")} description={t("emptyDescription")} />;
  } else if (shown.length === 0) {
    body = <NoResults title={t("noMatch")} filters={[{ label: t("filters.label"), value: t(`filters.${filter}`) }]} onClear={() => onFilter("all")} testId="system-work-no-match" />;
  } else {
    body = (
      <DataTable
        data={shown as TraceSummary[]}
        columns={columns}
        caption={t("caption", { name })}
        getRowId={(row) => String(row.id)}
        columnClassNames={{ agent: "hidden md:table-cell", waiting: "hidden lg:table-cell", runs: "hidden xl:table-cell" }}
        testId="system-work-table"
        mobile={(trace) => ({
          title: titleOf(trace),
          titleText: titleOf(trace),
          href: selectHref(trace.id) as never,
          status: <WorkStatusPill status={trace.status} />,
          // Only what there is: the agent it is at and what it waits for drop out when none, then its runs and cost.
          meta: [
            trace.agent ? t("mobileAt", { agent: agentName(trace.agent, names) }) : null,
            trace.waiting ? t("mobileWaits", { waiting: waitingText(trace.waiting) ?? "" }) : null,
            t("runs", { count: trace.runs }),
            usd(trace.usd),
          ]
            .filter(Boolean)
            .join(", "),
          data: { trace: trace.id, selected: trace.id === selected ? "true" : undefined },
        })}
      />
    );
  }
  return (
    <DataCard
      testId="system-work-list"
      toolbar={
        <DataToolbar label={t("listLabel", { name })} count={traces?.length ? t("filters.shown", { shown: shown.length, total: traces.length }) : undefined} countTestId="system-work-count">
          <FacetGroup
            label={t("filters.label")}
            options={options}
            selected={filter === "all" ? null : filter}
            onSelect={(value) => onFilter((value ?? "all") as WorkFilter)}
            countLabel={(count) => t("filters.count", { count })}
            testId="system-work-filters"
          />
        </DataToolbar>
      }
    >
      {body}
    </DataCard>
  );
}
