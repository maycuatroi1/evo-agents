"use client";

import { Goal, Signpost } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { Identifier, NAME_LINK } from "@/components/data/identifier";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { decideHref, goalsHref, goalsQuery, LOOP_REFRESH_MS, type LoopGoal, loopGoalsQuery, type ProjectGoal } from "@/lib/loop-queries";
import { cn } from "@/lib/utils";

import { GoalStatusBadge } from "./badges";
import { GoalProgress, OpenItems, ReachedBadge, useMetricText } from "./goal-parts";

type Row = { goal: ProjectGoal; figure: LoopGoal | null };

const NARROW_HIDDEN = { progress: "hidden sm:table-cell", items: "hidden lg:table-cell", deadline: "hidden xl:table-cell", owner: "hidden md:table-cell" };

/** The project's goals as one table: each opens its page, with its metric now against its target, its status and its open work items. */
function GoalsTable({ project, rows }: { project: string; rows: Row[] }) {
  const t = useTranslations("loop.goals");
  const metricText = useMetricText();
  const columns = useMemo(() => {
    const helper = dataTableColumns<Row>();
    return helper.columns([
      helper.accessor((row) => row.goal.code, {
        id: "goal",
        header: () => t("columns.goal"),
        meta: { primary: true },
        enableSorting: false,
        cell: (info) => {
          const { goal } = info.row.original;
          const metric = goal.metric ? metricText(goal.metric) : t("noMetric");
          const scope = goal.srs_document ? t("srsScope", { scope: goal.srs_heading ? `${goal.srs_document} / ${goal.srs_heading}` : goal.srs_document }) : null;
          const sub = scope ? `${metric}, ${scope}` : metric;
          return (
            <CellMain sub={sub} subTitle={sub}>
              <Identifier value={goal.code} />
              <Link href={goalsHref(project, goal.code)} className={cn(NAME_LINK, "truncate")} title={goal.statement} data-testid="goal-link" data-goal={goal.code}>
                {goal.statement}
              </Link>
            </CellMain>
          );
        },
      }),
      helper.display({
        id: "progress",
        header: () => t("columns.now"),
        cell: (info) => {
          const { goal, figure } = info.row.original;
          return (
            <div className="flex min-w-0 items-center gap-3">
              <GoalProgress metric={goal.metric} figure={figure} />
              {goal.metric ? <ReachedBadge figure={figure} /> : null}
            </div>
          );
        },
      }),
      helper.accessor((row) => row.goal.status, {
        id: "status",
        header: () => t("columns.status"),
        enableSorting: false,
        cell: (info) => <GoalStatusBadge goal={info.row.original.goal} />,
      }),
      helper.display({
        id: "items",
        header: () => t("columns.items"),
        cell: (info) => <OpenItems items={info.row.original.figure?.items ?? null} />,
      }),
      helper.accessor((row) => row.goal.deadline ?? "", {
        id: "deadline",
        header: () => t("columns.deadline"),
        enableSorting: false,
        cell: (info) => <span className="whitespace-nowrap tabular-nums">{info.row.original.goal.deadline ?? "-"}</span>,
      }),
      helper.accessor((row) => row.goal.owner, {
        id: "owner",
        header: () => t("columns.owner"),
        enableSorting: false,
        cell: (info) => <span className="whitespace-nowrap">{info.getValue()}</span>,
      }),
    ]);
  }, [t, project, metricText]);
  return (
    <DataTable
      data={rows}
      columns={columns}
      caption={t("caption", { project })}
      getRowId={(row) => row.goal.code}
      columnClassNames={NARROW_HIDDEN}
      testId="goals-table"
      mobile={({ goal, figure }) => ({
        title: goal.statement,
        titleText: goal.statement,
        href: goalsHref(project, goal.code),
        tags: <Identifier value={goal.code} />,
        status: <GoalStatusBadge goal={goal} />,
        meta: goal.metric ? [metricText(goal.metric), figure?.value != null ? t("now", { value: figure.value }) : t("noSignal")].join(", ") : t("noMetric"),
        data: { goal: goal.code },
      })}
    />
  );
}

/**
 * A project's goals (/p/{project}/goals), for any member with a grant on it: each goal's statement and metric, its
 * value now against its target, its status, its open work items, its deadline and owner, by rank. An admin writes a
 * goal with `evo-agents hub goal set`, and decides the month's review on the Decisions page; here everyone reads.
 */
export function GoalsPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("loop.goals");
  const state = useHubQuery(goalsQuery(browserApi, project), initialError);
  const figures = useHubQuery({ ...loopGoalsQuery(browserApi, project), refetchInterval: LOOP_REFRESH_MS });
  const byCode = new Map((figures.status === "success" ? figures.data : []).map((figure) => [figure.code, figure]));
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="goals-page">
      <PageHeader
        title={t("title")}
        actions={
          <Button asChild variant="secondary">
            <Link href={decideHref(project)} data-testid="goals-decide">
              <Signpost aria-hidden="true" />
              {t("decide")}
            </Link>
          </Button>
        }
      />
      <QueryView state={state} loading={<TableSkeleton rows={3} />}>
        {(goals) =>
          goals.length === 0 ? (
            <EmptyState icon={Goal} title={t("emptyTitle")} description={t("emptyText")} testId="goals-empty">
              <code className="rounded-xs bg-muted px-2 py-1 font-mono text-xs text-foreground">evo-agents hub goal set</code>
            </EmptyState>
          ) : (
            <GoalsTable project={project} rows={goals.map((goal) => ({ goal, figure: byCode.get(goal.code) ?? null }))} />
          )
        }
      </QueryView>
    </div>
  );
}
