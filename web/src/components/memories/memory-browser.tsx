"use client";

import { useQuery } from "@tanstack/react-query";
import { Brain, Info, NotebookPen, ShieldCheck, TriangleAlert } from "lucide-react";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";

import { DataCard, DataToolbar } from "@/components/data/data-card";
import { FacetGroup, type FacetOption } from "@/components/data/facet-group";
import { ToolbarFilters } from "@/components/data/filter-sheet";
import { SearchField } from "@/components/data/search-field";
import { VisibilityLevel } from "@/components/data/visibility";
import { PageHeader } from "@/components/shell/page-header";
import { type HubQueryState, QueryView, useHubQuery } from "@/components/states/query-view";
import { type ActiveFilter, ApiErrorState, EmptyState, NoResults, NotFoundState, TableSkeleton } from "@/components/states/states";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { agentsQuery } from "@/lib/agent-queries";
import { browserApi } from "@/lib/api/browser";
import type { Project } from "@/lib/api/client";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { projectQuery } from "@/lib/queries";

import {
  applyFilters,
  countBy,
  filtersQuery,
  isFiltered,
  locationOptions,
  type MemoryFilters,
  NO_FILTERS,
  readFilters,
} from "./memory-filters";
import { MEMORY_TYPES, TYPE_ICONS } from "./memory-meta";
import { type MemoryRow, MemoryTable } from "./memory-table";
import { memoryListQuery, type MemoryScope, memorySearchQuery } from "./queries";

const HARNESS = "harness";

type Row = MemoryRow;

const sum = (counts: Map<string, number>) => [...counts.values()].reduce((total, count) => total + count, 0);

/**
 * The filters in the URL. Changing them replaces the URL through the History API, which Next.js syncs with
 * useSearchParams without asking the server for the page again: the queries run here, and a reload or a shared link
 * gets the same view rendered on the server.
 */
function useFilters(): [MemoryFilters, (next: MemoryFilters) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const filters = readFilters(params);
  const set = (next: MemoryFilters) => window.history.replaceState(null, "", `${pathname}${filtersQuery(next)}`);
  return [filters, set];
}

type BrowserProps = {
  scope: MemoryScope;
  /** What the server's prefetch of the list or the search failed with, if it did. */
  initialError: ApiErrorInfo | null;
  /** For a project: the project's own prefetch failure. */
  projectError?: ApiErrorInfo | null;
};

/** The memories of a project, or the visitor's personal ones: facets, full-text search, and the table. */
export function MemoryBrowser({ scope, initialError, projectError = null }: BrowserProps) {
  if (scope.kind === "project") {
    return <ProjectMemories project={scope.project} initialError={initialError} projectError={projectError} />;
  }
  return <Memories scope={scope} project={null} initialError={initialError} />;
}

function ProjectMemories({
  project: name,
  initialError,
  projectError,
}: {
  project: string;
  initialError: ApiErrorInfo | null;
  projectError: ApiErrorInfo | null;
}) {
  const tStates = useTranslations("states");
  const state = useHubQuery(projectQuery(browserApi, name), projectError);
  if (state.status === "error" && state.error.status === 404) {
    return (
      <NotFoundState
        title={tStates("projectNotFoundTitle", { name })}
        description={tStates("projectNotFoundDescription")}
      />
    );
  }
  if (state.status === "error") return <ApiErrorState error={state.error} onRetry={state.retry} />;
  return (
    <Memories
      scope={{ kind: "project", project: name }}
      project={state.status === "success" ? state.data : null}
      initialError={initialError}
    />
  );
}

function Memories({
  scope,
  project,
  initialError,
}: {
  scope: MemoryScope;
  project: Project | null;
  initialError: ApiErrorInfo | null;
}) {
  const t = useTranslations("memories");
  const [read, setFilters] = useFilters();
  const filters = scope.kind === "project" ? read : { ...read, agent: null }; // a personal memory has no agent
  const searching = filters.q.length > 0;
  // One of the two runs: the whole list, or the search (narrowed to a location on the server).
  const list = useHubQuery(
    { ...memoryListQuery(browserApi, scope, filters.agent), enabled: !searching },
    searching ? null : initialError,
  );
  const search = useHubQuery(
    { ...memorySearchQuery(browserApi, scope, filters.q, filters.location, filters.agent), enabled: searching },
    searching ? initialError : null,
  );
  const state: HubQueryState<{ items: Row[]; truncated: boolean }> = searching
    ? search.status === "success"
      ? { status: "success", data: { items: search.data, truncated: false } }
      : search
    : list;

  const isProject = scope.kind === "project";
  const noGrant = isProject && project?.role === null; // a hub admin without a grant: the API shows nothing
  const total = state.status === "success" ? state.data.items.length : null;

  return (
    <>
      <PageHeader
        title={isProject ? t("projectTitle") : t("personalTitle")}
        tags={
          total !== null && !searching && !noGrant ? (
            <Badge variant="secondary">{t("count", { count: total })}</Badge>
          ) : null
        }
      />
      {isProject && project ? <Visibility project={project} /> : null}
      {noGrant ? null : (
        <MemoryList
          scope={scope}
          project={project}
          filters={filters}
          setFilters={setFilters}
          state={state}
        />
      )}
    </>
  );
}

function MemoryList({
  scope,
  project,
  filters,
  setFilters,
  state,
}: {
  scope: MemoryScope;
  project: Project | null;
  filters: MemoryFilters;
  setFilters: (next: MemoryFilters) => void;
  state: HubQueryState<{ items: Row[]; truncated: boolean }>;
}) {
  const t = useTranslations("memories");
  const tStates = useTranslations("states.noResults");
  const isProject = scope.kind === "project";
  const declared = isProject && project ? [HARNESS, ...project.repos.map((repo) => repo.name)] : [];
  const searching = filters.q.length > 0;
  const items = state.status === "success" ? state.data.items : null;
  const agents = useAgentOptions(isProject ? scope.project : null, filters.agent, t("facets.shared"));

  // No memory at all and no filter (an agent's list is a filter: its empty table keeps the facet to go back): the page says what memories are, without a toolbar over nothing.
  if (items !== null && items.length === 0 && !isFiltered(filters)) {
    return (
      <>
        {!isProject ? <PersonalNote /> : null}
        {isProject ? (
          <EmptyState
            icon={Brain}
            title={t("empty.projectTitle")}
            description={t.rich("empty.projectDescription", { code: (chunks) => <code className="font-mono">{chunks}</code> })}
          />
        ) : (
          <EmptyState
            icon={NotebookPen}
            title={t("empty.personalTitle")}
            description={t.rich("empty.personalDescription", { code: (chunks) => <code className="font-mono">{chunks}</code> })}
          />
        )}
      </>
    );
  }

  const words: FacetWords = { all: t("facets.all"), harness: t("facets.harness"), type: (type) => t(`types.${type}`) };
  const facets = items !== null ? memoryFacets(items, filters, declared, scope, words) : null;
  const harness = (location: string) => location === HARNESS && isProject;
  const inUse: ActiveFilter[] = [
    ...(filters.q ? [{ label: tStates("search"), value: filters.q }] : []),
    ...(filters.location
      ? [{ label: t("facets.location"), value: harness(filters.location) ? t("facets.harness") : filters.location }]
      : []),
    ...(filters.type ? [{ label: t("facets.type"), value: t(`types.${filters.type}`) }] : []),
    ...(filters.agent ? [{ label: t("facets.agent"), value: `@${filters.agent}` }] : []),
  ];
  const countLabel = (count: number) => t("count", { count });

  const count =
    facets && items
      ? searching
        ? t("summary.search", { count: facets.shown.length, q: filters.q })
        : isFiltered(filters)
          ? t("summary.filtered", { shown: facets.shown.length, count: items.length })
          : t("summary.all", { count: items.length })
      : undefined;
  const toolbar = (
    <DataToolbar label={isProject ? t("caption") : t("personalTitle")} count={count} countTestId="memories-summary">
      <SearchField
        value={filters.q}
        onCommit={(q) => setFilters({ ...filters, q })}
        label={t("search.label")}
        placeholder={t("search.placeholder")}
        clearLabel={t("search.clear")}
        className="sm:w-64 max-md:flex-1"
        testId="memories-search"
      />
      {facets ? (
        <ToolbarFilters
          active={(filters.location ? 1 : 0) + (filters.type ? 1 : 0) + (filters.agent ? 1 : 0)}
          summary={count}
          onClear={() => setFilters({ ...filters, location: null, type: null, agent: null })}
          testId="memories-filters"
        >
          <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2 max-md:flex-col max-md:items-stretch max-md:gap-5" data-testid="memories-facets">
            {facets.locations.length > 2 || filters.location ? (
              <FacetGroup
                label={t("facets.location")}
                showLabel
                options={facets.locations}
                selected={filters.location}
                onSelect={(location) => setFilters({ ...filters, location })}
                countLabel={countLabel}
                testId="facet-location"
              />
            ) : null}
            {agents.length > 1 ? (
              <FacetGroup
                label={t("facets.agent")}
                showLabel
                options={agents}
                selected={filters.agent}
                onSelect={(agent) => setFilters({ ...filters, agent })}
                countLabel={countLabel}
                testId="facet-agent"
              />
            ) : null}
            <FacetGroup
              label={t("facets.type")}
              showLabel
              options={facets.types}
              selected={filters.type}
              onSelect={(type) => setFilters({ ...filters, type: type === null ? null : (type as MemoryFilters["type"]) })}
              countLabel={countLabel}
              testId="facet-type"
            />
          </div>
        </ToolbarFilters>
      ) : null}
      {isFiltered(filters) && facets && facets.shown.length > 0 ? (
        // On a phone the filter sheet has its own Clear filters, and the search field its clear button.
        <Button variant="ghost" size="sm" onClick={() => setFilters(NO_FILTERS)} className="max-md:hidden">
          {t("facets.clear")}
        </Button>
      ) : null}
    </DataToolbar>
  );

  return (
    <>
      {!isProject ? <PersonalNote /> : null}
      <DataCard toolbar={toolbar}>
        <QueryView state={state} loading={<TableSkeleton rows={6} />}>
          {(data) =>
            facets && facets.shown.length === 0 ? (
              <NoResults
                title={searching ? t("noResults.searchTitle", { q: filters.q }) : t("noResults.filterTitle")}
                filters={inUse}
                onClear={() => setFilters(NO_FILTERS)}
              />
            ) : (
              <>
                {data.truncated ? (
                  <p role="note" className="flex items-start gap-2 border-b bg-surface-sunken px-4 py-2.5 text-[13px] leading-[18px] text-muted-foreground">
                    <TriangleAlert className="mt-px size-4 shrink-0" aria-hidden="true" />
                    {t("truncated", { count: data.items.length })}
                  </p>
                ) : null}
                <MemoryTable
                  rows={facets?.shown ?? []}
                  scope={scope}
                  ranked={searching}
                  unrestricted={project?.locations[0]}
                  agent={filters.agent}
                />
              </>
            )
          }
        </QueryView>
      </DataCard>
    </>
  );
}

/** Where personal memories come from, above their list. */
function PersonalNote() {
  const t = useTranslations("memories");
  return (
    <p className="flex items-start gap-2 text-sm text-muted-foreground">
      <NotebookPen className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      {t("personalVisibility")}
    </p>
  );
}

/**
 * The agents whose memories the list can show instead of the project's shared ones (`null`, labelled `shared`): every
 * agent of the project but its default, which keeps none, and the one in force even when it is not among them.
 */
function useAgentOptions(project: string | null, selected: string | null, shared: string): FacetOption[] {
  const state = useQuery({ ...agentsQuery(browserApi, project ?? ""), enabled: project !== null });
  if (project === null) return [];
  const handles = (state.data ?? []).filter((agent) => !agent.is_default).map((agent) => agent.handle);
  if (selected && !handles.includes(selected)) handles.push(selected);
  return [{ value: null, label: shared }, ...handles.map((handle) => ({ value: handle, label: `@${handle}`, mono: true }))];
}

/** The words of the facets, from the page's messages. */
type FacetWords = { all: string; harness: string; type: (type: (typeof MEMORY_TYPES)[number]) => string };

/**
 * The facets of the rows a list or a search returned, and the rows shown: the locations (the project's declared ones,
 * and the one in force even when no memory sits there) and the types, each counted over the rows the other facet
 * leaves. In a search the location is applied by the API, so only the type narrows here, and locations go uncounted.
 */
function memoryFacets(items: Row[], filters: MemoryFilters, declared: string[], scope: MemoryScope, words: FacetWords) {
  const searching = filters.q.length > 0;
  const shown = applyFilters(items, searching ? { ...filters, location: null } : filters);
  const byLocation = countBy(applyFilters(items, { ...filters, location: null }), (item) => item.location);
  const byType = countBy(applyFilters(items, { ...filters, type: null }), (item) => item.type);
  const offered = filters.location && !declared.includes(filters.location) ? [...declared, filters.location] : declared;
  const harness = (location: string) => location === HARNESS && scope.kind === "project";
  const locations: FacetOption[] = [
    { value: null, label: words.all, count: searching ? undefined : sum(byLocation) },
    ...locationOptions(offered, items).map((location) => ({
      value: location,
      label: harness(location) ? words.harness : location,
      mono: !harness(location),
      count: searching ? undefined : (byLocation.get(location) ?? 0),
    })),
  ];
  const types: FacetOption[] = [
    { value: null, label: words.all, count: sum(byType) },
    ...MEMORY_TYPES.map((type) => ({ value: type, label: words.type(type), icon: TYPE_ICONS[type], count: byType.get(type) ?? 0 })),
  ];
  return { shown, locations, types };
}

function Visibility({ project }: { project: Project }) {
  const t = useTranslations("memories");
  if (project.role === null) {
    return (
      <Alert role="note">
        <Info aria-hidden="true" />
        <AlertDescription>{t("adminWithoutGrant")}</AlertDescription>
      </Alert>
    );
  }
  return (
    <p className="flex items-start gap-2 text-sm text-muted-foreground" data-testid="memories-visibility">
      <ShieldCheck className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span>
        {t.rich("projectVisibility", {
          level: () => <VisibilityLevel level={project.max_level} className="font-medium text-foreground" />,
        })}
      </span>
    </p>
  );
}
