"use client";

import { X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { Fact, Facts, useMoney } from "@/components/data/facts";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Sheet, SheetClose, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { browserApi } from "@/lib/api/browser";

import { FireOutcomeBadge, KindTag, PausedBadge, Relative, RunLink, ScheduleLine, workText } from "./parts";
import { type CronFire, cronFiresQuery, type CronJob } from "./queries";

/** The fires of a job, latest due first: when it was due and who pressed Run now, its outcome, its run and why. */
function FiresTable({ project, job, fires }: { project: string; job: CronJob; fires: CronFire[] }) {
  const t = useTranslations("cron.fires");
  const outcome = useTranslations("cron.outcome");
  const columns = useMemo(() => {
    const helper = dataTableColumns<CronFire>();
    const who = (fire: CronFire) => (fire.manual ? t("manual", { login: fire.actor ?? "-" }) : t("scheduled"));
    return helper.columns([
      helper.display({
        id: "due",
        header: () => t("columns.due"),
        meta: { primary: true },
        cell: (info) => (
          <CellMain sub={who(info.row.original)}>
            <Relative value={info.row.original.scheduled_for} />
          </CellMain>
        ),
      }),
      helper.display({ id: "outcome", header: () => t("columns.outcome"), cell: (info) => <FireOutcomeBadge fire={info.row.original} /> }),
      helper.display({ id: "run", header: () => t("columns.run"), cell: (info) => <RunLink project={project} fire={info.row.original} /> }),
      helper.display({
        id: "reason",
        header: () => t("columns.reason"),
        cell: (info) => {
          const fire = info.row.original;
          return (
            <span className={`block max-w-[36ch] truncate ${fire.outcome === "refused" ? "text-danger" : ""}`} title={fire.reason ?? undefined}>
              {fire.reason ?? "-"}
            </span>
          );
        },
      }),
    ]);
  }, [t, project]);
  if (fires.length === 0) {
    return (
      <p className="rounded-sm border border-dashed px-4 py-6 text-center text-[13px] text-muted-foreground" data-testid="cron-fires-empty">
        {t("empty")}
      </p>
    );
  }
  return (
    <DataTable
      data={fires}
      columns={columns}
      caption={t("caption", { name: job.name })}
      getRowId={(fire) => String(fire.id)}
      density="compact"
      columnClassNames={{ reason: "hidden md:table-cell" }}
      testId="cron-fires"
      mobile={(fire) => ({
        title: <Relative value={fire.scheduled_for} />,
        status: <FireOutcomeBadge fire={fire} />,
        meta: [fire.run_id === null ? null : `#${fire.run_id}`, fire.reason ?? outcome(fire.outcome ?? "pending")].filter(Boolean).join(": "),
        danger: fire.outcome === "refused",
        data: { "fire-id": fire.id },
      })}
    />
  );
}

/** What the job is, above its fires: what it starts, when, its owner, its next fire or why it is paused, its cost. */
function JobFacts({ job }: { job: CronJob }) {
  const t = useTranslations("cron.fires");
  const money = useMoney();
  return (
    <Facts>
      <Fact label={t("work")}>
        <span className="flex min-w-0 items-center gap-2">
          <KindTag kind={job.kind} />
          <span className="truncate font-mono text-xs" title={workText(job)}>
            {workText(job)}
          </span>
        </span>
      </Fact>
      <Fact label={t("schedule")}>
        <ScheduleLine job={job} />
      </Fact>
      <Fact label={t("owner")}>
        <span className="font-mono text-xs">{job.owner}</span>
      </Fact>
      <Fact label={t("next")} testId="cron-sheet-next">
        {job.paused ? (
          <span className="flex flex-col gap-1">
            <PausedBadge />
            <span className="text-xs text-muted-foreground">{job.paused_reason ?? (job.paused_by ? t("pausedBy", { login: job.paused_by }) : t("pausedByHub"))}</span>
          </span>
        ) : job.next_fire_at ? (
          <Relative value={job.next_fire_at} />
        ) : (
          "-"
        )}
      </Fact>
      <Fact label={t("cost")}>
        <span className="tabular-nums">{money(job.cost_24h_usd)}</span>
      </Fact>
    </Facts>
  );
}

function SheetBody({ project, job }: { project: string; job: CronJob }) {
  const t = useTranslations("cron.fires");
  const state = useHubQuery(cronFiresQuery(browserApi, project, job.name));
  return (
    <div className="flex flex-col gap-5 p-4">
      <JobFacts job={job} />
      <section aria-labelledby="cron-fires-title" className="flex flex-col gap-3">
        <h2 id="cron-fires-title" className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        <QueryView state={state} loading={<TableSkeleton rows={4} />}>
          {(fires) => <FiresTable project={project} job={job} fires={fires} />}
        </QueryView>
      </section>
    </div>
  );
}

/** A job's history in a sheet from the right: the job's facts, then its fires, kept current while it is open. */
export function FiresSheet({ project, job, onClose }: { project: string; job: CronJob | null; onClose: () => void }) {
  const t = useTranslations("cron.fires");
  return (
    <Sheet open={job !== null} onOpenChange={(open) => (open ? undefined : onClose())}>
      <SheetContent side="right" showCloseButton={false} aria-describedby={undefined} className="gap-0 p-0 data-[side=right]:w-full data-[side=right]:sm:max-w-2xl" data-testid="cron-fires-sheet">
        <div className="flex h-13 shrink-0 items-center gap-2 border-b px-4">
          <SheetTitle asChild>
            <p className="min-w-0 truncate text-[15px] leading-[22px] font-semibold">{job ? t("sheetTitle", { name: job.name }) : ""}</p>
          </SheetTitle>
          <SheetClose asChild>
            <Button type="button" variant="ghost" size="icon" className="ml-auto" aria-label={t("close")} data-testid="cron-fires-close">
              <X aria-hidden="true" />
            </Button>
          </SheetClose>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">{job ? <SheetBody key={job.name} project={project} job={job} /> : null}</div>
      </SheetContent>
    </Sheet>
  );
}
