"use client";

import { useQuery } from "@tanstack/react-query";
import { Lock, Replace, Wrench } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";

import { useInsightFormat } from "./insight-format";
import { Panel, PanelSkeleton } from "./panel";
import { Fact, Facts, TEXT_LINK } from "./parts";
import { fixesOf, weeksOfProbe } from "./probe-model";
import { type Probe, probeReportQuery, probesQuery, type ProbeWeek, type ProbeWeekResult } from "./probe-queries";
import { FixLine } from "./probe-report";
import { ArmCount, ModelName, ProbeFlag, ResultNotes } from "./probe-status";
import { proposalHref } from "./queries";

const SUBHEAD = "px-4 pt-3 pb-1.5 text-[13px] font-medium text-muted-foreground";

/** A block of text the card shows whole, in a region of its own that scrolls when it is long. */
function TextBlock({ label, children, mono = false, testId }: { label: string; children: ReactNode; mono?: boolean; testId: string }) {
  return (
    <div
      role="region"
      aria-label={label}
      tabIndex={0}
      className="mx-4 max-h-60 overflow-auto rounded-sm border bg-surface-sunken px-3 py-2 focus-visible:outline-offset-2"
      data-testid={testId}
    >
      {mono ? (
        <pre className="font-mono text-xs leading-5 whitespace-pre-wrap text-foreground [overflow-wrap:anywhere]">
          <code>{children}</code>
        </pre>
      ) : (
        <p className="text-[13px] leading-5 whitespace-pre-wrap text-foreground [overflow-wrap:anywhere]">{children}</p>
      )}
    </div>
  );
}

/** One runtime and model of the probe in a week: the arms side by side, then a flag when it is not as expected. */
function ResultLine({ result }: { result: ProbeWeekResult }) {
  const t = useTranslations("curator.probes");
  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid="probe-card-result" data-status={result.status}>
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-2">
        <ModelName runtime={result.runtime} model={result.model} />
        <ProbeFlag status={result.status} />
      </div>
      <dl className="grid grid-cols-2 gap-2 text-[13px] tabular-nums">
        <div className="flex flex-col items-start gap-0.5">
          <dt className="text-xs text-muted-foreground">{t("head.on")}</dt>
          <dd className="[&>span]:items-start">
            <ArmCount arm={result.on} side="on" />
          </dd>
        </div>
        <div className="flex flex-col items-start gap-0.5">
          <dt className="text-xs text-muted-foreground">{t("head.off")}</dt>
          <dd className="[&>span]:items-start">
            <ArmCount arm={result.off} side="off" />
          </dd>
        </div>
      </dl>
      <ResultNotes status={result.status} previous={result.previous} reason={result.reason} />
    </div>
  );
}

function WeekItem({ week, results }: { week: ProbeWeek; results: ProbeWeekResult[] }) {
  const t = useTranslations("curator.probes");
  const format = useInsightFormat();
  return (
    <li className="flex min-w-0 flex-col gap-2 px-4 py-2.5" data-testid="probe-card-week" data-week={week.week}>
      <span className="text-[13px] font-medium text-foreground">{week.complete ? t("weekOf", { day: format.shortDay(week.week) }) : t("thisWeek")}</span>
      {results.map((result) => (
        <ResultLine key={`${result.runtime}-${result.model ?? ""}`} result={result} />
      ))}
    </li>
  );
}

/** The probe's weeks and its change's fix proposals, from the weekly report. */
function ProbeWeeks({ project, probe }: { project: string; probe: Probe }) {
  const t = useTranslations("curator.probe");
  const report = useQuery(probeReportQuery(browserApi, project));
  if (report.isPending) return <PanelSkeleton rows={2} />;
  if (report.isError) {
    return (
      <div className="flex flex-wrap items-center gap-3 px-4 py-3 text-[13px] text-muted-foreground" role="alert">
        <span>{t("unreadable")}</span>
        <Button variant="outline" size="sm" onClick={() => void report.refetch()}>
          {t("retry")}
        </Button>
      </div>
    );
  }
  const weeks = weeksOfProbe(report.data, probe.id);
  const fixes = fixesOf(report.data, probe.change_id);
  return (
    <>
      <h3 className={SUBHEAD}>{t("weeks")}</h3>
      {weeks.length === 0 ? (
        <p className="px-4 pb-3 text-[13px] text-pretty text-muted-foreground" data-testid="probe-card-no-weeks">
          {probe.change_id === null ? t("waitsForChange") : t("noWeeks")}
        </p>
      ) : (
        <ul className="flex flex-col divide-y" data-testid="probe-card-weeks">
          {weeks.map((item) => (
            <WeekItem key={item.week.week} week={item.week} results={item.results} />
          ))}
        </ul>
      )}
      {fixes.length > 0 ? (
        <div className="flex flex-col border-t" data-testid="probe-card-fixes">
          <h3 className={SUBHEAD}>{t("fixes")}</h3>
          <ul className="flex flex-col divide-y">
            {fixes.map((fix) => (
              <FixLine key={fix.proposal_id} project={project} fix={fix} />
            ))}
          </ul>
        </div>
      ) : null}
    </>
  );
}

/**
 * A proposal's probe on its page: the task an agent is given each week, the repo and commit it starts from, the
 * verify (the answer key, which the API hands to the project's admins alone), what each week measured with the change's
 * rule in place and taken off, and the fix proposals once the probe recurred. The flags are the hub's statuses.
 */
export function ProbeCard({ project, probe }: { project: string; probe: Probe }) {
  const t = useTranslations("curator.probe");
  return (
    <Panel
      title={t("title")}
      sub={probe.change_id === null ? t("subNoChange", { id: probe.id }) : t("sub", { id: probe.id, change: probe.change_id })}
      testId="proposal-probe"
    >
      <div className="flex min-w-0 flex-col pb-1" data-testid="probe-card" data-probe={probe.id}>
        <Facts>
          <Fact label={t("repo")}>
            <span className="font-mono text-[13px]">{probe.repo}</span>
          </Fact>
          <Fact label={t("commit")}>{probe.commit ? <span className="font-mono text-[13px]">{probe.commit}</span> : <span className="text-[13px] text-muted-foreground">{t("tip")}</span>}</Fact>
          <Fact label={t("source")}>
            <span className="text-[13px]">{t(`sources.${probe.source}`)}</span>
          </Fact>
        </Facts>
        {probe.replaces != null ? (
          <p role="note" className="mx-4 mb-2 flex items-start gap-2 text-xs text-pretty text-muted-foreground" data-testid="probe-card-replaces">
            <Replace className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
            {t("replaces", { id: probe.replaces })}
          </p>
        ) : null}
        <h3 className={SUBHEAD}>{t("task")}</h3>
        <TextBlock label={t("task")} testId="probe-card-task">
          {probe.task}
        </TextBlock>
        <h3 className={SUBHEAD}>{t("verify")}</h3>
        {probe.verify !== null ? (
          <TextBlock label={t("verify")} mono testId="probe-card-verify">
            {probe.verify}
          </TextBlock>
        ) : (
          <p className="mx-4 flex items-start gap-2 rounded-sm border bg-surface-sunken px-3 py-2 text-xs text-pretty text-muted-foreground" data-testid="probe-card-verify-hidden">
            <Lock className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
            {t("verifyHidden")}
          </p>
        )}
        <div className="mt-2 flex min-w-0 flex-col border-t">
          <ProbeWeeks project={project} probe={probe} />
        </div>
      </div>
    </Panel>
  );
}

/** A fix proposal the hub wrote for a change whose probe recurred: which change, and the proposal that made it. */
export function FixOfNote({ project, change }: { project: string; change: number }) {
  const t = useTranslations("curator.probe");
  const probes = useQuery(probesQuery(browserApi, project, change));
  const of = probes.data?.probes.find((probe) => probe.proposal_id !== null)?.proposal_id ?? null;
  return (
    <div role="note" className="flex items-start gap-2.5 rounded-md border bg-surface-sunken px-4 py-3 text-[13px] text-muted-foreground" data-testid="proposal-fix-of">
      <Wrench className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <p className="text-pretty">
        {t.rich("fixOf", {
          change,
          link: (chunks) =>
            of === null ? (
              chunks
            ) : (
              <Link href={proposalHref(project, of)} className={TEXT_LINK}>
                {chunks}
              </Link>
            ),
        })}
      </p>
    </div>
  );
}
