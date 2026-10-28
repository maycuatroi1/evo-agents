"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ComponentProps, useCallback, useMemo, useState } from "react";

import { DataCard } from "@/components/data/data-card";
import type { DataListRow } from "@/components/data/data-list";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { useDay, useMoney } from "@/components/data/facts";
import { Identifier } from "@/components/data/identifier";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";

import { CiIcon, CycleBadge } from "./badges";
import { githubTagUrl, publishedTag } from "./model";
import { PanelLine, Relative } from "./parts";
import { CYCLES_PAGE, endedCyclesQuery, type ReleaseCycle, type ReleaseCycleList } from "./queries";

/** How a cycle that ended ended, in one line: the version it published, or why it closed. */
function useEnding() {
  const t = useTranslations("releases.past");
  return useCallback(
    (cycle: ReleaseCycle) => {
      const tag = publishedTag(cycle);
      if (tag) return t("published", { tag: tag.tag });
      return cycle.stop_reason ? t("closedWhy", { reason: cycle.stop_reason }) : t("closed");
    },
    [t],
  );
}

/** The tags a cycle followed, each as its name with its CI as an icon. */
function TagChips({ cycle, repo }: { cycle: ReleaseCycle; repo: string | null }) {
  const t = useTranslations("releases.past");
  if (cycle.tags.length === 0) return <span className="text-fg-subtle">{t("noTags")}</span>;
  return (
    <ul className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1" data-testid="release-past-tags">
      {cycle.tags.map((tag) => (
        <li key={tag.tag} className="inline-flex items-center gap-1">
          <CiIcon state={tag.ci_state} />
          <Identifier value={tag.tag} externalHref={githubTagUrl(repo, tag.tag)} linkLabel={t("tagLink", { tag: tag.tag })} />
        </li>
      ))}
    </ul>
  );
}

function PastTable({ project, list, repo }: { project: string; list: ReleaseCycleList; repo: string | null }) {
  const t = useTranslations("releases.past");
  const money = useMoney();
  const day = useDay();
  const ending = useEnding();
  const columns = useMemo(() => {
    const helper = dataTableColumns<ReleaseCycle>();
    return helper.columns([
      helper.display({
        id: "cycle",
        header: () => t("columns.cycle"),
        meta: { primary: true },
        cell: (info) => {
          const cycle = info.row.original;
          return (
            <CellMain sub={ending(cycle)} subTitle={ending(cycle)}>
              <span className="font-mono text-[13px] tabular-nums">#{cycle.id}</span>
              <span className="truncate font-normal text-muted-foreground">{day(cycle.local_day, "long")}</span>
            </CellMain>
          );
        },
      }),
      helper.display({ id: "state", header: () => t("columns.state"), cell: (info) => <CycleBadge state={info.row.original.state} /> }),
      helper.display({ id: "tags", header: () => t("columns.tags"), cell: (info) => <TagChips cycle={info.row.original} repo={repo} /> }),
      helper.display({
        id: "runs",
        header: () => t("columns.runs"),
        meta: { numeric: true },
        cell: (info) => <span className="tabular-nums">{info.row.original.runs.length}</span>,
      }),
      helper.display({
        id: "cost",
        header: () => t("columns.cost"),
        meta: { numeric: true },
        cell: (info) => {
          const cycle = info.row.original;
          return (
            <span className="tabular-nums">{money(cycle.cost_usd)}</span>
          );
        },
      }),
      helper.display({
        id: "ended",
        header: () => t("columns.ended"),
        meta: { numeric: true },
        cell: (info) => {
          const ended = info.row.original.ended_at;
          return ended ? <Relative value={ended} /> : <span className="text-fg-subtle">-</span>;
        },
      }),
    ]);
  }, [t, money, day, ending, repo]);

  const mobile = (cycle: ReleaseCycle): DataListRow => {
    const meta = t("mobileMeta", { ending: ending(cycle), runs: cycle.runs.length, cost: money(cycle.cost_usd) });
    return {
      title: t("mobileTitle", { id: cycle.id, day: day(cycle.local_day) }),
      status: <CycleBadge state={cycle.state} />,
      meta,
      metaText: meta,
      data: { "cycle-id": cycle.id },
    };
  };

  return (
    <DataTable
      data={list.cycles}
      columns={columns}
      caption={t("caption", { project })}
      getRowId={(row) => String(row.id)}
      columnClassNames={{ tags: "hidden lg:table-cell", runs: "hidden sm:table-cell", ended: "hidden md:table-cell" }}
      testId="release-past"
      mobile={mobile}
    />
  );
}

/** A pager button that keeps keyboard focus when it cannot act, as the admin lists' pager does. */
function PageButton({ unavailable, onClick, ...props }: ComponentProps<typeof Button> & { unavailable: boolean }) {
  return (
    <Button
      type="button"
      variant="secondary"
      size="sm"
      aria-disabled={unavailable || undefined}
      className="aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
      onClick={(event) => {
        if (!unavailable) onClick?.(event);
      }}
      {...props}
    />
  );
}

function Pager({ list, onOffset }: { list: ReleaseCycleList; onOffset: (offset: number) => void }) {
  const t = useTranslations("releases.past");
  if (list.total <= list.limit) return null;
  const first = list.offset + 1;
  const last = Math.min(list.offset + list.cycles.length, list.total);
  return (
    <nav aria-label={t("pager")} className="flex flex-wrap items-center justify-between gap-2" data-testid="release-past-pager">
      <p className="text-xs text-fg-subtle tabular-nums" aria-live="polite">
        {t("range", { first, last, total: list.total })}
      </p>
      <div className="flex items-center gap-2">
        <PageButton unavailable={list.offset === 0} onClick={() => onOffset(Math.max(list.offset - CYCLES_PAGE, 0))} data-testid="release-past-newer">
          <ChevronLeft aria-hidden="true" />
          {t("newer")}
        </PageButton>
        <PageButton unavailable={last >= list.total} onClick={() => onOffset(list.offset + CYCLES_PAGE)} data-testid="release-past-older">
          {t("older")}
          <ChevronRight aria-hidden="true" />
        </PageButton>
      </div>
    </nav>
  );
}

/** The cycles that ended, newest first, a page at a time: how each ended, its tags with their CI, its runs and cost. */
export function PastCycles({ project, repo }: { project: string; repo: string | null }) {
  const t = useTranslations("releases.past");
  const [offset, setOffset] = useState(0);
  const cycles = useHubQuery(endedCyclesQuery(browserApi, project, offset));
  return (
    <section aria-labelledby="release-past-title" className="flex flex-col gap-3">
      <h2 id="release-past-title" className="text-[15px] leading-[22px] font-semibold">
        {t("title")}
      </h2>
      <QueryView state={cycles} loading={<TableSkeleton rows={3} />}>
        {(list) => (
          <DataCard footer={<Pager list={list} onOffset={setOffset} />}>
            {list.cycles.length === 0 ? <PanelLine testId="release-past-none">{t("none")}</PanelLine> : <PastTable project={project} list={list} repo={repo} />}
          </DataCard>
        )}
      </QueryView>
    </section>
  );
}
