"use client";

import { Activity, CircleDollarSign, Lightbulb, MoonStar, PencilLine, ScanSearch } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type ReactNode, useMemo } from "react";

import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import type { DataListRow } from "@/components/data/data-list";
import { DataCard } from "@/components/data/data-card";
import { Identifier, RunRef, Tag } from "@/components/data/identifier";
import { MetricStrip } from "@/components/data/metric-strip";
import { CARD_LINK, HomeCard } from "@/components/home/parts";
import { runHref } from "@/components/runs/queries";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton, TableSkeleton } from "@/components/states/states";
import { StatusBadge } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Ago } from "@/components/workers/ago";
import { workerHref } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

import { useLensText } from "./badges";
import { CuratorHeader, CuratorTabs } from "./curator-header";
import { useCuratorRights, useCuratorViewer } from "./hooks";
import { budgetShare, nightOutcome, proposalSearch, NO_PROPOSAL_FILTERS } from "./model";
import { Fact, Facts, useDay, useMoney } from "./parts";
import { type CuratorStatus, curatorHref, curatorStatusQuery, type NightList, type NightSummary, nightsQuery } from "./queries";

/** The CLI command that writes a charter, for whoever reads the empty state without the admin role. */
const CHARTER_COMMAND = "evo-agents hub curator charter set";

/** The skeleton of the Curator's head and tabs, for a page whose own content loads on its own. */
export function CuratorHeadSkeleton() {
  return (
    <div className="flex flex-col gap-4">
      <Skeleton className="h-7 w-40" />
      <Skeleton className="h-11 w-full max-w-sm" />
    </div>
  );
}

/** The skeleton of the Curator's overview: its strip of figures, then the nights. */
export function CuratorSkeleton({ children }: { children?: ReactNode }) {
  return children ?? <ListSkeleton metrics={4} rows={5} />;
}

/** The project's proposals page with `filters` applied: from a figure, a night or the last review. */
function proposalsHref(project: string, filters: Partial<typeof NO_PROPOSAL_FILTERS>): Route {
  return `${curatorHref(project, "proposals")}${proposalSearch({ ...NO_PROPOSAL_FILTERS, ...filters })}` as Route;
}

/** Tonight (or the last night, outside the window): its runs and cost against the charter's caps, what waits, the last review. */
function NightStrip({ project, status }: { project: string; status: CuratorStatus }) {
  const t = useTranslations("curator.metrics");
  const money = useMoney();
  const day = useDay();
  const night = status.night;
  const last = status.last_review_run;
  if (!night) return null;
  const which = night.in_window ? t("tonight") : t("lastNight", { night: day(night.night) });
  return (
    <MetricStrip
      label={t("label")}
      testId="curator-metrics"
      metrics={[
        {
          id: "runs",
          label: night.in_window ? t("runsTonight") : t("runsLast"),
          icon: Activity,
          live: night.active_run_id !== null,
          // Running only while a run of the night shift is in flight; the runs that ended are a plain count.
          tone: night.active_run_id !== null ? "running" : undefined,
          value: night.runs,
          meta:
            night.active_run_id !== null
              ? t("runsActive", { id: night.active_run_id })
              : night.in_window
                ? t("runsMeta", { max: night.max_runs })
                : t("runsMetaLast", { max: night.max_runs, which }),
        },
        {
          id: "cost",
          label: t("cost"),
          icon: CircleDollarSign,
          value: night.cost_usd,
          display: money(night.cost_usd),
          meta: t("costMeta", { budget: money(night.budget_usd), share: Math.round(budgetShare(night.cost_usd, night.budget_usd) * 100) }),
        },
        {
          id: "waiting",
          label: t("waiting"),
          icon: Lightbulb,
          tone: "attention",
          value: status.open_proposals,
          href: proposalsHref(project, { state: "open" }),
          meta: status.open_proposals > 0 ? t("waitingMeta") : t("waitingNone"),
        },
        {
          id: "review",
          label: t("review"),
          icon: ScanSearch,
          value: last?.proposals ?? 0,
          extra: last ? t("reviewExtra", { count: last.proposals }) : null,
          href: last ? proposalsHref(project, { run: last.id }) : undefined,
          meta: last ? t("reviewMeta", { id: last.id, findings: last.findings, night: day(last.night) }) : t("reviewNone"),
        },
      ]}
    />
  );
}

/** The notices of the project in the Inbox, where the member the briefs go to reads them. */
function briefsHref(project: string): Route {
  return `/inbox?kind=notice&project=${encodeURIComponent(project)}` as Route;
}

/**
 * The schedule as the hub keeps it: the window, the worker on duty and the member it runs as, a pause, and the last
 * morning brief, with a link to the Inbox for the member it went to.
 */
function ScheduleCard({ project, status }: { project: string; status: CuratorStatus }) {
  const t = useTranslations("curator.schedule");
  const viewer = useCuratorViewer(project);
  const money = useMoney();
  const day = useDay();
  const charter = status.charter;
  if (!charter) return null;
  const schedule = status.schedules[0] ?? null;
  const night = status.night;
  const ownWorker = viewer !== null && viewer.login === charter.schedule_owner;
  return (
    <HomeCard
      title={t("title")}
      testId="curator-schedule"
      action={
        <Link href={curatorHref(project, "charter")} className={CARD_LINK}>
          {t("charter", { revision: charter.revision })}
        </Link>
      }
    >
      <Facts>
        <Fact label={t("window")}>
          {t("windowValue", { start: charter.window.start, end: charter.window.end, timezone: charter.window.timezone })}
        </Fact>
        {night ? (
          <Fact label={t("now")} testId="curator-local-time">
            <span className="tabular-nums">{night.local_time}</span>, {night.in_window ? t("inWindow") : t("outWindow")}
          </Fact>
        ) : null}
        <Fact label={t("worker")}>
          <Identifier value={charter.worker} href={ownWorker ? workerHref(charter.worker_id) : undefined} testId="curator-worker" />
        </Fact>
        <Fact label={t("owner")}>
          <span className="font-mono text-xs">{charter.schedule_owner}</span>
        </Fact>
        <Fact label={t("caps")}>
          {t("capsValue", { budget: money(charter.night_budget_usd), runs: charter.max_runs_per_night ?? 0 })}
        </Fact>
        <Fact label={t("brief")}>{t("briefValue", { at: charter.brief_at ?? "07:00" })}</Fact>
        {status.last_brief ? (
          <Fact label={t("lastBrief")} testId="curator-last-brief">
            <span className="inline-flex flex-wrap items-center gap-x-2">
              <span>
                {t("lastBriefValue", { night: day(status.last_brief.night), to: status.last_brief.to })}, <Ago value={status.last_brief.sent_at} never="" />
              </span>
              {viewer !== null && viewer.login === status.last_brief.to ? (
                <Link href={briefsHref(project)} className={CARD_LINK} data-testid="curator-last-brief-link">
                  {t("readBrief")}
                </Link>
              ) : null}
            </span>
          </Fact>
        ) : null}
        {schedule?.paused_at ? (
          <Fact label={t("paused")} testId="curator-paused-fact">
            {schedule.paused_by === null ? t("pausedByHub") : t("pausedBy", { login: schedule.paused_by })}{" "}
            <Ago value={schedule.paused_at} never="" />
          </Fact>
        ) : null}
        {schedule?.pause_reason ? (
          <Fact label={t("pauseReason")} testId="curator-pause-reason">
            <span className="text-pretty">{schedule.pause_reason}</span>
          </Fact>
        ) : null}
      </Facts>
    </HomeCard>
  );
}

/** The project's latest review run: its run, its night's lenses, and what it wrote, linked to its proposals. */
function LastReviewCard({ project, status }: { project: string; status: CuratorStatus }) {
  const t = useTranslations("curator.lastReview");
  const lens = useLensText();
  const day = useDay();
  const last = status.last_review_run;
  return (
    <HomeCard
      title={t("title")}
      testId="curator-last-review"
      action={
        last ? (
          <Link href={proposalsHref(project, { run: last.id })} className={CARD_LINK} data-testid="curator-last-review-proposals">
            {t("proposalsLink", { count: last.proposals })}
          </Link>
        ) : null
      }
    >
      {last ? (
        <Facts>
          <Fact label={t("run")}>
            <span className="inline-flex flex-wrap items-center gap-2">
              <RunRef id={last.id} href={runHref(project, last.id)} label={t("openRun", { id: last.id })} data-testid="curator-last-review-run" />
              <StatusBadge kind="run" status={last.state} />
            </span>
          </Fact>
          <Fact label={t("night")}>{day(last.night)}</Fact>
          <Fact label={t("lenses")}>
            <span className="flex flex-wrap gap-1.5">
              {last.lenses.length > 0 ? last.lenses.map((name) => <Tag key={name}>{lens(name)}</Tag>) : <span className="text-muted-foreground">-</span>}
            </span>
          </Fact>
          <Fact label={t("wrote")}>{t("wroteValue", { findings: last.findings, proposals: last.proposals })}</Fact>
          <Fact label={t("ended")}>
            {last.finished_at ? <Ago value={last.finished_at} never="-" /> : <span className="text-muted-foreground">{t("notEnded")}</span>}
          </Fact>
        </Facts>
      ) : (
        <p className="px-4 py-6 text-[13px] text-muted-foreground" data-testid="curator-last-review-none">
          {t("none")}
        </p>
      )}
    </HomeCard>
  );
}

/** A night's outcome in words, in its tone; a failure in `danger`. */
function NightMeta({ night }: { night: NightSummary }) {
  const t = useTranslations("curator.nights");
  const outcome = nightOutcome(night);
  return t(`outcome.${outcome}`, { done: night.done, failed: night.failed, active: night.active, runs: night.runs });
}

function NightsTable({ project, list }: { project: string; list: NightList }) {
  const t = useTranslations("curator.nights");
  const money = useMoney();
  const day = useDay();
  const caption = t("caption", { project });
  const columns = useMemo(() => {
    const helper = dataTableColumns<NightSummary>();
    return helper.columns([
      helper.display({
        id: "night",
        header: () => t("columns.night"),
        meta: { primary: true },
        cell: (info) => {
          const night = info.row.original;
          return (
            <CellMain sub={<NightMeta night={night} />} danger={nightOutcome(night) === "failed"}>
              <span className="truncate" data-testid="curator-night-date">
                {day(night.night, "long")}
              </span>
              {night.active > 0 ? <StatusBadge kind="curator" status="running" data-testid="curator-night-state" /> : null}
            </CellMain>
          );
        },
      }),
      helper.display({
        id: "runs",
        header: () => t("columns.runs"),
        meta: { numeric: true },
        cell: (info) => <span className="tabular-nums">{info.row.original.runs}</span>,
      }),
      helper.display({
        id: "cost",
        header: () => t("columns.cost"),
        meta: { numeric: true },
        cell: (info) => (
          <span className="tabular-nums" title={list.budget_usd !== null ? t("costOf", { budget: money(list.budget_usd) }) : undefined}>
            {money(info.row.original.cost_usd)}
          </span>
        ),
      }),
      helper.display({
        id: "review",
        header: () => t("columns.review"),
        cell: (info) => {
          const review = info.row.original.review_run;
          if (!review) return <span className="text-fg-subtle">{t("noReview")}</span>;
          return (
            <span className="inline-flex items-center gap-2 whitespace-nowrap">
              <RunRef id={review.id} href={runHref(project, review.id)} label={t("openRun", { id: review.id })} />
              <StatusBadge kind="run" status={review.state} />
            </span>
          );
        },
      }),
      helper.display({
        id: "proposals",
        header: () => t("columns.proposals"),
        meta: { numeric: true },
        cell: (info) => {
          const review = info.row.original.review_run;
          if (!review) return <span className="text-fg-subtle">-</span>;
          return (
            <Link
              href={proposalsHref(project, { run: review.id })}
              className="font-medium text-brand tabular-nums underline-offset-4 hover:underline"
              aria-label={t("proposalsOf", { count: review.proposals, id: review.id })}
              data-testid="curator-night-proposals"
            >
              {review.proposals}
            </Link>
          );
        },
      }),
    ]);
  }, [t, day, money, project, list.budget_usd]);

  const mobile = (night: NightSummary): DataListRow => {
    const review = night.review_run;
    const detail = t("mobileMeta", { runs: night.runs, cost: money(night.cost_usd), proposals: review?.proposals ?? 0 });
    return {
      title: day(night.night, "long"),
      href: review ? proposalsHref(project, { run: review.id }) : undefined,
      status: review ? <StatusBadge kind="run" status={review.state} /> : undefined,
      meta: detail,
      metaText: detail,
      danger: nightOutcome(night) === "failed",
      data: { night: night.night },
    };
  };

  return (
    <DataTable
      data={list.nights}
      columns={columns}
      caption={caption}
      getRowId={(row) => row.night}
      columnClassNames={{ cost: "hidden sm:table-cell", review: "hidden md:table-cell" }}
      testId="curator-nights"
      mobile={mobile}
    />
  );
}

function Nights({ project, status }: { project: string; status: CuratorStatus }) {
  const t = useTranslations("curator.nights");
  const nights = useHubQuery(nightsQuery(browserApi, project));
  const charter = status.charter;
  return (
    <section aria-labelledby="curator-nights-title" className="flex flex-col gap-3">
      <h2 id="curator-nights-title" className="text-[15px] leading-[22px] font-semibold">
        {t("title")}
      </h2>
      <DataCard>
        <QueryView state={nights} loading={<TableSkeleton rows={4} />}>
          {(list) =>
            list.nights.length === 0 ? (
              <p className="px-4 py-6 text-[13px] text-muted-foreground" data-testid="curator-nights-empty">
                {charter ? t("empty", { start: charter.window.start, timezone: charter.window.timezone }) : t("emptyNoCharter")}
              </p>
            ) : (
              <NightsTable project={project} list={list} />
            )
          }
        </QueryView>
      </DataCard>
    </section>
  );
}

/** A project without a charter: what the Curator does, and the way to give it one. */
function NoCharter({ project }: { project: string }) {
  const t = useTranslations("curator.empty");
  const rights = useCuratorRights(project, null);
  return (
    <EmptyState
      icon={MoonStar}
      title={t("title")}
      description={
        <>
          <p>{t("description")}</p>
          {rights.charter ? null : (
            <p className="mt-2">
              {t.rich("reader", { command: () => <code className="rounded-xs bg-muted px-1 font-mono text-xs">{CHARTER_COMMAND}</code> })}
            </p>
          )}
        </>
      }
    >
      {rights.charter ? (
        <Button asChild>
          <Link href={`${curatorHref(project, "charter")}?edit=1` as Route} data-testid="curator-write-charter">
            <PencilLine aria-hidden="true" />
            {t("admin")}
          </Link>
        </Button>
      ) : null}
    </EmptyState>
  );
}

/**
 * A project's Curator: where its night shift stands (the pill, Pause or Resume), tonight's runs and cost against the
 * charter's caps, what waits for an answer, the last review run, the schedule, and the nights before, each with its
 * runs, cost and review. Read every 10 seconds while a run of the night shift is in flight, every minute otherwise.
 */
export function CuratorPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const status = useHubQuery(curatorStatusQuery(browserApi, project), initialError, { live: true });
  return (
    <QueryView state={status} loading={<CuratorSkeleton />}>
      {(found) => (
        <div className="flex flex-col gap-6" data-testid="curator-page" data-state={found.state ?? "off"}>
          <CuratorHeader project={project} status={found} />
          <CuratorTabs project={project} waiting={found.open_proposals} />
          {found.charter === null ? (
            <NoCharter project={project} />
          ) : (
            <>
              <NightStrip project={project} status={found} />
              <div className={cn("grid items-start gap-4 lg:grid-cols-2")}>
                <ScheduleCard project={project} status={found} />
                <LastReviewCard project={project} status={found} />
              </div>
              <Nights project={project} status={found} />
            </>
          )}
        </div>
      )}
    </QueryView>
  );
}

/** The night shift as the other Curator pages read it for their head, tabs and rights. */
export function useCuratorStatus(project: string, initialError: ApiErrorInfo | null = null) {
  return useHubQuery(curatorStatusQuery(browserApi, project), initialError);
}
