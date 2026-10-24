"use client";

import { Plus, Telescope } from "lucide-react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { DataCard, DataToolbar } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { FacetGroup } from "@/components/data/facet-group";
import { Identifier, NAME_LINK } from "@/components/data/identifier";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, NoResults, TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { OPPORTUNITY_STATUSES, type OpportunityStatus, opportunitiesHref, opportunitiesQuery, type ProjectOpportunity } from "@/lib/opportunity-queries";
import { cn } from "@/lib/utils";

import { AnswerChips } from "./answer-chips";
import { NewOpportunityDialog } from "./opportunity-dialog";
import { useOpportunityViewer } from "./opportunity-hooks";
import { byClosingWindow, readStatusFilter, statusCounts } from "./opportunity-model";
import { useDaysLeftText } from "./opportunity-parts";
import { OPPORTUNITY_LOOKS, OpportunityStatusBadge } from "./opportunity-status";

const NARROW_HIDDEN = { answers: "hidden md:table-cell", owner: "hidden lg:table-cell" };

/** The project's opportunities as one table: each opening its page, with who gains, when its window closes, its status and the problems it answers. */
function OpportunitiesTable({ project, opportunities }: { project: string; opportunities: ProjectOpportunity[] }) {
  const t = useTranslations("opportunities.list");
  const left = useDaysLeftText();
  const columns = useMemo(() => {
    const helper = dataTableColumns<ProjectOpportunity>();
    return helper.columns([
      helper.accessor("statement", {
        id: "opportunity",
        header: () => t("columns.opportunity"),
        meta: { primary: true },
        enableSorting: false,
        cell: (info) => {
          const one = info.row.original;
          const sub = t("beneficiary", { who: one.beneficiary });
          return (
            <CellMain sub={sub} subTitle={sub}>
              <Identifier value={one.code} className="shrink-0" />
              <Link
                href={opportunitiesHref(project, one.code)}
                className={cn(NAME_LINK, "truncate")}
                title={one.statement}
                data-testid="opportunity-link"
                data-opportunity={one.code}
              >
                {one.statement}
              </Link>
            </CellMain>
          );
        },
      }),
      helper.accessor("status", {
        header: () => t("columns.status"),
        enableSorting: false,
        cell: (info) => <OpportunityStatusBadge status={info.getValue()} />,
      }),
      helper.accessor((row) => row.window_until ?? "", {
        id: "closes",
        header: () => t("columns.closes"),
        enableSorting: false,
        cell: (info) => {
          const { window_until: until, status } = info.row.original;
          if (!until) return <span className="text-[13px] text-fg-subtle">{t("noWindow")}</span>;
          return (
            <span className="block whitespace-nowrap text-[13px]" data-testid="opportunity-closes">
              <span className="font-mono text-xs">{until}</span>
              {status === "open" ? <span className="block text-muted-foreground">{left(until)}</span> : null}
            </span>
          );
        },
      }),
      helper.accessor((row) => row.answers.length + row.hidden_answers, {
        id: "answers",
        header: () => t("columns.answers"),
        enableSorting: false,
        cell: (info) => {
          const one = info.row.original;
          return one.answers.length + one.hidden_answers === 0 ? (
            <span className="text-[13px] text-fg-subtle">{t("noAnswers")}</span>
          ) : (
            <AnswerChips project={project} answers={one.answers} hidden={one.hidden_answers} />
          );
        },
      }),
      helper.accessor("owner", {
        header: () => t("columns.owner"),
        enableSorting: false,
        cell: (info) => <span className="font-mono text-xs whitespace-nowrap">{info.getValue()}</span>,
      }),
    ]);
  }, [t, project, left]);
  return (
    <DataTable
      data={opportunities}
      columns={columns}
      caption={t("caption", { project })}
      getRowId={(one) => one.code}
      columnClassNames={NARROW_HIDDEN}
      testId="opportunities-table"
      mobile={(one) => ({
        title: one.statement,
        titleText: one.statement,
        href: opportunitiesHref(project, one.code),
        tags: <Identifier value={one.code} />,
        status: <OpportunityStatusBadge status={one.status} />,
        meta: one.window_until ? t("mobileMeta", { until: one.window_until, who: one.beneficiary }) : one.beneficiary,
        data: { opportunity: one.code },
      })}
    />
  );
}

/** The status filter, read from the address (`?status=`) when the page opens and written back to it, so a link keeps it. */
function useStatusFilter(): [OpportunityStatus | null, (status: OpportunityStatus | null) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const [status, setStatus] = useState(() => readStatusFilter(params));
  const set = (next: OpportunityStatus | null) => {
    setStatus(next);
    window.history.replaceState(null, "", `${pathname}${next ? `?${new URLSearchParams({ status: next })}` : ""}`);
  };
  return [status, set];
}

function Opportunities({ project, opportunities, writer, onNew }: { project: string; opportunities: ProjectOpportunity[]; writer: boolean; onNew: () => void }) {
  const t = useTranslations("opportunities.list");
  const words = useTranslations("opportunities.status");
  const [status, setStatus] = useStatusFilter();
  const counts = statusCounts(opportunities);
  if (opportunities.length === 0) {
    return (
      <EmptyState icon={Telescope} title={t("emptyTitle")} description={t("emptyText")} testId="opportunities-empty">
        {writer ? (
          <Button type="button" onClick={onNew} data-testid="opportunities-new-empty">
            <Plus aria-hidden="true" />
            {t("new")}
          </Button>
        ) : (
          <code className="rounded-xs bg-muted px-2 py-1 font-mono text-xs text-foreground">evo-agents hub opportunity set</code>
        )}
      </EmptyState>
    );
  }
  const shown = byClosingWindow(status ? opportunities.filter((one) => one.status === status) : opportunities);
  const options = [
    { value: null, label: t("all"), count: opportunities.length },
    ...OPPORTUNITY_STATUSES.map((value) => ({ value, label: words(value), icon: OPPORTUNITY_LOOKS[value].icon, count: counts[value] })),
  ];
  return (
    <DataCard
      testId="opportunities-card"
      toolbar={
        <DataToolbar label={t("search")} count={t("count", { count: shown.length })} countTestId="opportunities-count">
          <FacetGroup
            label={t("statusLabel")}
            options={options}
            selected={status}
            onSelect={(value) => setStatus(value as OpportunityStatus | null)}
            countLabel={(count) => t("count", { count })}
            testId="opportunity-statuses"
          />
        </DataToolbar>
      }
    >
      {shown.length === 0 && status ? (
        <NoResults title={t("noMatch")} filters={[{ label: t("statusLabel"), value: words(status) }]} onClear={() => setStatus(null)} />
      ) : (
        <OpportunitiesTable project={project} opportunities={shown} />
      )}
    </DataCard>
  );
}

/**
 * A project's opportunities (/p/{project}/opportunities), for any member with a grant on it: what the project could win,
 * the nearest closing window first and those with no date last, filtered by status. A writer adds one here (New
 * opportunity) or with `evo-agents hub opportunity set`; goals and claims point back to them.
 */
export function OpportunitiesPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("opportunities.list");
  const state = useHubQuery(opportunitiesQuery(browserApi, project), initialError);
  const viewer = useOpportunityViewer(project);
  const writer = viewer?.writer ?? false;
  const [creating, setCreating] = useState(false);
  const known = state.status === "success" ? state.data : [];
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="opportunities-page">
      <PageHeader
        title={t("title")}
        actions={
          writer && known.length > 0 ? (
            <Button type="button" onClick={() => setCreating(true)} data-testid="opportunities-new">
              <Plus aria-hidden="true" />
              {t("new")}
            </Button>
          ) : null
        }
      />
      <QueryView state={state} loading={<TableSkeleton rows={3} toolbar />}>
        {(opportunities) => <Opportunities project={project} opportunities={opportunities} writer={writer} onNew={() => setCreating(true)} />}
      </QueryView>
      {writer ? <NewOpportunityDialog project={project} opportunities={known} open={creating} onOpenChange={setCreating} /> : null}
    </div>
  );
}
