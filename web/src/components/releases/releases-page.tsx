"use client";

import { Tags } from "lucide-react";
import { useTranslations } from "next-intl";

import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { ActionsBanner, CycleCard, NoCycle, StoppedBanner } from "./cycle-card";
import { lacksActions } from "./model";
import { PastCycles } from "./past-cycles";
import { ReleaseHeader } from "./release-header";
import { ChecksTable, LastCheckCard, ScheduleCard } from "./schedule-card";
import { releaseStatusQuery, type ReleaseStatus } from "./queries";

/** The CLI command that sets a schedule, for the empty state: the page itself does not set one. */
const SET_COMMAND = "evo-agents hub release set --project";

/** A project without a release schedule: what the daily release does, and the command an admin sets it with. */
function NoSchedule({ project }: { project: string }) {
  const t = useTranslations("releases.empty");
  return (
    <EmptyState
      icon={Tags}
      title={t("title")}
      description={
        <>
          <p>{t("description")}</p>
          <p className="mt-2">
            {t.rich("command", {
              command: () => (
                <code className="rounded-xs bg-muted px-1 font-mono text-xs [overflow-wrap:anywhere]" data-testid="release-set-command">
                  {SET_COMMAND} {project}
                </code>
              ),
            })}
          </p>
        </>
      }
    />
  );
}

function Release({ project, status }: { project: string; status: ReleaseStatus }) {
  const schedule = status.schedule;
  if (!schedule) return <NoSchedule project={project} />;
  const cycle = status.cycle;
  return (
    <>
      {lacksActions(status) ? <ActionsBanner schedule={schedule} /> : null}
      {cycle ? <StoppedBanner cycle={cycle} /> : null}
      {cycle ? <CycleCard project={project} cycle={cycle} schedule={schedule} /> : <NoCycle status={status} />}
      <div className="grid items-start gap-4 lg:grid-cols-2">
        <ScheduleCard status={status} schedule={schedule} />
        <LastCheckCard status={status} schedule={schedule} />
      </div>
      {status.checks.length > 1 ? <ChecksTable project={project} checks={status.checks.slice(1)} /> : null}
      <PastCycles project={project} repo={schedule.github_repo} />
    </>
  );
}

/**
 * A project's daily release: its schedule (paused or not, the next check, its owner and cap), the newest check and
 * those before it, the cycle open now (its tags with their CI and workflow runs, its cut and fix runs, its cost against
 * its cap, and why it stopped), and the cycles that ended. Read every 10 seconds while the cycle has work in flight,
 * every minute otherwise; a state shows only when something needs a word: paused, stopped, failed.
 */
export function ReleasesPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const status = useHubQuery(releaseStatusQuery(browserApi, project), initialError, { live: true });
  return (
    <QueryView state={status} loading={<ListSkeleton rows={5} />}>
      {(found) => (
        <div className="flex min-w-0 flex-col gap-6" data-testid="releases-page" data-cycle={found.cycle?.state ?? "none"} data-paused={found.paused || undefined}>
          <ReleaseHeader project={project} status={found} />
          <Release project={project} status={found} />
        </div>
      )}
    </QueryView>
  );
}
