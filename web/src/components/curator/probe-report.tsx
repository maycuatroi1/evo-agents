"use client";

import { useQuery } from "@tanstack/react-query";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { projectHref } from "@/components/shell/nav";
import { StatusBadge } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { useInsightFormat } from "./insight-format";
import { NUMBER, Panel, PanelLine, PanelSkeleton, PanelTable, TABLE_CELL, TABLE_HEAD } from "./panel";
import { TEXT_LINK } from "./parts";
import { flaggedCounts, flaggedTotal, probesById, reportShown, taskLine } from "./probe-model";
import { type Probe, type ProbeFix, probeReportQuery, probesQuery, type ProbeUnprobed, type ProbeWeek, type ProbeWeekly } from "./probe-queries";
import { ArmCount, ModelName, ProbeFlag, ResultNotes } from "./probe-status";
import { proposalHref, type ProposalState } from "./queries";

/** The changes without a probe, and the fix proposals, a list shows before it says how many more there are. */
const LISTED = 10;

/** A week as the picker and the summary name it: this week while it is in progress, the week of its Monday after. */
function useWeekName() {
  const t = useTranslations("curator.probes");
  const format = useInsightFormat();
  return (week: ProbeWeek) => (week.complete ? t("weekOf", { day: format.shortDay(week.week) }) : t("thisWeek"));
}

/** The weeks of the report, newest first, as toggles: each with its flags counted, the one shown pressed. */
function WeekPicker({ weeks, shown, onPick }: { weeks: ProbeWeek[]; shown: string; onPick: (week: string) => void }) {
  const t = useTranslations("curator.probes");
  const name = useWeekName();
  return (
    <div role="group" aria-label={t("weeks")} className="flex flex-wrap gap-2 border-b px-4 py-3" data-testid="probe-weeks">
      {weeks.map((week) => {
        const flags = flaggedTotal(week);
        return (
          <Button
            key={week.week}
            type="button"
            variant="outline"
            size="sm"
            aria-pressed={week.week === shown}
            onClick={() => onPick(week.week)}
            className="aria-pressed:border-brand aria-pressed:bg-surface-selected aria-pressed:text-foreground max-md:h-11"
            data-testid="probe-week"
            data-week={week.week}
          >
            {name(week)}
            {flags > 0 ? (
              <span className={cn("rounded-full px-1.5 text-xs tabular-nums", week.totals.recurring > 0 ? "bg-danger-soft text-danger" : "bg-muted text-muted-foreground")} data-testid="probe-week-flags">
                <span aria-hidden="true">{flags}</span>
                <span className="sr-only">{t("flagCount", { count: flags })}</span>
              </span>
            ) : null}
          </Button>
        );
      })}
    </div>
  );
}

/** One line on the week: how many results, those that wear a flag counted by status, and what the week's runs cost. */
function WeekSummary({ week }: { week: ProbeWeek }) {
  const t = useTranslations("curator.probes");
  const format = useInsightFormat();
  const name = useWeekName();
  const flags = flaggedCounts(week);
  return (
    <p className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2.5 text-[13px] text-muted-foreground" data-testid="probe-week-summary">
      <span className="font-medium text-foreground">{name(week)}</span>
      <span className="tabular-nums">{t("results", { count: week.totals.probes })}</span>
      {flags.map((item) => (
        <span key={item.status} className={cn("tabular-nums", item.status === "recurring" && "text-danger")}>
          {t("statusCount", { count: item.count, status: t(`word.${item.status}`) })}
        </span>
      ))}
      <span className="tabular-nums">
        {week.budget_usd === null ? t("spent", { cost: format.money(week.cost_usd) }) : t("spentOf", { cost: format.money(week.cost_usd), budget: format.money(week.budget_usd) })}
      </span>
    </p>
  );
}

/** A probe as a row names it: its task's first line, a link to its proposal when it has one, and its ids. */
function ProbeName({ project, id, change, probe }: { project: string; id: number; change: number; probe: Probe | undefined }) {
  const t = useTranslations("curator.probes");
  const name = probe ? taskLine(probe.task) : t("probeName", { id });
  return (
    <span className="flex min-w-0 flex-col gap-0.5">
      {probe?.proposal_id ? (
        <Link href={proposalHref(project, probe.proposal_id)} className={cn(TEXT_LINK, "text-pretty [overflow-wrap:anywhere]")} data-testid="probe-proposal-link">
          {name}
        </Link>
      ) : (
        <span className="text-pretty text-foreground [overflow-wrap:anywhere]">{name}</span>
      )}
      <span className="font-mono text-xs text-fg-subtle tabular-nums">{t("ids", { probe: id, change })}</span>
    </span>
  );
}

function WeekTable({ project, week, probes }: { project: string; week: ProbeWeek; probes: Map<number, Probe> }) {
  const t = useTranslations("curator.probes");
  return (
    <PanelTable label={t("caption")} testId="probe-results">
      <thead className="border-b bg-surface-sunken">
        <tr>
          <th scope="col" className={TABLE_HEAD}>
            {t("head.probe")}
          </th>
          <th scope="col" className={TABLE_HEAD}>
            {t("head.model")}
          </th>
          <th scope="col" className={cn(TABLE_HEAD, "text-right")}>
            {t("head.on")}
          </th>
          <th scope="col" className={cn(TABLE_HEAD, "text-right")}>
            {t("head.off")}
          </th>
          <th scope="col" className={TABLE_HEAD}>
            <span className="sr-only">{t("head.status")}</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {week.probes.map((result) => (
          <tr
            key={`${result.probe_id}-${result.runtime}-${result.model ?? ""}`}
            className="border-b align-top last:border-b-0"
            data-testid="probe-result"
            data-probe={result.probe_id}
            data-status={result.status}
          >
            <th scope="row" className={cn(TABLE_CELL, "min-w-56 py-2.5 text-left font-normal")}>
              <ProbeName project={project} id={result.probe_id} change={result.change_id} probe={probes.get(result.probe_id)} />
            </th>
            <td className={cn(TABLE_CELL, "py-2.5")}>
              <ModelName runtime={result.runtime} model={result.model} />
            </td>
            <td className={cn(TABLE_CELL, NUMBER, "py-2.5")}>
              <ArmCount arm={result.on} side="on" />
            </td>
            <td className={cn(TABLE_CELL, NUMBER, "py-2.5")}>
              <ArmCount arm={result.off} side="off" />
            </td>
            <td className={cn(TABLE_CELL, "min-w-44 py-2.5")}>
              <span className="flex min-w-0 flex-col items-start gap-1">
                <ProbeFlag status={result.status} />
                <ResultNotes status={result.status} previous={result.previous} reason={result.reason} />
                {result.run_id !== null ? (
                  <Link href={projectHref(project, `runs/${result.run_id}`) as Route} className="font-mono text-xs tabular-nums hover:text-brand max-md:inline-flex max-md:min-h-11 max-md:items-center" aria-label={t("openRun", { id: result.run_id })}>
                    #{result.run_id}
                  </Link>
                ) : null}
              </span>
            </td>
          </tr>
        ))}
      </tbody>
    </PanelTable>
  );
}

/** A short list under the week's table, with a head of its own and a line that counts what it leaves out. */
function SubList<T>({ title, items, testId, render }: { title: string; items: T[]; testId: string; render: (item: T) => ReactNode }) {
  const t = useTranslations("curator.probes");
  if (items.length === 0) return null;
  return (
    <div className="flex flex-col border-t" data-testid={testId}>
      <h3 className="px-4 pt-3 pb-1 text-[13px] font-medium text-muted-foreground">{title}</h3>
      <ul className="flex flex-col divide-y">{items.slice(0, LISTED).map(render)}</ul>
      {items.length > LISTED ? <p className="px-4 py-2 text-xs text-fg-subtle">{t("more", { count: items.length - LISTED })}</p> : null}
    </div>
  );
}

export function FixLine({ project, fix }: { project: string; fix: ProbeFix }) {
  const t = useTranslations("curator.probes");
  return (
    <li className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-[13px]" data-testid="probe-fix" data-proposal={fix.proposal_id}>
      <Link href={proposalHref(project, fix.proposal_id)} className={cn(TEXT_LINK, "min-w-0 text-pretty [overflow-wrap:anywhere]")}>
        #{fix.proposal_id} {fix.title}
      </Link>
      <StatusBadge kind="proposal" status={fix.state as ProposalState} />
      <span className="font-mono text-xs text-fg-subtle tabular-nums">{t("fixOf", { change: fix.change_id })}</span>
    </li>
  );
}

function UnprobedLine({ project, item }: { project: string; item: ProbeUnprobed }) {
  return (
    <li className="flex min-w-0 flex-col gap-0.5 px-4 py-2.5 text-[13px]" data-testid="probe-unprobed" data-change={item.change_id}>
      <Link href={proposalHref(project, item.proposal_id)} className={cn(TEXT_LINK, "w-fit min-w-0 text-pretty [overflow-wrap:anywhere]")}>
        #{item.proposal_id} {item.title}
      </Link>
      <span className="text-xs text-pretty text-fg-subtle [overflow-wrap:anywhere]">{item.reason}</span>
    </li>
  );
}

function ReportBody({ project, report, probes }: { project: string; report: ProbeWeekly; probes: Map<number, Probe> }) {
  const t = useTranslations("curator.probes");
  const [picked, setPicked] = useState<string | null>(null);
  const measured = report.budget_usd !== null || report.weeks.some((week) => week.probes.length > 0);
  const week = report.weeks.find((item) => item.week === picked) ?? report.weeks[0];
  return (
    <div className="flex min-w-0 flex-col" data-testid="probe-report-body">
      {measured && week ? (
        <>
          <WeekPicker weeks={report.weeks} shown={week.week} onPick={setPicked} />
          <WeekSummary week={week} />
          {week.probes.length === 0 ? (
            <PanelLine testId="probe-week-empty">{t("noResults")}</PanelLine>
          ) : (
            <>
              <p className="border-b px-4 py-2 text-xs text-pretty text-fg-subtle">{t("explain", { k: report.k })}</p>
              <WeekTable project={project} week={week} probes={probes} />
            </>
          )}
        </>
      ) : (
        <PanelLine testId="probe-off">{t("off")}</PanelLine>
      )}
      <SubList title={t("fixes")} items={report.fixes} testId="probe-fixes" render={(fix) => <FixLine key={fix.proposal_id} project={project} fix={fix} />} />
      <SubList title={t("unprobed")} items={report.unprobed} testId="probe-unprobed-list" render={(item) => <UnprobedLine key={item.change_id} project={project} item={item} />} />
    </div>
  );
}

/**
 * The weekly report of the project's probes on the Curator's page: the week in progress and the weeks before it, each
 * probe of a merged change on each model the charter names, with the change's rule in place and taken off, and a flag
 * only on a result that is not as expected; then the fix proposals of probes that recurred and the merged changes born
 * of a correction that have no probe. A project whose charter runs no probe and has nothing to list shows nothing.
 */
export function ProbeReport({ project }: { project: string }) {
  const t = useTranslations("curator.probes");
  const format = useInsightFormat();
  const report = useQuery(probeReportQuery(browserApi, project));
  const probes = useQuery(probesQuery(browserApi, project));
  let body;
  let sub: string | undefined;
  if (report.isPending) body = <PanelSkeleton rows={4} />;
  else if (report.isError) {
    body = (
      <div className="flex flex-wrap items-center gap-3 px-4 py-3 text-[13px] text-muted-foreground" role="alert">
        <span>{t("unreadable")}</span>
        <Button variant="outline" size="sm" onClick={() => void report.refetch()}>
          {t("retry")}
        </Button>
      </div>
    );
  } else if (!reportShown(report.data)) return null;
  else {
    const budget = report.data.budget_usd;
    sub = budget === null ? undefined : t("sub", { k: report.data.k, budget: format.money(budget) });
    body = <ReportBody project={project} report={report.data} probes={probesById(probes.data?.probes ?? [])} />;
  }
  return (
    <Panel title={t("title")} sub={sub} testId="probe-report">
      {body}
    </Panel>
  );
}
