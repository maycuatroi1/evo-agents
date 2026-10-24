"use client";

import { ArrowRight, CircleX, Eye, EyeOff, LoaderCircle, Minimize2, SearchX } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { SearchField } from "@/components/data/search-field";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { displayName } from "@/lib/kg/graph";
import { kgSearchQuery, MAX_QUERY } from "@/lib/kg/queries";
import { buildsHref } from "@/lib/kg/routes";
import type { GraphSummary, KgBuild, KgNode, KindCount } from "@/lib/kg/types";
import { cn } from "@/lib/utils";

import { KindShape } from "./badges";

/** A section of the rail: an overline title, an optional count and its content. */
export function RailSection({
  title,
  aside,
  children,
  testId,
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <section className="flex flex-col gap-2 border-b px-4 py-3 last:border-b-0" data-testid={testId}>
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-[11px] leading-4 font-semibold tracking-[0.06em] text-muted-foreground uppercase">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

const ROW =
  "flex w-full min-w-0 cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-left text-[13px] leading-[18px] transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none max-md:min-h-11";

function Results({ query, kind, data, onPick }: { query: string; kind: string; data: { results: KgNode[] }; onPick: (node: KgNode) => void }) {
  const t = useTranslations("kg.explorer.rail");
  return (
    <div className="flex flex-col gap-1">
      <p className="text-xs text-muted-foreground" role="status" data-testid="kg-results-count">
        {kind ? t("resultsKind", { count: data.results.length, query, kind }) : t("results", { count: data.results.length, query })}
      </p>
      {data.results.length === 0 ? (
        <p className="flex items-start gap-2 text-xs text-muted-foreground">
          <SearchX className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
          {t("noResults")}
        </p>
      ) : (
        <ul className="-mx-2 flex flex-col" aria-label={t("resultsLabel")} data-testid="kg-results">
          {data.results.map((node) => (
            <li key={node.id}>
              <button type="button" className={ROW} onClick={() => onPick(node)} data-testid="kg-result" data-node-id={node.id}>
                <KindShape kind={node.kind} />
                <span className="flex min-w-0 flex-col">
                  <span className="truncate font-medium">{displayName(node)}</span>
                  <span className="truncate font-mono text-[11px] text-fg-subtle">{node.id}</span>
                </span>
                <span className="sr-only">, {node.kind}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** The rail's search: the query in the URL (`?q=`), up to 25 nodes, best match first; picking one explores it. */
export function RailSearch({
  project,
  query,
  kind,
  initialError,
  onCommit,
  onPick,
}: {
  project: string;
  query: string;
  kind: string;
  initialError: ApiErrorInfo | null;
  onCommit: (query: string) => void;
  onPick: (node: KgNode) => void;
}) {
  const t = useTranslations("kg.explorer.rail");
  const search = useHubQuery({ ...kgSearchQuery(browserApi, project, query, kind), enabled: query !== "" }, initialError);
  return (
    <div className="flex flex-col gap-2 border-b px-4 py-3" role="search" data-testid="kg-search">
      <SearchField
        value={query}
        onCommit={onCommit}
        label={t("search")}
        placeholder={t("searchPlaceholder")}
        clearLabel={t("clear")}
        maxLength={MAX_QUERY}
        controls="kg-results-region"
        testId="kg-search-input"
      />
      <div id="kg-results-region" className="empty:hidden">
        {query ? (
          <QueryView state={search} loading={<Skeleton className="h-16 w-full" />}>
            {(data) => <Results query={query} kind={kind} data={data} onPick={onPick} />}
          </QueryView>
        ) : null}
      </div>
    </div>
  );
}

/** Nodes, edges and clusters in one container, then the build the graph comes from, and only an unusual build state. */
export function RailStats({
  project,
  summary,
  clusters,
  latest,
}: {
  project: string;
  summary: GraphSummary | undefined;
  clusters: number | null;
  latest: KgBuild | undefined;
}) {
  const t = useTranslations("kg.explorer.rail");
  const format = useFormatter();
  const graph = summary?.graph ?? null;
  const cells = [
    { id: "nodes", label: t("nodes"), value: graph?.nodes },
    { id: "edges", label: t("edges"), value: graph?.edges },
    { id: "clusters", label: t("clusters"), value: clusters ?? undefined },
  ];
  return (
    <RailSection title={t("stats")} testId="kg-stats">
      <dl className="grid grid-cols-3 divide-x rounded-md border">
        {cells.map((cell) => (
          <div key={cell.id} className="flex min-w-0 flex-col gap-0.5 px-2.5 py-2" data-testid={`kg-stat-${cell.id}`}>
            <dt className="text-xs text-muted-foreground">{cell.label}</dt>
            <dd className="truncate text-[15px] leading-[22px] font-semibold tabular-nums">
              {cell.value === undefined ? <Skeleton className="h-5 w-10" /> : format.number(cell.value)}
            </dd>
          </div>
        ))}
      </dl>
      {graph ? (
        <p className="text-xs text-muted-foreground">
          {t.rich("build", {
            id: graph.build_id,
            when: format.dateTime(new Date(graph.finished_at), { dateStyle: "medium", timeStyle: "short" }),
            link: (chunks) => (
              <Link href={buildsHref(project)} className="text-brand underline underline-offset-4 hover:text-brand-hover">
                {chunks}
              </Link>
            ),
          })}
        </p>
      ) : null}
      {latest?.status === "failed" ? (
        <Link
          href={buildsHref(project, true)}
          className="inline-flex w-fit items-center gap-1.5 rounded-full bg-danger-soft px-2 py-0.5 text-xs font-medium text-danger"
          data-testid="kg-build-failed"
        >
          <CircleX className="size-3.5" aria-hidden="true" />
          {t("buildFailed")}
        </Link>
      ) : latest?.status === "running" || latest?.status === "queued" ? (
        <Link
          href={buildsHref(project)}
          className="inline-flex w-fit items-center gap-1.5 rounded-full bg-running-soft px-2 py-0.5 text-xs font-medium text-running"
          data-testid="kg-building"
        >
          <LoaderCircle className="size-3.5 motion-safe:animate-spin" aria-hidden="true" />
          {t("building")}
        </Link>
      ) : null}
    </RailSection>
  );
}

/** The kinds of the graph with their counts; each row shows or hides its kind on the canvas. */
export function RailKinds({
  kinds,
  hidden,
  present,
  onToggle,
  onShowAll,
}: {
  kinds: KindCount[];
  hidden: ReadonlySet<string>;
  /** The kinds the canvas draws at this scope; the others read quieter, with a title that says so. */
  present: ReadonlySet<string> | null;
  onToggle: (kind: string) => void;
  onShowAll: () => void;
}) {
  const t = useTranslations("kg.explorer.rail");
  const format = useFormatter();
  return (
    <RailSection
      title={t("kinds")}
      aside={
        hidden.size > 0 ? (
          <Button type="button" variant="link" size="sm" className="h-auto p-0 text-xs" onClick={onShowAll}>
            {t("showAll")}
          </Button>
        ) : null
      }
      testId="kg-kinds"
    >
      <ul className="-mx-2 flex flex-col">
        {kinds.map((k) => {
          const off = hidden.has(k.kind);
          const absent = present !== null && !present.has(k.kind);
          return (
            <li key={k.kind}>
              <button
                type="button"
                aria-pressed={!off}
                title={absent ? t("notDrawn", { kind: k.kind }) : off ? t("showKind", { kind: k.kind }) : t("hideKind", { kind: k.kind })}
                onClick={() => onToggle(k.kind)}
                className={cn(ROW, "group py-1", (off || absent) && "text-fg-subtle")}
                data-testid="kg-kind"
                data-kind={k.kind}
                data-drawn={absent ? "false" : "true"}
              >
                <KindShape kind={k.kind} className={cn(off && "opacity-30")} />
                <span className={cn("min-w-0 flex-1 truncate", off && "line-through")}>{k.kind}</span>
                <span className="text-xs text-muted-foreground tabular-nums">{format.number(k.count)}</span>
                {off ? (
                  <EyeOff className="size-3.5 shrink-0" aria-hidden="true" />
                ) : (
                  <Eye className="size-3.5 shrink-0 opacity-0 transition-opacity group-hover:opacity-60" aria-hidden="true" />
                )}
              </button>
            </li>
          );
        })}
      </ul>
      {kinds.length === 0 ? <p className="text-xs text-muted-foreground">{t("noKinds")}</p> : null}
    </RailSection>
  );
}

export type ClusterRow = { id: string; label: string; size: number; colour?: string; weight?: number };

/** A list of clusters (the map's, a cluster's parts or its linked clusters): select on click, open with the arrow. */
export function ClusterList({
  title,
  rows,
  selected,
  onSelect,
  onOpen,
  onHover,
  testId,
}: {
  title: string;
  rows: ClusterRow[];
  selected: string | null;
  onSelect: (id: string) => void;
  onOpen: (id: string) => void;
  onHover: (id: string | null) => void;
  testId: string;
}) {
  const t = useTranslations("kg.explorer.rail");
  const format = useFormatter();
  if (rows.length === 0) return null;
  return (
    <RailSection title={title} aside={<span className="text-xs text-muted-foreground tabular-nums">{rows.length}</span>} testId={testId}>
      <ul className="-mx-2 flex flex-col" onMouseLeave={() => onHover(null)}>
        {rows.map((row) => (
          <li key={row.id} className="group/row flex items-center gap-1" onMouseEnter={() => onHover(row.id)}>
            <button
              type="button"
              aria-current={row.id === selected ? "true" : undefined}
              onClick={() => onSelect(row.id)}
              onFocus={() => onHover(row.id)}
              onBlur={() => onHover(null)}
              className={cn(ROW, "flex-1", row.id === selected && "bg-sidebar-accent")}
              data-testid="kg-cluster-row"
              data-cluster-id={row.id}
            >
              {row.colour ? (
                <span className="size-2.5 shrink-0 rounded-xs" style={{ backgroundColor: row.colour }} aria-hidden="true" />
              ) : null}
              <span className="min-w-0 flex-1 truncate" title={row.label}>
                {row.label}
              </span>
              <span className="text-xs text-muted-foreground tabular-nums">
                {row.weight !== undefined ? t("weight", { count: row.weight }) : format.number(row.size)}
              </span>
            </button>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              className="shrink-0 cursor-pointer text-muted-foreground max-md:size-11"
              aria-label={t("openCluster", { name: row.label })}
              title={t("openCluster", { name: row.label })}
              onClick={() => onOpen(row.id)}
              data-testid="kg-cluster-open"
            >
              <ArrowRight aria-hidden="true" />
            </Button>
          </li>
        ))}
      </ul>
    </RailSection>
  );
}

export type ExpandedRow = { id: string; name: string; kind: string; seed: boolean };

/** The nodes the reader expanded in a node's view, each with a way to collapse it, and the nodes they hid. */
export function ExpandedList({
  rows,
  hidden,
  selected,
  onSelect,
  onCollapse,
  onShowHidden,
}: {
  rows: ExpandedRow[];
  hidden: number;
  selected: string | null;
  onSelect: (id: string) => void;
  onCollapse: (id: string) => void;
  onShowHidden: () => void;
}) {
  const t = useTranslations("kg.explorer.rail");
  return (
    <RailSection title={t("expanded")} aside={<span className="text-xs text-muted-foreground tabular-nums">{rows.length}</span>} testId="kg-expanded">
      <ul className="-mx-2 flex flex-col">
        {rows.map((row) => (
          <li key={row.id} className="flex items-center gap-1">
            <button
              type="button"
              aria-current={row.id === selected ? "true" : undefined}
              onClick={() => onSelect(row.id)}
              className={cn(ROW, "flex-1", row.id === selected && "bg-sidebar-accent")}
              data-testid="kg-expanded-row"
            >
              <KindShape kind={row.kind} />
              <span className="min-w-0 flex-1 truncate" title={row.name}>
                {row.name}
              </span>
              {row.seed ? <span className="text-[11px] text-fg-subtle">{t("seed")}</span> : null}
            </button>
            {row.seed ? null : (
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                className="shrink-0 cursor-pointer text-muted-foreground max-md:size-11"
                aria-label={t("collapse", { name: row.name })}
                title={t("collapse", { name: row.name })}
                onClick={() => onCollapse(row.id)}
              >
                <Minimize2 aria-hidden="true" />
              </Button>
            )}
          </li>
        ))}
      </ul>
      {hidden > 0 ? (
        <p className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground" data-testid="kg-hidden-nodes">
          {t("hidden", { count: hidden })}
          <Button type="button" variant="link" size="sm" className="h-auto p-0 text-xs" onClick={onShowHidden}>
            {t("showHidden")}
          </Button>
        </p>
      ) : null}
    </RailSection>
  );
}
