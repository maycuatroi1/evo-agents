"use client";

import { useTranslations } from "next-intl";
import { useCallback, useMemo } from "react";

import type { DataListRow } from "@/components/data/data-list";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { Fact, Facts, useDay, useMoney } from "@/components/data/facts";
import { Identifier } from "@/components/data/identifier";

import { OutcomeLine } from "./badges";
import { githubCommitUrl, githubRepoUrl, githubTagUrl, outcomeTone, shortCommit } from "./model";
import { FullTime, Panel, PanelLine, PathList, Relative } from "./parts";
import type { ReleaseCheck, ReleaseSchedule, ReleaseStatus } from "./queries";

/** The schedule as the hub keeps it: the repo, when it checks, what it ships, its paths, its cap and its owner. */
export function ScheduleCard({ status, schedule }: { status: ReleaseStatus; schedule: ReleaseSchedule }) {
  const t = useTranslations("releases.schedule");
  const money = useMoney();
  const repo = schedule.github_repo ?? schedule.repo;
  return (
    <Panel title={t("title")} testId="release-schedule">
      <Facts>
        <Fact label={t("repo")}>
          <Identifier value={repo} externalHref={githubRepoUrl(schedule.github_repo)} linkLabel={t("repoLink", { repo })} testId="release-repo" />
        </Fact>
        <Fact label={t("daily")} testId="release-daily">
          {t("dailyValue", { at: schedule.at, timezone: schedule.timezone })}
        </Fact>
        <Fact label={t("next")}>
          {status.paused ? (
            <span className="text-attention">{t("nextPaused")}</span>
          ) : status.next_check_at ? (
            <span className="inline-flex flex-wrap gap-x-1.5">
              <FullTime value={status.next_check_at} testId="release-next-time" />
              <span className="text-fg-subtle">
                (<Relative value={status.next_check_at} />)
              </span>
            </span>
          ) : (
            <span className="text-muted-foreground">-</span>
          )}
        </Fact>
        <Fact label={t("ships")}>{t("shipsValue", { ref: schedule.target_ref, pattern: schedule.tag_pattern })}</Fact>
        <Fact label={t("workflow")}>
          <span className="font-mono text-xs">{schedule.workflow}</span>
        </Fact>
        <Fact label={t("paths")}>
          <PathList paths={schedule.paths} empty="-" testId="release-paths" />
        </Fact>
        <Fact label={t("cap")} testId="release-cap">
          {t("capValue", { budget: money(schedule.cycle_budget_usd) })}
        </Fact>
        <Fact label={t("owner")} testId="release-owner">
          <span className="font-mono text-xs">{schedule.owner}</span>
        </Fact>
        <Fact label={t("updated")}>
          {t.rich("updatedValue", { login: schedule.updated_by, ago: () => <Relative value={schedule.updated_at} /> })}
        </Fact>
      </Facts>
    </Panel>
  );
}

/** Who made a check: the daily job, or an admin's Run now. */
function useTrigger() {
  const t = useTranslations("releases.check");
  const day = useDay();
  return useCallback(
    (check: ReleaseCheck) => (check.trigger === "manual" ? t("manual", { login: check.requested_by ?? "-" }) : t("daily", { day: day(check.local_day) })),
    [t, day],
  );
}

/** The newest check: what it found, in its tone when a person is needed or it failed, what it read and why. */
export function LastCheckCard({ status, schedule }: { status: ReleaseStatus; schedule: ReleaseSchedule }) {
  const t = useTranslations("releases.check");
  const tOutcome = useTranslations("releases.outcome");
  const trigger = useTrigger();
  const check = status.last_check;
  const repo = schedule.github_repo;
  return (
    <Panel title={t("title")} testId="release-last-check">
      {check ? (
        <Facts>
          <Fact label={t("outcome")} testId="release-last-outcome">
            <OutcomeLine outcome={check.outcome}>
              {check.outcome === "opened" && check.cycle_id !== null ? tOutcome("openedCycle", { id: check.cycle_id }) : tOutcome(check.outcome)}
            </OutcomeLine>
          </Fact>
          <Fact label={t("when")}>
            <Relative value={check.checked_at} />
          </Fact>
          <Fact label={t("by")} testId="release-last-trigger">
            {trigger(check)}
          </Fact>
          {check.base_tag ? (
            <Fact label={t("since")}>
              <Identifier value={check.base_tag} externalHref={githubTagUrl(repo, check.base_tag)} linkLabel={t("tagLink", { tag: check.base_tag })} />
            </Fact>
          ) : null}
          {check.target_commit ? (
            <Fact label={t("target")}>
              <Identifier
                value={check.target_commit}
                externalHref={githubCommitUrl(repo, check.target_commit)}
                linkLabel={t("commitLink", { commit: shortCommit(check.target_commit) })}
                title={check.target_commit}
              >
                {`${schedule.target_ref} ${shortCommit(check.target_commit)}`}
              </Identifier>
            </Fact>
          ) : null}
          {check.changed_paths !== null ? (
            <Fact label={t("changed")}>
              <PathList paths={check.changed_paths} empty={t("noChange")} testId="release-last-paths" />
            </Fact>
          ) : null}
          {check.reason ? (
            <Fact label={t("reason")} testId="release-last-reason">
              <span className={outcomeTone(check.outcome) === "danger" ? "text-danger" : undefined}>{check.reason}</span>
            </Fact>
          ) : null}
        </Facts>
      ) : (
        <PanelLine testId="release-last-check-none">{t("none", { at: schedule.at, timezone: schedule.timezone })}</PanelLine>
      )}
    </Panel>
  );
}

/** The checks before the newest, newest first: when, who made it, what it found and why. */
export function ChecksTable({ project, checks }: { project: string; checks: ReleaseCheck[] }) {
  const t = useTranslations("releases.checks");
  const tOutcome = useTranslations("releases.outcome");
  const trigger = useTrigger();
  const columns = useMemo(() => {
    const helper = dataTableColumns<ReleaseCheck>();
    return helper.columns([
      helper.display({
        id: "when",
        header: () => t("columns.when"),
        meta: { primary: true },
        cell: (info) => (
          <CellMain sub={trigger(info.row.original)}>
            <Relative value={info.row.original.checked_at} />
          </CellMain>
        ),
      }),
      helper.display({
        id: "outcome",
        header: () => t("columns.outcome"),
        cell: (info) => <OutcomeLine outcome={info.row.original.outcome} testId="release-check-outcome" />,
      }),
      helper.display({
        id: "reason",
        header: () => t("columns.reason"),
        cell: (info) => {
          const check = info.row.original;
          return (
            <span className={`block max-w-[48ch] truncate ${outcomeTone(check.outcome) === "danger" ? "text-danger" : ""}`} title={check.reason ?? undefined}>
              {check.reason ?? "-"}
            </span>
          );
        },
      }),
    ]);
  }, [t, trigger]);

  const mobile = (check: ReleaseCheck): DataListRow => {
    const meta = [trigger(check), check.reason].filter(Boolean).join(": ");
    return {
      title: <Relative value={check.checked_at} />,
      status: <OutcomeLine outcome={check.outcome}>{tOutcome(check.outcome)}</OutcomeLine>,
      meta,
      metaText: meta,
      danger: check.outcome === "error",
      data: { "check-id": check.id },
    };
  };

  return (
    <section aria-labelledby="release-checks-title" className="flex flex-col gap-3">
      <h2 id="release-checks-title" className="text-[15px] leading-[22px] font-semibold">
        {t("title")}
      </h2>
      <DataTable
        data={checks}
        columns={columns}
        caption={t("caption", { project })}
        getRowId={(row) => String(row.id)}
        columnClassNames={{ reason: "hidden md:table-cell" }}
        testId="release-checks"
        mobile={mobile}
      />
    </section>
  );
}
