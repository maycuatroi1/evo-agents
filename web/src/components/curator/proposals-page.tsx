"use client";

import { Lightbulb, X } from "lucide-react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useId, useMemo } from "react";

import { Pager } from "@/components/admin/pager";
import { usePagedQuery } from "@/components/admin/use-paged-query";
import { When } from "@/components/admin/when";
import { DataCard, DataToolbar } from "@/components/data/data-card";
import type { DataListRow } from "@/components/data/data-list";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { FacetGroup, type FacetOption } from "@/components/data/facet-group";
import { ToolbarField, ToolbarFilters } from "@/components/data/filter-sheet";
import { NAME_LINK } from "@/components/data/identifier";
import { QueryView, type HubQueryState } from "@/components/states/query-view";
import { type ActiveFilter, EmptyState, NoResults, TableSkeleton } from "@/components/states/states";
import { STATUS_LOOKS, StatusBadge, useStatusText } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

import { TierTag, useKindText, useLensText } from "./badges";
import { CuratorHeader, CuratorTabs } from "./curator-header";
import { CuratorHeadSkeleton, useCuratorStatus } from "./curator-page";
import {
  isFiltered,
  isLens,
  LENSES,
  NO_PROPOSAL_FILTERS,
  PROPOSAL_STATES,
  type ProposalFilters,
  proposalListQuery,
  proposalSearch,
  readProposalFilters,
  type Tier,
  TIERS,
} from "./model";
import { type ProposalList, proposalHref, proposalsQuery, type ProposalSummary } from "./queries";

/** The filters in the URL, changed through the History API (Next.js syncs useSearchParams with it). */
function useFilters(): [ProposalFilters, (next: ProposalFilters) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const filters = readProposalFilters(params);
  const set = (next: ProposalFilters) => window.history.replaceState(null, "", `${pathname}${proposalSearch(next)}`);
  return [filters, set];
}

function useMobileProposal() {
  const t = useTranslations("curator.proposals");
  const kind = useKindText();
  const lens = useLensText();
  return (proposal: ProposalSummary): DataListRow => {
    const detail = t("mobileMeta", { id: proposal.id, tier: proposal.tier, kind: kind(proposal.kind), lens: lens(proposal.lens) });
    return {
      title: proposal.title,
      titleText: proposal.title,
      href: proposalHref(proposal.project, proposal.id),
      status: <StatusBadge kind="proposal" status={proposal.state} />,
      meta: detail,
      metaText: detail,
      data: { "proposal-id": proposal.id, state: proposal.state, tier: proposal.tier },
    };
  };
}

/** The proposals as a table, newest first as the API sends them; under 768 px a list of rows. */
export function ProposalsTable({ proposals, caption, testId = "proposals-table" }: { proposals: ProposalSummary[]; caption: string; testId?: string }) {
  const t = useTranslations("curator.proposals");
  const kind = useKindText();
  const lens = useLensText();
  const mobile = useMobileProposal();
  const columns = useMemo(() => {
    const helper = dataTableColumns<ProposalSummary>();
    return helper.columns([
      helper.display({
        id: "id",
        header: () => t("columns.id"),
        cell: (info) => (
          <span className="font-mono text-[13px] text-muted-foreground tabular-nums" data-proposal-id={info.row.original.id}>
            #{info.row.original.id}
          </span>
        ),
      }),
      helper.display({
        id: "proposal",
        header: () => t("columns.proposal"),
        meta: { primary: true },
        cell: (info) => {
          const proposal = info.row.original;
          const where = t("sub", { kind: kind(proposal.kind), lens: lens(proposal.lens) });
          return (
            <CellMain sub={where} subTitle={where}>
              <Link
                href={proposalHref(proposal.project, proposal.id)}
                className={cn(NAME_LINK, "truncate")}
                title={proposal.title}
                data-testid="proposal-link"
              >
                {proposal.title}
              </Link>
            </CellMain>
          );
        },
      }),
      helper.display({
        id: "tier",
        header: () => t("columns.tier"),
        cell: (info) => <TierTag tier={info.row.original.tier} />,
      }),
      helper.display({
        id: "evidence",
        header: () => t("columns.evidence"),
        meta: { numeric: true },
        cell: (info) => (
          <span className="tabular-nums" title={t("evidenceCount", { count: info.row.original.evidence_count })}>
            {info.row.original.evidence_count}
          </span>
        ),
      }),
      helper.display({
        id: "state",
        header: () => t("columns.state"),
        cell: (info) => <StatusBadge kind="proposal" status={info.row.original.state} />,
      }),
      helper.display({
        id: "created",
        header: () => t("columns.created"),
        meta: { numeric: true },
        cell: (info) => <When value={info.row.original.created_at} short />,
      }),
    ]);
  }, [t, kind, lens]);
  return (
    <DataTable
      data={proposals}
      columns={columns}
      caption={caption}
      getRowId={(row) => String(row.id)}
      columnClassNames={{ id: "hidden sm:table-cell", evidence: "hidden lg:table-cell", created: "hidden md:table-cell" }}
      testId={testId}
      mobile={mobile}
    />
  );
}

function ProposalList({ project, state, stale, filters, setFilters }: {
  project: string;
  state: HubQueryState<ProposalList>;
  stale: boolean;
  filters: ProposalFilters;
  setFilters: (next: ProposalFilters) => void;
}) {
  const t = useTranslations("curator.proposals");
  const stateText = useStatusText("proposal");
  const lensText = useLensText();
  const ids = useId();
  const caption = t("caption", { project });
  const counts = state.status === "success" ? state.data.counts : null;
  const inUse: ActiveFilter[] = [
    ...(filters.state ? [{ label: t("facets.state"), value: stateText(filters.state) }] : []),
    ...(filters.tier !== null ? [{ label: t("facets.tier"), value: t("tierValue", { tier: filters.tier }) }] : []),
    ...(filters.lens ? [{ label: t("facets.lens"), value: lensText(filters.lens) }] : []),
    ...(filters.run !== null ? [{ label: t("facets.run"), value: `#${filters.run}` }] : []),
  ];
  const active = inUse.length;
  const summary =
    state.status === "success"
      ? isFiltered(filters)
        ? t("listSummary.filtered", { count: state.data.total })
        : t("listSummary.all", { count: state.data.total })
      : undefined;
  const sumOf = (record: Record<string, number> | undefined) => (record ? Object.values(record).reduce((sum, n) => sum + n, 0) : undefined);

  const toolbar: ReactNode = (
    <DataToolbar label={caption} count={summary} countTestId="proposals-summary">
      <ToolbarFilters
        active={active}
        summary={summary}
        onClear={() => setFilters(NO_PROPOSAL_FILTERS)}
        testId="proposals-filters"
      >
        <FacetGroup
          label={t("facets.state")}
          options={[
            { value: null, label: t("facets.all"), count: sumOf(counts?.state) },
            ...PROPOSAL_STATES.map(
              (value): FacetOption => ({
                value,
                label: stateText(value),
                icon: STATUS_LOOKS.proposal[value].icon,
                count: counts?.state[value],
              }),
            ),
          ]}
          selected={filters.state}
          onSelect={(value) => setFilters({ ...filters, state: (value as ProposalFilters["state"]) ?? null, page: 1 })}
          countLabel={(n) => t("count", { count: n })}
          testId="proposals-facet-state"
        />
        <FacetGroup
          label={t("facets.tier")}
          options={[
            { value: null, label: t("facets.allTiers"), count: sumOf(counts?.tier) },
            ...TIERS.map((tier): FacetOption => ({ value: String(tier), label: t("tierValue", { tier }), count: counts?.tier[String(tier)] })),
          ]}
          selected={filters.tier === null ? null : String(filters.tier)}
          onSelect={(value) => setFilters({ ...filters, tier: value === null ? null : (Number(value) as Tier), page: 1 })}
          countLabel={(n) => t("count", { count: n })}
          testId="proposals-facet-tier"
        />
        <ToolbarField label={t("facets.lens")}>
          {(id) => (
            <NativeSelect
              id={id}
              value={filters.lens ?? ""}
              onChange={(event) => setFilters({ ...filters, lens: isLens(event.target.value) ? event.target.value : null, page: 1 })}
              className="w-full md:w-48"
              aria-controls={`${ids}-results`}
              data-testid="proposals-lens"
            >
              <NativeSelectOption value="">{t("facets.allLenses")}</NativeSelectOption>
              {LENSES.map((lens) => (
                <NativeSelectOption key={lens} value={lens}>
                  {counts ? t("lensOption", { lens: lensText(lens), count: counts.lens[lens] ?? 0 }) : lensText(lens)}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          )}
        </ToolbarField>
        {filters.run !== null ? (
          <Button
            type="button"
            variant="secondary"
            size="sm"
            className="border-brand bg-surface-selected font-semibold text-foreground"
            onClick={() => setFilters({ ...filters, run: null, page: 1 })}
            aria-label={t("clearRun", { id: filters.run })}
            data-testid="proposals-run-filter"
          >
            {t("runFilter", { id: filters.run })}
            <X aria-hidden="true" />
          </Button>
        ) : null}
      </ToolbarFilters>
    </DataToolbar>
  );

  return (
    <DataCard
      toolbar={toolbar}
      busy={stale}
      footer={
        state.status === "success" && state.data.total > state.data.limit ? (
          <Pager
            label={t("pagerLabel")}
            page={filters.page}
            count={state.data.proposals.length}
            hasNext={state.data.offset + state.data.limit < state.data.total}
            canGoBack={filters.page > 1}
            atStart={filters.page === 1}
            busy={stale}
            onNext={() => setFilters({ ...filters, page: filters.page + 1 })}
            onPrevious={() => setFilters({ ...filters, page: Math.max(1, filters.page - 1) })}
            onFirst={() => setFilters({ ...filters, page: 1 })}
          />
        ) : null
      }
    >
      <div id={`${ids}-results`}>
        <QueryView state={state} loading={<TableSkeleton rows={6} />}>
          {(list) =>
            list.proposals.length === 0 ? (
              isFiltered(filters) || filters.page > 1 ? (
                <NoResults title={t("noResults.title")} filters={inUse} onClear={() => setFilters(NO_PROPOSAL_FILTERS)} />
              ) : (
                <EmptyState icon={Lightbulb} title={t("empty.title")} description={t("empty.description")} />
              )
            ) : (
              <ProposalsTable proposals={list.proposals} caption={caption} />
            )
          }
        </QueryView>
      </div>
    </DataCard>
  );
}

/**
 * The proposals of a project's review runs, newest first, filtered by state, tier and lens (and by review run, from a
 * night's row), each filter's values with how many proposals they hold. Read every minute.
 */
export function ProposalsPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const status = useCuratorStatus(project);
  const [filters, setFilters] = useFilters();
  const { state, stale } = usePagedQuery(proposalsQuery(browserApi, project, proposalListQuery(filters)), initialError, { live: true });
  const t = useTranslations("curator.proposals");
  return (
    <div className="flex flex-col gap-6" data-testid="proposals-page">
      <QueryView state={status} loading={<CuratorHeadSkeleton />}>
        {(found) => (
          <>
            <CuratorHeader project={project} status={found} />
            <CuratorTabs project={project} waiting={found.open_proposals} />
          </>
        )}
      </QueryView>
      <section aria-labelledby="proposals-title" className="flex flex-col gap-3">
        <h2 id="proposals-title" className="sr-only">
          {t("caption", { project })}
        </h2>
        <ProposalList project={project} state={state} stale={stale} filters={filters} setFilters={setFilters} />
      </section>
    </div>
  );
}
