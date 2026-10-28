"use client";

import { MessageSquare, MessagesSquare, OctagonPause, ShieldAlert } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { InFrame } from "@/components/data/data-card";
import { Fact, Facts, useDay, useMoney } from "@/components/data/facts";
import { Identifier } from "@/components/data/identifier";
import { Banner } from "@/components/feedback/banner";
import { Button } from "@/components/ui/button";

import { CycleBadge } from "./badges";
import { RunsTable, TagsTable } from "./cycle-tables";
import { githubCommitUrl, githubTagUrl, shortCommit } from "./model";
import { Panel, PanelLine, PathList, Relative } from "./parts";
import type { ReleaseCycle, ReleaseSchedule, ReleaseStatus } from "./queries";

/** The decision a stopped cycle asks its owner, in the Inbox's sheet. */
function decisionHref(id: number): Route {
  return `/inbox?decision=${id}` as Route;
}

/** A stopped cycle: why, in `attention` since its owner must answer, with the decision and the triage thread a click away. */
export function StoppedBanner({ cycle }: { cycle: ReleaseCycle }) {
  const t = useTranslations("releases.problems.stopped");
  const tCause = useTranslations("releases.cause");
  if (cycle.state !== "stopped") return null;
  const cause = cycle.stop_cause ? tCause(cycle.stop_cause) : t("noCause");
  return (
    <Banner
      icon={OctagonPause}
      tone="attention"
      title={t("title", { id: cycle.id, cause })}
      testId="release-stopped"
      actions={
        <>
          {cycle.decision_id !== null ? (
            <Button asChild variant="secondary" size="sm">
              <Link href={decisionHref(cycle.decision_id)} data-testid="release-decision-link">
                <MessageSquare aria-hidden="true" />
                {t("decision")}
              </Link>
            </Button>
          ) : null}
          {cycle.thread ? (
            <Button asChild variant="secondary" size="sm">
              <Link href={cycle.thread as Route} data-testid="release-thread-link">
                <MessagesSquare aria-hidden="true" />
                {t("thread")}
              </Link>
            </Button>
          ) : null}
        </>
      }
    >
      <p>{cycle.stop_reason ?? t("noReason")}</p>
      <p className="mt-1">{t("waits", { owner: cycle.owner })}</p>
    </Banner>
  );
}

/** The App's installation grants no Actions: the hub reads no CI and fixes no red tag until a member accepts it. */
export function ActionsBanner({ schedule }: { schedule: ReleaseSchedule }) {
  const t = useTranslations("releases.problems.actions");
  return (
    <Banner icon={ShieldAlert} tone="danger" role="alert" title={t("title", { workflow: schedule.workflow })} testId="release-actions-permission">
      {t("text", { repo: schedule.github_repo ?? schedule.repo })}
    </Banner>
  );
}

/** What the cycle's runs cost so far: a cycle has no cap. */
function CycleCost({ cycle }: { cycle: ReleaseCycle }) {
  const money = useMoney();
  return (
    <span className="tabular-nums" data-testid="release-cycle-cost">
      {money(cycle.cost_usd)}
    </span>
  );
}

/** The cycle open now, or stopped: where it started, what changed, its cost, its tags and its runs. */
export function CycleCard({ project, cycle, schedule }: { project: string; cycle: ReleaseCycle; schedule: ReleaseSchedule }) {
  const t = useTranslations("releases.cycle");
  const day = useDay();
  const repo = schedule.github_repo;
  return (
    <Panel
      title={t("title", { id: cycle.id })}
      status={cycle.state === "stopped" ? <CycleBadge state="stopped" /> : null}
      sub={t("sub", { day: day(cycle.local_day, "long"), owner: cycle.owner })}
      testId="release-cycle"
    >
      <Facts>
        <Fact label={t("opened")}>
          <Relative value={cycle.opened_at} />
        </Fact>
        <Fact label={t("start")} testId="release-cycle-start">
          {cycle.start === "fix" ? t("startFix", { tag: cycle.base_tag ?? "-" }) : t("startCut", { ref: cycle.target_ref })}
        </Fact>
        <Fact label={t("range")}>
          <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-1">
            {cycle.base_tag ? (
              <Identifier value={cycle.base_tag} externalHref={githubTagUrl(repo, cycle.base_tag)} linkLabel={t("tagLink", { tag: cycle.base_tag })} />
            ) : (
              <span className="text-muted-foreground">{t("noBase")}</span>
            )}
            <span className="text-fg-subtle">{t("to")}</span>
            <Identifier
              value={cycle.target_commit ?? cycle.target_ref}
              externalHref={githubCommitUrl(repo, cycle.target_commit)}
              linkLabel={t("commitLink", { commit: shortCommit(cycle.target_commit) })}
              title={cycle.target_commit ?? undefined}
            >
              {cycle.target_commit ? `${cycle.target_ref} ${shortCommit(cycle.target_commit)}` : cycle.target_ref}
            </Identifier>
          </span>
        </Fact>
        <Fact label={t("changed")}>
          <PathList paths={cycle.changed_paths} empty={t("noChange")} testId="release-cycle-paths" />
        </Fact>
        <Fact label={t("cost")}>
          <CycleCost cycle={cycle} />
        </Fact>
      </Facts>
      <div className="flex flex-col gap-2 border-t pt-3">
        <h3 className="px-4 text-[13px] leading-[18px] font-semibold">{t("tags")}</h3>
        {cycle.tags.length > 0 ? (
          <InFrame>
            <TagsTable project={project} cycle={cycle} repo={repo} />
          </InFrame>
        ) : (
          <PanelLine testId="release-tags-none">{t("noTags")}</PanelLine>
        )}
      </div>
      <div className="flex flex-col gap-2 border-t pt-3">
        <h3 className="px-4 text-[13px] leading-[18px] font-semibold">{t("runs")}</h3>
        {cycle.runs.length > 0 ? (
          <InFrame>
            <RunsTable project={project} cycle={cycle} />
          </InFrame>
        ) : (
          <PanelLine testId="release-runs-none">{t("noRuns")}</PanelLine>
        )}
      </div>
    </Panel>
  );
}

/** No cycle open: one quiet line, and when the next check looks. */
export function NoCycle({ status }: { status: ReleaseStatus }) {
  const t = useTranslations("releases.cycle.none");
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md border bg-surface-sunken px-4 py-3 text-[13px] leading-[18px]" data-testid="release-no-cycle">
      <p className="min-w-0 flex-1 text-pretty text-muted-foreground">
        <span className="font-semibold text-foreground">{t("title")}</span>{" "}
        {status.paused
          ? t("paused")
          : status.next_check_at
            ? t.rich("next", { next: () => <Relative value={status.next_check_at as string} /> })
            : t("noNext")}
      </p>
    </div>
  );
}
