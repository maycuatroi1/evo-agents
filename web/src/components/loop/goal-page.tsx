"use client";

import { ChartLine, Goal } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { DataCard, DataToolbar } from "@/components/data/data-card";
import { FacetGroup } from "@/components/data/facet-group";
import { Fact, Facts } from "@/components/data/facts";
import { Identifier } from "@/components/data/identifier";
import { ProblemChips } from "@/components/data/problem-chips";
import { When } from "@/components/data/when";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton, TableSkeleton } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import {
  goalQuery,
  goalsHref,
  ITEM_STATES,
  type ItemState,
  LOOP_REFRESH_MS,
  loopGoalsQuery,
  loopItemsQuery,
  type ProjectGoal,
} from "@/lib/loop-queries";

import { GoalStatusBadge } from "./badges";
import { BaselinePanel } from "./baseline-panel";
import { GoalProgress, ReachedBadge, useMetricText } from "./goal-parts";
import { useLoopViewer } from "./hooks";
import { ItemsTable } from "./items-table";
import { byState, countOf, STATE_LOOKS, totalCount } from "./model";

function signalsHref(project: string, key: string): Route {
  return `/p/${encodeURIComponent(project)}/signals?key=${encodeURIComponent(key)}` as Route;
}

function canvasHref(project: string, slug: string): Route {
  return `/p/${encodeURIComponent(project)}/x/canvas/canvases/${encodeURIComponent(slug)}` as Route;
}

/** The goal's metric now: its value against its target, whether it is reached, and the key's series. */
function MetricCard({ project, goal }: { project: string; goal: ProjectGoal }) {
  const t = useTranslations("loop.goal");
  const metricText = useMetricText();
  const figures = useHubQuery({ ...loopGoalsQuery(browserApi, project), refetchInterval: LOOP_REFRESH_MS });
  const figure = figures.status === "success" ? (figures.data.find((found) => found.code === goal.code) ?? null) : null;
  return (
    <section aria-labelledby="goal-metric-title" className="min-w-0 rounded-md border bg-card shadow-raised" data-testid="goal-metric">
      <div className="flex min-h-12 flex-wrap items-center gap-2 border-b px-4 py-2">
        <h2 id="goal-metric-title" className="text-[15px] leading-[22px] font-semibold">
          {t("metricTitle")}
        </h2>
        {goal.metric ? <ReachedBadge figure={figure} /> : null}
        {goal.metric ? (
          <Link href={signalsHref(project, goal.metric.key)} className="ml-auto inline-flex items-center gap-1.5 text-[13px] text-brand hover:text-brand-hover" data-testid="goal-signals">
            <ChartLine className="size-4" aria-hidden="true" />
            {t("series")}
          </Link>
        ) : null}
      </div>
      <div className="flex flex-col gap-3 px-4 py-4">
        <GoalProgress metric={goal.metric} figure={figure} large />
        <p className="text-[13px] text-muted-foreground">{goal.metric ? metricText(goal.metric) : t("noMetricText")}</p>
      </div>
    </section>
  );
}

/** The baseline and backlog of a goal that aims at an SRS document; an admin confirms the baseline here too. */
function BacklogCard({ project, goal }: { project: string; goal: ProjectGoal }) {
  const t = useTranslations("loop.goal");
  const viewer = useLoopViewer(project);
  return (
    <section aria-labelledby="goal-backlog-title" className="min-w-0 rounded-md border bg-card shadow-raised" data-testid="goal-backlog">
      <div className="flex min-h-12 items-center border-b px-4 py-2">
        <h2 id="goal-backlog-title" className="text-[15px] leading-[22px] font-semibold">
          {t("backlogTitle")}
        </h2>
      </div>
      <div className="px-4 py-4">
        <BaselinePanel project={project} goal={goal.code} admin={viewer?.admin ?? false} />
      </div>
    </section>
  );
}

function FactsCard({ project, goal }: { project: string; goal: ProjectGoal }) {
  const t = useTranslations("loop.goal");
  return (
    <section aria-labelledby="goal-facts-title" className="min-w-0 rounded-md border bg-card shadow-raised" data-testid="goal-facts">
      <div className="flex min-h-12 items-center border-b px-4 py-2">
        <h2 id="goal-facts-title" className="text-[15px] leading-[22px] font-semibold">
          {t("factsTitle")}
        </h2>
      </div>
      <Facts>
        <Fact label={t("problem")} testId="goal-problem">
          {goal.problem ? <ProblemChips project={project} codes={[goal.problem]} detail /> : t("noProblem")}
        </Fact>
        {goal.opportunity ? (
          <Fact label={t("opportunity")} testId="goal-opportunity">
            <Identifier value={goal.opportunity} />
          </Fact>
        ) : null}
        {goal.srs_document ? (
          <Fact label={t("srsScope")} testId="goal-srs-scope">
            {goal.srs_heading ? t("srsScopeHeading", { document: goal.srs_document, heading: goal.srs_heading }) : goal.srs_document}
          </Fact>
        ) : null}
        <Fact label={t("deadline")}>{goal.deadline ?? t("noDeadline")}</Fact>
        <Fact label={t("owner")}>{goal.owner}</Fact>
        <Fact label={t("canvas")}>{goal.canvas ? <Identifier value={goal.canvas} href={canvasHref(project, goal.canvas)} /> : t("noCanvas")}</Fact>
        <Fact label={t("rank")}>{goal.rank}</Fact>
        <Fact label={t("revision")}>{t("revisionText", { revision: goal.revision, by: goal.created_by })}</Fact>
        <Fact label={t("changed")}>
          <When value={goal.created_at} />
        </Fact>
        {goal.note ? <Fact label={t("note")}>{goal.note}</Fact> : null}
      </Facts>
    </section>
  );
}

/** The goal's work items, filtered by state with each state's count, by state then newest. */
function GoalItems({ project, goal }: { project: string; goal: ProjectGoal }) {
  const t = useTranslations("loop.goal");
  const items = useTranslations("loop.items");
  const words = useTranslations("loop.state");
  const [state, setState] = useState<ItemState | null>(null);
  const figures = useHubQuery(loopGoalsQuery(browserApi, project));
  const counts = figures.status === "success" ? figures.data.find((found) => found.code === goal.code)?.items : undefined;
  const found = useHubQuery({ ...loopItemsQuery(browserApi, project, { state, kind: null, goal: goal.code, open: false }), refetchInterval: LOOP_REFRESH_MS });
  const options = [
    { value: null, label: items("all"), count: counts ? totalCount(counts) : undefined },
    ...ITEM_STATES.map((value) => ({ value, label: words(value), icon: STATE_LOOKS[value].icon, count: counts ? countOf(counts, value) : undefined })),
  ];
  const shown = found.status === "success" ? found.data.items : [];
  return (
    <section aria-labelledby="goal-items-title" className="min-w-0">
      <h2 id="goal-items-title" className="mb-3 text-[15px] leading-[22px] font-semibold">
        {t("itemsTitle")}
      </h2>
      <DataCard testId="goal-items" busy={found.status === "loading"}>
        <DataToolbar label={t("itemsSearch", { goal: goal.code })} count={items("count", { count: shown.length })}>
          <FacetGroup
            label={items("stateLabel")}
            options={options}
            selected={state}
            onSelect={(value) => setState(value as ItemState | null)}
            countLabel={(count) => items("count", { count })}
            testId="goal-item-states"
          />
        </DataToolbar>
        <QueryView state={found} loading={<TableSkeleton rows={4} />}>
          {(data) => <ItemsTable project={project} items={byState(data.items)} caption={t("itemsCaption", { goal: goal.code })} withGoal={false} testId="goal-items-table" />}
        </QueryView>
      </DataCard>
    </section>
  );
}

/**
 * One goal (/p/{project}/goals/{code}), for any member with a grant on the project: its statement and status, the
 * problem it answers, its metric now against its target, what else its revision says, and its work items by state. An admin writes it with
 * `evo-agents hub goal set`; this page only reads.
 */
export function GoalPage({ project, code, initialError }: { project: string; code: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("loop.goal");
  const state = useHubQuery(goalQuery(browserApi, project, code), initialError);
  if (state.status === "error" && state.error.status === 404) {
    return <NotFoundState title={t("notFoundTitle", { code })} description={t("notFoundText")} />;
  }
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(goal) => (
        <div className="flex min-w-0 flex-col gap-6" data-testid="goal-page" data-goal={goal.code}>
          <PageHeader
            title={goal.statement}
            status={<GoalStatusBadge goal={goal} size="lg" />}
            tags={
              <>
                <Identifier value={goal.code} testId="goal-code" />
                {goal.problem ? <ProblemChips project={project} codes={[goal.problem]} marked testId="goal-problems" /> : null}
              </>
            }
            sub={
              <Link href={goalsHref(project)} className="inline-flex items-center gap-1 text-brand hover:text-brand-hover">
                <Goal className="size-3.5" aria-hidden="true" />
                {t("back")}
              </Link>
            }
          />
          <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_22rem]">
            <MetricCard project={project} goal={goal} />
            <FactsCard project={project} goal={goal} />
          </div>
          {goal.srs_document ? <BacklogCard project={project} goal={goal} /> : null}
          <GoalItems project={project} goal={goal} />
        </div>
      )}
    </QueryView>
  );
}
