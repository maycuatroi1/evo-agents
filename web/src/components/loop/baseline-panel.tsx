"use client";

import { CircleCheck, ClipboardCheck, FileText, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Identifier, Tag } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { Banner } from "@/components/feedback/banner";
import { ConfirmAction } from "@/components/feedback/confirm-action";
import { useWriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ListSkeleton, TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { baselineQuery, type BaselineItem, goalsHref, goalsQuery, LOOP_REFRESH_MS, loopItemsQuery, type SrsBaseline } from "@/lib/loop-queries";

import { useConfirmBaseline } from "./hooks";
import { ItemsTable } from "./items-table";
import { BASELINE_STATES, backlogGroups, backlogOpen, byState } from "./model";

/** The counts of a baseline by state, open ones first: "2 undelivered, 1 unknown". */
function StateCounts({ counts }: { counts: Partial<Record<BaselineItem["state"], number>> }) {
  const t = useTranslations("loop.baseline");
  return (
    <ul className="flex flex-wrap gap-1.5" data-testid="baseline-counts">
      {BASELINE_STATES.filter((state) => (counts[state] ?? 0) > 0).map((state) => (
        <li key={state}>
          <Tag data-state={state}>{t("count", { label: t(`state.${state}`), count: counts[state] ?? 0 })}</Tag>
        </li>
      ))}
    </ul>
  );
}

/** The use cases of a baseline by group (their heading): each group with its counts, and under it each use case with its state and its evidence. */
export function BacklogGroups({ baseline }: { baseline: SrsBaseline }) {
  const t = useTranslations("loop.baseline");
  const groups = backlogGroups(baseline.items);
  if (groups.length === 0) return <p className="text-[13px] text-fg-subtle">{t("noUseCases")}</p>;
  return (
    <ul className="flex flex-col divide-y rounded-sm border" data-testid="baseline-groups">
      {groups.map((group) => (
        <li key={group.heading ?? ""} className="flex flex-col gap-2 px-3 py-2" data-testid="baseline-group" data-heading={group.heading ?? ""}>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="text-[13px] font-medium">{group.heading ?? t("noHeading")}</span>
            <StateCounts counts={group.counts} />
          </div>
          <ul className="flex flex-col gap-1">
            {group.items.map((item) => (
              <li key={item.code} className="flex flex-wrap items-baseline gap-x-2 text-[13px]" data-testid="baseline-item" data-code={item.code} data-state={item.state}>
                <Identifier value={item.code} />
                <span className="min-w-0 [overflow-wrap:anywhere]">{item.title}</span>
                <span className="text-xs text-fg-subtle">{t(`state.${item.state}`)}</span>
                {item.reason ? <span className="text-xs text-fg-subtle [overflow-wrap:anywhere]">{item.reason}</span> : null}
              </li>
            ))}
          </ul>
        </li>
      ))}
    </ul>
  );
}

/** The admin's confirmation of the baseline: until it, the Loop makes no build for the goal. */
function ConfirmBaseline({ project, goal, baseline }: { project: string; goal: string; baseline: SrsBaseline }) {
  const t = useTranslations("loop.baseline");
  const [open, setOpen] = useState(false);
  const write = useConfirmBaseline(project, goal);
  const failure = useWriteFailure();
  const error = write.isError && open ? failure(write.error, { 403: t("forbidden"), 409: t("conflict") }) : null;
  const confirm = async () => {
    try {
      await write.mutateAsync();
      setOpen(false);
      notify({ tone: "success", text: t("done"), description: t("doneText", { goal }) });
    } catch {
      // The reason shows in the dialog, which stays open.
    }
  };
  return (
    <>
      <Button
        type="button"
        onClick={() => {
          write.reset();
          setOpen(true);
        }}
        disabled={!baseline.graph}
        data-testid="baseline-confirm"
      >
        <ClipboardCheck aria-hidden="true" />
        {t("confirm")}
      </Button>
      <ConfirmAction
        open={open}
        onOpenChange={setOpen}
        title={t("confirmTitle", { goal })}
        description={<p>{t("confirmText", { open: backlogOpen(baseline.items), total: baseline.items.length })}</p>}
        confirmLabel={t("confirm")}
        pendingLabel={t("confirming")}
        cancelLabel={t("cancel")}
        pending={write.isPending}
        error={error}
        onConfirm={() => void confirm()}
        testId="baseline-confirm-dialog"
      />
    </>
  );
}

function Body({ project, goal, baseline, admin }: { project: string; goal: string; baseline: SrsBaseline; admin: boolean }) {
  const t = useTranslations("loop.baseline");
  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid="baseline" data-confirmed={baseline.confirmed ? "true" : "false"}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <Tag>
          <FileText aria-hidden="true" />
          {baseline.heading ? t("scopeHeading", { document: baseline.document, heading: baseline.heading }) : baseline.document}
        </Tag>
        {baseline.confirmed ? (
          <span className="inline-flex items-center gap-1 text-[13px] text-success" data-testid="baseline-confirmed">
            <CircleCheck className="size-4" aria-hidden="true" />
            {t("confirmed", { by: baseline.confirmed_by ?? "-" })}
            {baseline.confirmed_at ? <When value={baseline.confirmed_at} short /> : null}
          </span>
        ) : (
          <span className="text-[13px] text-attention" data-testid="baseline-proposed">
            {t("proposed")}
          </span>
        )}
        <StateCounts counts={baseline.counts} />
        {!baseline.confirmed && admin ? (
          <span className="ml-auto">
            <ConfirmBaseline project={project} goal={goal} baseline={baseline} />
          </span>
        ) : null}
      </div>
      {!baseline.graph ? (
        <Banner icon={TriangleAlert} tone="attention" title={t("noGraphTitle")} testId="baseline-no-graph">
          {baseline.why ?? t("noGraphText")}
        </Banner>
      ) : null}
      <BacklogGroups baseline={baseline} />
    </div>
  );
}

/**
 * The baseline of a goal that aims at an SRS document, as the hub proposes it from the knowledge graph: each approved
 * use case by group with its state (delivered, undelivered, unknown, delivering) and why. Until an admin confirms it
 * the Loop makes no build for the goal; the button is the admin's, and the page decides nothing else here.
 */
export function BaselinePanel({ project, goal, admin }: { project: string; goal: string; admin: boolean }) {
  const state = useHubQuery(baselineQuery(browserApi, project, goal));
  return (
    <QueryView state={state} loading={<ListSkeleton rows={3} />}>
      {(baseline) => <Body project={project} goal={goal} baseline={baseline} admin={admin} />}
    </QueryView>
  );
}

/** The build work items of one goal of the Loop, each for a group of its backlog. */
function GoalBuilds({ project, goal }: { project: string; goal: string }) {
  const t = useTranslations("loop.baseline");
  const state = useHubQuery({ ...loopItemsQuery(browserApi, project, { state: null, kind: "build", goal, open: false }), refetchInterval: LOOP_REFRESH_MS });
  return (
    <QueryView state={state} loading={<TableSkeleton rows={2} />}>
      {(found) =>
        found.items.length === 0 ? (
          <p className="text-[13px] text-fg-subtle" data-testid="backlog-no-builds">
            {t("noBuilds")}
          </p>
        ) : (
          <ItemsTable project={project} items={byState(found.items)} caption={t("buildsCaption", { goal })} withGoal={false} testId="backlog-builds" />
        )
      }
    </QueryView>
  );
}

/**
 * The backlog of each active goal of the project that aims at an SRS document, on the Loop page: its baseline with
 * the use cases by group and their states, and the builds the Loop made for the groups. Nothing without such a goal.
 */
export function SrsBacklog({ project, admin }: { project: string; admin: boolean }) {
  const t = useTranslations("loop.baseline");
  const state = useHubQuery(goalsQuery(browserApi, project));
  const goals = state.status === "success" ? state.data.filter((goal) => goal.srs_document && goal.status === "active") : [];
  if (goals.length === 0) return null;
  return (
    <section aria-labelledby="loop-backlog-title" className="flex min-w-0 flex-col gap-4" data-testid="loop-backlog">
      <h2 id="loop-backlog-title" className="text-[15px] leading-[22px] font-semibold">
        {t("title")}
      </h2>
      {goals.map((goal) => (
        <div key={goal.code} className="flex min-w-0 flex-col gap-3 rounded-md border bg-card px-4 py-4 shadow-raised" data-testid="loop-backlog-goal" data-goal={goal.code}>
          <div className="flex flex-wrap items-center gap-2">
            <Identifier value={goal.code} href={goalsHref(project, goal.code)} />
            <span className="text-[13px] text-muted-foreground [overflow-wrap:anywhere]">{goal.statement}</span>
          </div>
          <BaselinePanel project={project} goal={goal.code} admin={admin} />
          <GoalBuilds project={project} goal={goal.code} />
        </div>
      ))}
    </section>
  );
}
