"use client";

import { Network, Search, SearchX } from "lucide-react";
import Link from "next/link";
import Form from "next/form";
import { useTranslations } from "next-intl";
import { useMemo, useRef } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { useSearchShortcut } from "@/components/data/search-shortcut";
import { projectHref } from "@/components/shell/nav";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { useCharacterKeys } from "@/lib/keyboard";
import { displayName } from "@/lib/kg/graph";
import { kgSearchQuery, MAX_QUERY } from "@/lib/kg/queries";
import { kgHref, nodeHref } from "@/lib/kg/routes";
import type { KgNode, KindCount, NodeSearch } from "@/lib/kg/types";
import { cn } from "@/lib/utils";

import { KindBadge, KindShape, LabelBadge } from "./badges";

/** The kind select, drawn as the kit's input: 32 px, and 44 px with 16 px text under 768 px. */
const FIELD =
  "h-8 w-full min-w-0 rounded-sm border border-input bg-card px-2.5 text-sm transition-colors outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 max-md:h-11 max-md:text-base";

/**
 * A GET form: the query lives in the URL (`?q=&kind=`), so a search can be shared, reloaded and walked back, and it
 * works before the page's scripts run. The server reads the same parameters and prefetches the results.
 */
export function SearchForm({
  project,
  query,
  kind,
  kinds,
}: {
  project: string;
  query: string;
  kind: string;
  kinds: KindCount[];
}) {
  const t = useTranslations("kg.search");
  const input = useRef<HTMLInputElement>(null);
  useSearchShortcut(input);
  const slash = useCharacterKeys();
  const options = kind && !kinds.some((k) => k.kind === kind) ? [{ kind, count: 0 }, ...kinds] : kinds;
  return (
    <Form
      action={kgHref(project)}
      role="search"
      aria-label={t("title")}
      className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(10rem,14rem)_auto] sm:items-end"
      data-testid="kg-search-form"
    >
      <div className="flex min-w-0 flex-col gap-1.5">
        <label htmlFor="kg-q" className="text-sm font-medium">
          {t("query")}
        </label>
        <Input
          ref={input}
          id="kg-q"
          name="q"
          type="search"
          className="max-md:h-11"
          aria-keyshortcuts={slash ? "/" : undefined}
          defaultValue={query}
          required
          maxLength={MAX_QUERY}
          placeholder={t("placeholder")}
          autoComplete="off"
          spellCheck={false}
          aria-describedby="kg-q-hint"
        />
      </div>
      <div className="flex min-w-0 flex-col gap-1.5">
        <label htmlFor="kg-kind" className="text-sm font-medium">
          {t("kind")}
        </label>
        <select id="kg-kind" name="kind" defaultValue={kind} className={`${FIELD} cursor-pointer`}>
          <option value="">{t("allKinds")}</option>
          {options.map((option) => (
            <option key={option.kind} value={option.kind}>
              {t("kindOption", { kind: option.kind, count: option.count })}
            </option>
          ))}
        </select>
      </div>
      <Button type="submit" className="cursor-pointer">
        <Search aria-hidden="true" />
        {t("submit")}
      </Button>
      <p id="kg-q-hint" className="text-xs text-muted-foreground sm:col-span-3">
        {t("hint")}
      </p>
    </Form>
  );
}

/** What the member can see of the graph, by kind, before searching. */
export function KindSummary({ kinds, project }: { kinds: KindCount[]; project: string }) {
  const t = useTranslations("kg.search");
  const total = kinds.reduce((sum, k) => sum + k.count, 0);
  return (
    <div className="flex flex-col gap-2" data-testid="kg-kinds">
      <p className="text-sm text-muted-foreground">{t("visible", { count: total })}</p>
      <ul className="flex flex-wrap gap-2" aria-label={t("byKind")}>
        {kinds.map((k) => (
          <li key={k.kind}>
            <span className="inline-flex items-center gap-1.5 rounded-md border bg-card px-2.5 py-1 text-xs">
              <KindShape kind={k.kind} />
              <span className="font-medium">{k.kind}</span>
              <span className="text-muted-foreground tabular-nums">{k.count}</span>
            </span>
          </li>
        ))}
      </ul>
      {kinds.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {t("nothingVisible")}{" "}
          <Link href={projectHref(project)} className="text-brand underline-offset-4 hover:underline">
            {t("seeLevels")}
          </Link>
        </p>
      ) : null}
    </div>
  );
}

const NARROW = { label: "hidden md:table-cell", source: "hidden lg:table-cell", status: "hidden sm:table-cell" };

function ResultsTable({ project, results, caption }: { project: string; results: KgNode[]; caption: string }) {
  const t = useTranslations("kg.search");
  const columns = useMemo(() => {
    const helper = dataTableColumns<KgNode>();
    return helper.columns([
      helper.accessor((row) => displayName(row), {
        id: "name",
        header: () => t("columns.node"),
        sortFn: "text",
        cell: (info) => (
          <div className="flex min-w-0 flex-col gap-0.5">
            <Link
              href={nodeHref(project, info.row.original.id)}
              className={cn(NAME_LINK, "break-words whitespace-normal")}
              data-testid="kg-result-link"
              data-node-id={info.row.original.id}
            >
              {info.getValue()}
            </Link>
            <span className="font-mono text-xs break-all whitespace-normal text-muted-foreground">
              {info.row.original.id}
            </span>
          </div>
        ),
      }),
      helper.accessor("kind", {
        header: () => t("columns.kind"),
        sortFn: "text",
        cell: (info) => <KindBadge kind={info.getValue()} />,
      }),
      helper.accessor("status", {
        id: "status",
        header: () => t("columns.status"),
        sortFn: "text",
        cell: (info) => <span className="font-mono text-xs">{info.getValue()}</span>,
      }),
      helper.accessor((row) => row.label.level, {
        id: "label",
        header: () => t("columns.label"),
        sortFn: "text",
        cell: (info) => <LabelBadge label={info.row.original.label} />,
      }),
      helper.accessor((row) => (typeof row.props.source === "string" ? row.props.source : ""), {
        id: "source",
        header: () => t("columns.source"),
        sortFn: "text",
        cell: (info) => <span className="font-mono text-xs">{info.getValue() || "-"}</span>,
      }),
    ]);
  }, [t, project]);
  return (
    <DataTable
      data={results}
      columns={columns}
      caption={caption}
      getRowId={(row) => row.id}
      columnClassNames={NARROW}
      testId="kg-results"
    />
  );
}

function Results({ project, query, kind, data }: { project: string; query: string; kind: string; data: NodeSearch }) {
  const t = useTranslations("kg.search");
  if (data.graph === null) return <NoGraph />;
  const summary = kind
    ? t("countKind", { count: data.results.length, query, kind })
    : t("count", { count: data.results.length, query });
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted-foreground" role="status" data-testid="kg-results-count">
        {summary}
      </p>
      {data.results.length === 0 ? (
        <EmptyState icon={SearchX} title={t("emptyTitle", { query })} description={t("emptyDescription")}>
          {kind ? (
            <Button asChild variant="outline">
              <Link href={kgHref(project, { q: query })}>{t("clearKind")}</Link>
            </Button>
          ) : null}
        </EmptyState>
      ) : (
        <ResultsTable project={project} results={data.results} caption={summary} />
      )}
    </div>
  );
}

export function SearchResults({
  project,
  query,
  kind,
  initialError,
}: {
  project: string;
  query: string;
  kind: string;
  initialError: ApiErrorInfo | null;
}) {
  const state = useHubQuery(kgSearchQuery(browserApi, project, query, kind), initialError);
  return (
    <QueryView state={state} loading={<TableSkeleton rows={4} />}>
      {(data) => <Results project={project} query={query} kind={kind} data={data} />}
    </QueryView>
  );
}

export function NoGraph() {
  const t = useTranslations("kg.builds");
  return (
    <EmptyState
      icon={Network}
      title={t("noGraphTitle")}
      description={t.rich("noGraphDescription", { code: (chunks) => <code className="font-mono">{chunks}</code> })}
    />
  );
}
