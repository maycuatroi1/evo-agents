"use client";

import { Brain, Info, NotebookPen, SearchX, ShieldCheck, TriangleAlert } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { FacetGroup, type FacetOption } from "@/components/data/facet-group";
import { SearchField } from "@/components/data/search-field";
import { projectHref } from "@/components/shell/nav";
import { PageHeader } from "@/components/shell/page-header";
import { type HubQueryState, QueryView, useHubQuery } from "@/components/states/query-view";
import { ApiErrorState, EmptyState, NotFoundState, TableSkeleton } from "@/components/states/states";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { Project } from "@/lib/api/client";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { projectQuery } from "@/lib/queries";

import { LabelBadge, MemoryTypeBadge } from "./badges";
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
import { MEMORY_TYPES, memoryTitle, TYPE_ICONS } from "./memory-meta";
import { type Memory, memoryListQuery, type MemoryScope, memorySearchQuery } from "./queries";

const HARNESS = "harness";
const NARROW_HIDDEN = { location: "hidden md:table-cell", updated: "hidden lg:table-cell", label: "hidden sm:table-cell" };

export function memoryHref(scope: MemoryScope, id: number): Route {
  return scope.kind === "project" ? projectHref(scope.project, `memories/${id}`) : (`/memories/${id}` as Route);
}

export function memoriesHref(scope: MemoryScope): Route {
  return scope.kind === "project" ? projectHref(scope.project, "memories") : ("/memories" as Route);
}

type Row = Memory & { rank?: number };

const sum = (counts: Map<string, number>) => [...counts.values()].reduce((total, count) => total + count, 0);

function MemoryTable({
  rows,
  scope,
  ranked,
  unrestricted,
}: {
  rows: Row[];
  scope: MemoryScope;
  ranked: boolean;
  unrestricted?: string;
}) {
  const t = useTranslations("memories");
  const format = useFormatter();
  const columns = useMemo(() => {
    const helper = dataTableColumns<Row>();
    return helper.columns([
      helper.accessor((row) => memoryTitle(row.name, row.body).title, {
        id: "name",
        header: () => t("columns.name"),
        sortFn: "alphanumeric",
        cell: (info) => {
          const row = info.row.original;
          const { title, description } = memoryTitle(row.name, row.body);
          return (
            <div className="flex min-w-0 flex-col gap-0.5 py-0.5">
              <Link
                href={memoryHref(scope, row.id)}
                className="w-fit font-medium text-primary underline-offset-4 [overflow-wrap:anywhere] hover:underline"
                data-memory-name={row.name}
              >
                {title}
              </Link>
              {title !== row.name ? (
                <span className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{row.name}</span>
              ) : null}
              {description ? (
                <span className="line-clamp-2 max-w-prose text-xs text-pretty text-muted-foreground">{description}</span>
              ) : null}
              {/* The columns a phone hides, under the name instead. */}
              <span className="flex flex-wrap items-center gap-1.5 pt-1 md:hidden">
                {scope.kind === "project" ? (
                  <LabelBadge label={row.label} unrestricted={unrestricted} className="sm:hidden" />
                ) : null}
                <span className="font-mono text-xs text-muted-foreground">{row.location}</span>
              </span>
            </div>
          );
        },
      }),
      helper.accessor("type", {
        header: () => t("columns.type"),
        sortFn: "text",
        cell: (info) => <MemoryTypeBadge type={info.getValue()} />,
      }),
      helper.accessor("location", {
        header: () => t("columns.location"),
        sortFn: "text",
        cell: (info) => <span className="font-mono text-xs [overflow-wrap:anywhere]">{info.getValue()}</span>,
      }),
      helper.accessor((row) => String(row.label.level ?? ""), {
        id: "label",
        header: () => t("columns.label"),
        enableSorting: false,
        cell: (info) => <LabelBadge label={info.row.original.label} unrestricted={unrestricted} />,
      }),
      helper.accessor((row) => new Date(row.updated_at), {
        id: "updated",
        header: () => t("columns.updated"),
        sortFn: "datetime",
        cell: (info) => (
          <div className="flex flex-col text-xs">
            <time dateTime={info.row.original.updated_at} className="tabular-nums">
              {format.dateTime(info.getValue(), { dateStyle: "medium", timeStyle: "short" })}
            </time>
            <span className="text-muted-foreground">{t("by", { login: info.row.original.updated_by })}</span>
          </div>
        ),
      }),
    ]).filter((column) => scope.kind === "project" || column.id !== "label"); // a personal memory has no label
  }, [t, format, scope, unrestricted]);

  return (
    <DataTable
      key={ranked ? "ranked" : "browse"}
      data={rows}
      columns={columns}
      caption={ranked ? t("captionSearch") : t("caption")}
      getRowId={(row) => String(row.id)}
      initialSorting={ranked ? [] : [{ id: "updated", desc: true }]}
      columnClassNames={NARROW_HIDDEN}
      testId="memories-table"
    />
  );
}

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
  const [filters, setFilters] = useFilters();
  const searching = filters.q.length > 0;
  // One of the two runs: the whole list, or the search (narrowed to a location on the server).
  const list = useHubQuery(
    { ...memoryListQuery(browserApi, scope), enabled: !searching },
    searching ? null : initialError,
  );
  const search = useHubQuery(
    { ...memorySearchQuery(browserApi, scope, filters.q, filters.location), enabled: searching },
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
        eyebrow={isProject ? t("projectEyebrow", { project: scope.project }) : t("personalEyebrow")}
        title={isProject ? t("projectTitle") : t("personalTitle")}
        description={isProject ? t("projectDescription") : t("personalDescription")}
        meta={
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
  const isProject = scope.kind === "project";
  const declared = isProject && project ? [HARNESS, ...project.repos.map((repo) => repo.name)] : [];
  return (
    <>
      {!isProject ? (
        <p className="flex items-start gap-2 text-sm text-muted-foreground">
          <NotebookPen className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          {t("personalVisibility")}
        </p>
      ) : null}
      <SearchField
        value={filters.q}
        onCommit={(q) => setFilters({ ...filters, q })}
        label={t("search.label")}
        placeholder={t("search.placeholder")}
        clearLabel={t("search.clear")}
        testId="memories-search"
      />
      <QueryView state={state} loading={<TableSkeleton rows={6} />}>
        {(data) => (
          <Results
            scope={scope}
            items={data.items}
            truncated={data.truncated}
            filters={filters}
            setFilters={setFilters}
            declared={declared}
            unrestricted={project?.locations[0]}
          />
        )}
      </QueryView>
    </>
  );
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
          level: project.max_level ?? "-",
          code: (chunks) => <code className="rounded bg-muted px-1 font-mono text-xs text-foreground">{chunks}</code>,
        })}
      </span>
    </p>
  );
}

function Results({
  scope,
  items,
  truncated,
  filters,
  setFilters,
  declared,
  unrestricted,
}: {
  scope: MemoryScope;
  items: Row[];
  truncated: boolean;
  filters: MemoryFilters;
  setFilters: (next: MemoryFilters) => void;
  declared: string[];
  unrestricted?: string;
}) {
  const t = useTranslations("memories");
  const searching = filters.q.length > 0;
  // In a search the location is applied by the API, so only the type narrows here; in the list, both do.
  const shown = applyFilters(items, searching ? { ...filters, location: null } : filters);
  const byLocation = countBy(applyFilters(items, { ...filters, location: null }), (item) => item.location);
  const byType = countBy(applyFilters(items, { ...filters, type: null }), (item) => item.type);
  const countLabel = (count: number) => t("count", { count });

  // The location in force stays offered even when no memory sits there (a search narrowed to it found none).
  const offered = filters.location && !declared.includes(filters.location) ? [...declared, filters.location] : declared;
  const harness = (location: string) => location === HARNESS && scope.kind === "project";
  const locations: FacetOption[] = [
    { value: null, label: t("facets.all"), count: searching ? undefined : sum(byLocation) },
    ...locationOptions(offered, items).map((location) => ({
      value: location,
      label: harness(location) ? t("facets.harness") : location,
      mono: !harness(location),
      count: searching ? undefined : (byLocation.get(location) ?? 0),
    })),
  ];
  const types: FacetOption[] = [
    { value: null, label: t("facets.all"), count: sum(byType) },
    ...MEMORY_TYPES.map((type) => ({
      value: type,
      label: t(`types.${type}`),
      icon: TYPE_ICONS[type],
      count: byType.get(type) ?? 0,
    })),
  ];

  if (items.length === 0 && !isFiltered(filters)) {
    return scope.kind === "project" ? (
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
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-2.5 rounded-xl border bg-card p-3" data-testid="memories-facets">
        {locations.length > 2 || filters.location ? (
          <FacetGroup
            label={t("facets.location")}
            options={locations}
            selected={filters.location}
            onSelect={(location) => setFilters({ ...filters, location })}
            countLabel={countLabel}
            testId="facet-location"
          />
        ) : null}
        <FacetGroup
          label={t("facets.type")}
          options={types}
          selected={filters.type}
          onSelect={(type) => setFilters({ ...filters, type: type === null ? null : (type as MemoryFilters["type"]) })}
          countLabel={countLabel}
          testId="facet-type"
        />
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p aria-live="polite" className="text-sm text-muted-foreground" data-testid="memories-summary">
          {searching
            ? t("summary.search", { count: shown.length, q: filters.q })
            : isFiltered(filters)
              ? t("summary.filtered", { shown: shown.length, count: items.length })
              : t("summary.all", { count: items.length })}
        </p>
        {isFiltered(filters) ? (
          <Button variant="ghost" size="sm" onClick={() => setFilters(NO_FILTERS)}>
            {t("facets.clear")}
          </Button>
        ) : null}
      </div>
      {truncated ? (
        <Alert role="note">
          <TriangleAlert aria-hidden="true" />
          <AlertDescription>{t("truncated", { count: items.length })}</AlertDescription>
        </Alert>
      ) : null}
      {shown.length === 0 ? (
        <EmptyState
          icon={SearchX}
          title={searching ? t("noResults.searchTitle", { q: filters.q }) : t("noResults.filterTitle")}
          description={searching ? t("noResults.searchDescription") : t("noResults.filterDescription")}
        >
          <Button
            variant="outline"
            size="lg"
            onClick={() => setFilters(searching ? NO_FILTERS : { ...filters, location: null, type: null })}
          >
            {searching ? t("search.clear") : t("facets.clear")}
          </Button>
        </EmptyState>
      ) : (
        <MemoryTable rows={shown} scope={scope} ranked={searching} unrestricted={unrestricted} />
      )}
    </div>
  );
}
