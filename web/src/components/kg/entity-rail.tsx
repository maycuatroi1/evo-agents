"use client";

import { Eye, EyeOff, Minimize2, SearchX } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { useState } from "react";

import { SearchField } from "@/components/data/search-field";
import { useHubQuery } from "@/components/states/query-view";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { type EntityGraphNode, kindColour, matchEntities } from "@/lib/kg/entity-graph";
import { entitySearchQuery } from "@/lib/kg/entity-queries";
import { MAX_QUERY } from "@/lib/kg/queries";
import { cn } from "@/lib/utils";

import { RailSection } from "./explorer-rail";

const ROW =
  "flex w-full min-w-0 cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-left text-[13px] leading-[18px] transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none max-md:min-h-11";
const ISOLATED_SHOWN = 30;

/** A kind's colour as a dot, the same tone the canvas draws it in. */
export function TypeDot({ kind, className }: { kind: string; className?: string }) {
  const { resolvedTheme } = useTheme();
  return (
    <span
      className={cn("size-2.5 shrink-0 rounded-full", className)}
      style={{ backgroundColor: kindColour(kind, resolvedTheme === "dark") }}
      aria-hidden="true"
    />
  );
}

function Row({ node, onPick, aside }: { node: Pick<EntityGraphNode, "id" | "kind" | "name">; onPick: (id: string) => void; aside?: string }) {
  return (
    <li>
      <button type="button" className={ROW} onClick={() => onPick(node.id)} data-testid="kg-entity-row" data-node-id={node.id}>
        <TypeDot kind={node.kind} />
        <span className="min-w-0 flex-1 truncate">{node.name}</span>
        {aside ? <span className="shrink-0 text-[11px] text-fg-subtle tabular-nums">{aside}</span> : null}
        <span className="sr-only">, {node.kind}</span>
      </button>
    </li>
  );
}

/**
 * Find an entity: the drawn ones whose name holds the words first (picking one selects it and brings it into view),
 * then the graph's search over the same kinds for the ones outside the view (picking one centres the view on it).
 */
export function EntitySearch({
  project,
  nodes,
  kinds,
  onPickDrawn,
  onPickOther,
}: {
  project: string;
  nodes: readonly EntityGraphNode[];
  kinds: readonly string[];
  onPickDrawn: (id: string) => void;
  onPickOther: (id: string) => void;
}) {
  const t = useTranslations("kg.explorer.entities.rail");
  const [query, setQuery] = useState("");
  const drawn = matchEntities(nodes, query);
  const ids = new Set(nodes.map((node) => node.id));
  const remote = useHubQuery({ ...entitySearchQuery(browserApi, project, query, kinds), enabled: query !== "" && kinds.length > 0 }, null);
  const others = remote.status === "success" ? remote.data.results.filter((node) => node.id.startsWith("entity:") && !ids.has(node.id)) : [];
  return (
    <div className="flex flex-col gap-2 border-b px-4 py-3" role="search" data-testid="kg-entity-search">
      <SearchField
        value={query}
        onCommit={setQuery}
        label={t("search")}
        placeholder={t("searchPlaceholder")}
        clearLabel={t("clear")}
        maxLength={MAX_QUERY}
        controls="kg-entity-results"
        debounce={200}
        testId="kg-entity-search-input"
      />
      <div id="kg-entity-results" className="flex flex-col gap-2 empty:hidden">
        {query ? (
          <>
            <p className="text-xs text-muted-foreground" role="status">
              {t("inView", { count: drawn.length, query })}
            </p>
            {drawn.length > 0 ? (
              <ul className="-mx-2 flex flex-col" aria-label={t("inViewLabel")}>
                {drawn.map((node) => (
                  <Row key={node.id} node={node} onPick={onPickDrawn} />
                ))}
              </ul>
            ) : null}
            {remote.status === "loading" ? (
              <Skeleton className="h-8 w-full" />
            ) : others.length > 0 ? (
              <>
                <p className="text-xs text-muted-foreground">{t("elsewhere")}</p>
                <ul className="-mx-2 flex flex-col" aria-label={t("elsewhere")}>
                  {others.map((node) => (
                    <Row key={node.id} node={{ id: node.id, kind: node.kind, name: node.name ?? node.id }} onPick={onPickOther} />
                  ))}
                </ul>
              </>
            ) : drawn.length === 0 ? (
              <p className="flex items-start gap-2 text-xs text-muted-foreground">
                <SearchX className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
                {t("noResults")}
              </p>
            ) : null}
          </>
        ) : null}
      </div>
    </div>
  );
}

/** The figures of what is drawn against what the reader can see of the graph. */
export function EntityStats({ shown, relations, total, totalRelations }: { shown: number; relations: number; total: number | null; totalRelations: number | null }) {
  const t = useTranslations("kg.explorer.entities.rail");
  const format = useFormatter();
  const cells = [
    { id: "entities", label: t("entities"), value: shown, of: total },
    { id: "relations", label: t("relations"), value: relations, of: totalRelations },
  ];
  return (
    <RailSection title={t("stats")} testId="kg-entity-stats">
      <dl className="grid grid-cols-2 divide-x rounded-md border">
        {cells.map((cell) => (
          <div key={cell.id} className="flex min-w-0 flex-col gap-0.5 px-2.5 py-2">
            <dt className="text-xs text-muted-foreground">{cell.label}</dt>
            <dd className="truncate text-[15px] leading-[22px] font-semibold tabular-nums">
              {format.number(cell.value)}
              {cell.of !== null && cell.of > cell.value ? (
                <span className="text-xs font-normal text-muted-foreground"> {t("of", { total: format.number(cell.of) })}</span>
              ) : null}
            </dd>
          </div>
        ))}
      </dl>
    </RailSection>
  );
}

/** The legend of the canvas's colours, each kind a switch that hides or shows its entities. */
export function TypeFilter({
  kinds,
  hidden,
  onToggle,
  onShowAll,
}: {
  kinds: { kind: string; count: number }[];
  hidden: ReadonlySet<string>;
  onToggle: (kind: string) => void;
  onShowAll: () => void;
}) {
  const t = useTranslations("kg.explorer.entities.rail");
  const format = useFormatter();
  return (
    <RailSection
      title={t("types")}
      aside={
        hidden.size > 0 ? (
          <Button type="button" variant="ghost" size="sm" className="h-6 cursor-pointer px-2 text-xs max-md:h-11" onClick={onShowAll}>
            {t("showAll")}
          </Button>
        ) : null
      }
      testId="kg-entity-types"
    >
      <ul className="-mx-2 flex flex-col">
        {kinds.map(({ kind, count }) => {
          const off = hidden.has(kind);
          return (
            <li key={kind}>
              <button
                type="button"
                className={cn(ROW, off && "text-muted-foreground")}
                aria-pressed={!off}
                aria-label={off ? t("showType", { kind }) : t("hideType", { kind })}
                onClick={() => onToggle(kind)}
                data-testid="kg-entity-type"
                data-kind={kind}
              >
                <TypeDot kind={kind} className={cn(off && "opacity-30")} />
                <span className={cn("min-w-0 flex-1 truncate", off && "line-through")}>{kind}</span>
                <span className="shrink-0 text-[11px] text-fg-subtle tabular-nums">{format.number(count)}</span>
                {off ? <EyeOff className="size-3.5 shrink-0" aria-hidden="true" /> : <Eye className="size-3.5 shrink-0 text-fg-subtle" aria-hidden="true" />}
              </button>
            </li>
          );
        })}
      </ul>
    </RailSection>
  );
}

/** Entities without a relation among those shown: listed here rather than scattered over the canvas. */
export function IsolatedList({ nodes, selected, onPick }: { nodes: EntityGraphNode[]; selected: string | null; onPick: (id: string) => void }) {
  const t = useTranslations("kg.explorer.entities.rail");
  const format = useFormatter();
  const [all, setAll] = useState(false);
  if (nodes.length === 0) return null;
  const shown = all ? nodes : nodes.slice(0, ISOLATED_SHOWN);
  return (
    <RailSection title={t("isolated")} aside={<span className="text-xs text-muted-foreground tabular-nums">{format.number(nodes.length)}</span>} testId="kg-entity-isolated">
      <p className="text-xs text-muted-foreground">{t("isolatedHint")}</p>
      <ul className="-mx-2 flex flex-col">
        {shown.map((node) => (
          <li key={node.id} className={cn(node.id === selected && "rounded-sm bg-accent")}>
            <button type="button" className={ROW} onClick={() => onPick(node.id)} aria-current={node.id === selected ? "true" : undefined} data-testid="kg-entity-isolated-row">
              <TypeDot kind={node.kind} />
              <span className="min-w-0 flex-1 truncate">{node.name}</span>
              <span className="shrink-0 text-[11px] text-fg-subtle tabular-nums" title={t("docs")}>
                {format.number(node.docs)}
              </span>
              <span className="sr-only">, {node.kind}</span>
            </button>
          </li>
        ))}
      </ul>
      {nodes.length > ISOLATED_SHOWN ? (
        <Button type="button" variant="ghost" size="sm" className="w-fit cursor-pointer" onClick={() => setAll(!all)}>
          {all ? t("showFewer") : t("showAllCount", { count: nodes.length })}
        </Button>
      ) : null}
    </RailSection>
  );
}

/** The entities whose neighbours the reader opened, each with a way to close them again. */
export function ExpandedEntities({ nodes, onPick, onCollapse }: { nodes: Pick<EntityGraphNode, "id" | "kind" | "name">[]; onPick: (id: string) => void; onCollapse: (id: string) => void }) {
  const t = useTranslations("kg.explorer.entities.rail");
  if (nodes.length === 0) return null;
  return (
    <RailSection title={t("expanded")} testId="kg-entity-expanded">
      <ul className="-mx-2 flex flex-col">
        {nodes.map((node) => (
          <li key={node.id} className="flex items-center gap-1">
            <button type="button" className={ROW} onClick={() => onPick(node.id)}>
              <TypeDot kind={node.kind} />
              <span className="min-w-0 flex-1 truncate">{node.name}</span>
            </button>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              className="shrink-0 cursor-pointer max-md:size-11"
              aria-label={t("collapse", { name: node.name })}
              title={t("collapse", { name: node.name })}
              onClick={() => onCollapse(node.id)}
            >
              <Minimize2 aria-hidden="true" />
            </Button>
          </li>
        ))}
      </ul>
    </RailSection>
  );
}
