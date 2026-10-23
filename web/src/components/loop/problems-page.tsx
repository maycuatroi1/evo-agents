"use client";

import { Bandage, Plus } from "lucide-react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { DataCard, DataToolbar } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { FacetGroup } from "@/components/data/facet-group";
import { Identifier, NAME_LINK } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, NoResults, TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { PROBLEM_STATUSES, type ProblemStatus, problemsHref, problemsQuery, type ProjectProblem } from "@/lib/problem-queries";
import { cn } from "@/lib/utils";

import { NewProblemDialog } from "./problem-dialog";
import { useProblemViewer } from "./problem-hooks";
import { byStatusThenRank, readStatusFilter, statusCounts } from "./problem-model";
import { useProblemMetricText } from "./problem-parts";
import { PROBLEM_LOOKS, ProblemStatusBadge } from "./problem-status";

const NARROW_HIDDEN = { metric: "hidden md:table-cell", owner: "hidden lg:table-cell", changed: "hidden xl:table-cell" };

/** The project's problems as one table: rank, then each problem opening its page, with who it hurts, its metric, status, owner and last change. */
function ProblemsTable({ project, problems }: { project: string; problems: ProjectProblem[] }) {
  const t = useTranslations("problems.list");
  const metricText = useProblemMetricText();
  const columns = useMemo(() => {
    const helper = dataTableColumns<ProjectProblem>();
    return helper.columns([
      helper.accessor("rank", {
        header: () => t("columns.rank"),
        enableSorting: false,
        meta: { numeric: true },
        cell: (info) => <span className="tabular-nums text-muted-foreground">{info.getValue()}</span>,
      }),
      helper.accessor("statement", {
        id: "problem",
        header: () => t("columns.problem"),
        meta: { primary: true },
        enableSorting: false,
        cell: (info) => {
          const problem = info.row.original;
          const sub = t("who", { who: problem.who });
          return (
            <CellMain sub={sub} subTitle={sub}>
              <Identifier value={problem.code} className="shrink-0" />
              <Link
                href={problemsHref(project, problem.code)}
                className={cn(NAME_LINK, "truncate")}
                title={problem.statement}
                data-testid="problem-link"
                data-problem={problem.code}
              >
                {problem.statement}
              </Link>
            </CellMain>
          );
        },
      }),
      helper.accessor((row) => row.metric?.key ?? "", {
        id: "metric",
        header: () => t("columns.metric"),
        enableSorting: false,
        cell: (info) => {
          const { metric } = info.row.original;
          return metric ? (
            <span className="block max-w-64 truncate font-mono text-xs" title={metricText(metric)}>
              {metricText(metric)}
            </span>
          ) : (
            <span className="text-[13px] text-fg-subtle">{t("noMetric")}</span>
          );
        },
      }),
      helper.accessor("status", {
        header: () => t("columns.status"),
        enableSorting: false,
        cell: (info) => <ProblemStatusBadge status={info.getValue()} />,
      }),
      helper.accessor("owner", {
        header: () => t("columns.owner"),
        enableSorting: false,
        cell: (info) => <span className="font-mono text-xs whitespace-nowrap">{info.getValue()}</span>,
      }),
      helper.accessor("created_at", {
        id: "changed",
        header: () => t("columns.changed"),
        enableSorting: false,
        meta: { numeric: true },
        cell: (info) => <When value={info.getValue()} short />,
      }),
    ]);
  }, [t, project, metricText]);
  return (
    <DataTable
      data={problems}
      columns={columns}
      caption={t("caption", { project })}
      getRowId={(problem) => problem.code}
      columnClassNames={NARROW_HIDDEN}
      testId="problems-table"
      mobile={(problem) => ({
        title: problem.statement,
        titleText: problem.statement,
        href: problemsHref(project, problem.code),
        tags: <Identifier value={problem.code} />,
        status: <ProblemStatusBadge status={problem.status} />,
        meta: t("mobileMeta", { rank: problem.rank, who: problem.who }),
        data: { problem: problem.code },
      })}
    />
  );
}

/** The status filter, read from the address (`?status=`) when the page opens and written back to it, so a link keeps it. */
function useStatusFilter(): [ProblemStatus | null, (status: ProblemStatus | null) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const [status, setStatus] = useState(() => readStatusFilter(params));
  const set = (next: ProblemStatus | null) => {
    setStatus(next);
    window.history.replaceState(null, "", `${pathname}${next ? `?${new URLSearchParams({ status: next })}` : ""}`);
  };
  return [status, set];
}

function Problems({ project, problems, writer, onNew }: { project: string; problems: ProjectProblem[]; writer: boolean; onNew: () => void }) {
  const t = useTranslations("problems.list");
  const words = useTranslations("problems.status");
  const [status, setStatus] = useStatusFilter();
  const counts = statusCounts(problems);
  if (problems.length === 0) {
    return (
      <EmptyState icon={Bandage} title={t("emptyTitle")} description={t("emptyText")} testId="problems-empty">
        {writer ? (
          <Button type="button" onClick={onNew} data-testid="problems-new-empty">
            <Plus aria-hidden="true" />
            {t("new")}
          </Button>
        ) : (
          <code className="rounded-xs bg-muted px-2 py-1 font-mono text-xs text-foreground">evo-agents hub problem set</code>
        )}
      </EmptyState>
    );
  }
  const shown = byStatusThenRank(status ? problems.filter((problem) => problem.status === status) : problems);
  const options = [
    { value: null, label: t("all"), count: problems.length },
    ...PROBLEM_STATUSES.map((value) => ({ value, label: words(value), icon: PROBLEM_LOOKS[value].icon, count: counts[value] })),
  ];
  return (
    <DataCard
      testId="problems-card"
      toolbar={
        <DataToolbar label={t("search")} count={t("count", { count: shown.length })} countTestId="problems-count">
          <FacetGroup
            label={t("statusLabel")}
            options={options}
            selected={status}
            onSelect={(value) => setStatus(value as ProblemStatus | null)}
            countLabel={(count) => t("count", { count })}
            testId="problem-statuses"
          />
        </DataToolbar>
      }
    >
      {shown.length === 0 && status ? (
        <NoResults title={t("noMatch")} filters={[{ label: t("statusLabel"), value: words(status) }]} onClear={() => setStatus(null)} />
      ) : (
        <ProblemsTable project={project} problems={shown} />
      )}
    </DataCard>
  );
}

/**
 * A project's problems (/p/{project}/problems), for any member with a grant on it: what hurts, before any solution,
 * the open ones first and each status by rank, filtered by status. A writer adds one here (New problem) or with
 * `evo-agents hub problem set`; goals, claims and plans point back to them.
 */
export function ProblemsPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("problems.list");
  const state = useHubQuery(problemsQuery(browserApi, project), initialError);
  const viewer = useProblemViewer(project);
  const writer = viewer?.writer ?? false;
  const [creating, setCreating] = useState(false);
  const known = state.status === "success" ? state.data : [];
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="problems-page">
      <PageHeader
        title={t("title")}
        actions={
          writer && known.length > 0 ? (
            <Button type="button" onClick={() => setCreating(true)} data-testid="problems-new">
              <Plus aria-hidden="true" />
              {t("new")}
            </Button>
          ) : null
        }
      />
      <QueryView state={state} loading={<TableSkeleton rows={3} toolbar />}>
        {(problems) => <Problems project={project} problems={problems} writer={writer} onNew={() => setCreating(true)} />}
      </QueryView>
      {writer ? <NewProblemDialog project={project} problems={known} open={creating} onOpenChange={setCreating} /> : null}
    </div>
  );
}
