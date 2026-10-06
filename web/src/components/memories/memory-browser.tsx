"use client";

import { Brain, Info, NotebookPen, ShieldCheck, TriangleAlert } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo } from "react";

import { DataCard, DataToolbar } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { FacetGroup, type FacetOption } from "@/components/data/facet-group";
import { NAME_LINK } from "@/components/data/identifier";
import { SearchField } from "@/components/data/search-field";
import { VisibilityLevel } from "@/components/data/visibility";
import { projectHref } from "@/components/shell/nav";
import { PageHeader } from "@/components/shell/page-header";
import { type HubQueryState, QueryView, useHubQuery } from "@/components/states/query-view";
import { type ActiveFilter, ApiErrorState, EmptyState, NoResults, NotFoundState, TableSkeleton } from "@/components/states/states";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { Project } from "@/lib/api/client";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { projectQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

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
        meta: { primary: true },
        cell: (info) => {
          const row = info.row.original;
          const { title, description } = memoryTitle(row.name, row.body);
          // One line under the title: what the memory is about, or its file name when the title is not the name.
          const sub = description ?? (title !== row.name ? <span className="font-mono">{row.name}</span> : null);
          return (
            <CellMain sub={sub} subTitle={description ?? row.name}>
              <Link
                href={memoryHref(scope, row.id)}
                className={cn(NAME_LINK, "truncate")}
                title={title !== row.name ? `${title} (${row.name})` : title}
                data-memory-name={row.name}
              >
                {title}
              </Link>
            </CellMain>
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
        cell: (info) => <span className="font-mono text-xs text-foreground">{info.getValue()}</span>,
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
        meta: { numeric: true },
        cell: (info) => (
          <div className="flex flex-col items-end gap-px">
            <time dateTime={info.row.original.updated_at} className="whitespace-nowrap">
              {format.dateTime(info.getValue(), { dateStyle: "medium", timeStyle: "short" })}
            </time>
            <span className="text-xs leading-4 whitespace-nowrap text-fg-subtle">
              {t("by", { login: info.row.original.updated_by })}
            </span>
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

  // No memory at all and no filter: the page says what memories are, without a toolbar over nothing.
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
  ];
  const countLabel = (count: number) => t("count", { count });

  const toolbar = (
    <DataToolbar
      label={isProject ? t("caption") : t("personalTitle")}
      count={
        facets && items
          ? searching
            ? t("summary.search", { count: facets.shown.length, q: filters.q })
            : isFiltered(filters)
              ? t("summary.filtered", { shown: facets.shown.length, count: items.length })
              : t("summary.all", { count: items.length })
          : undefined
      }
      countTestId="memories-summary"
    >
      <SearchField
        value={filters.q}
        onCommit={(q) => setFilters({ ...filters, q })}
        label={t("search.label")}
        placeholder={t("search.placeholder")}
        clearLabel={t("search.clear")}
        className="sm:w-64"
        testId="memories-search"
      />
      {facets ? (
        <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2" data-testid="memories-facets">
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
      ) : null}
      {isFiltered(filters) && facets && facets.shown.length > 0 ? (
        <Button variant="ghost" size="sm" onClick={() => setFilters(NO_FILTERS)}>
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
