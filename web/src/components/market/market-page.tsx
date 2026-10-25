"use client";

import { useQuery } from "@tanstack/react-query";
import { Settings2, Store } from "lucide-react";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { DataCard, DataToolbar } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { ToolbarField, ToolbarFilters } from "@/components/data/filter-sheet";
import { NAME_LINK, Tag } from "@/components/data/identifier";
import { SearchField } from "@/components/data/search-field";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { type ActiveFilter, EmptyState, NoResults, TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { whoamiQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { activeFilters, filterPlugins, filtersSearch, type MarketFilters, NO_FILTERS, parseFilters } from "./model";
import { MarketStatusBadge, PriceTag } from "./parts";
import { adminMarketHref, type MarketList, type MarketPlugin, marketPluginHref, marketQuery } from "./queries";

const NARROW_HIDDEN = { publisher: "hidden lg:table-cell", categories: "hidden md:table-cell", price: "hidden sm:table-cell" };

function PluginsTable({ plugins }: { plugins: MarketPlugin[] }) {
  const t = useTranslations("market");
  const columns = useMemo(() => {
    const helper = dataTableColumns<MarketPlugin>();
    return helper.columns([
      helper.accessor("name", {
        header: () => t("columns.plugin"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => {
          const plugin = info.row.original;
          return (
            <CellMain sub={plugin.description} subTitle={plugin.description}>
              <Link
                href={marketPluginHref(plugin.source, plugin.id)}
                className={cn(NAME_LINK, "truncate")}
                data-market-link={plugin.id}
              >
                {plugin.name}
              </Link>
            </CellMain>
          );
        },
      }),
      helper.accessor((row) => row.publisher.name ?? row.publisher.id, {
        id: "publisher",
        header: () => t("columns.publisher"),
        sortFn: "alphanumeric",
        cell: (info) => <span className="truncate text-[13px]">{info.getValue()}</span>,
      }),
      helper.display({
        id: "categories",
        header: () => t("columns.categories"),
        cell: (info) => (
          <span className="flex flex-wrap gap-1">
            {info.row.original.categories.map((category) => (
              <Tag key={category}>{category}</Tag>
            ))}
          </span>
        ),
      }),
      helper.display({
        id: "price",
        header: () => t("columns.price"),
        cell: (info) => <PriceTag price={info.row.original.price} />,
      }),
      helper.accessor("status", {
        header: () => t("columns.status"),
        cell: (info) => <MarketStatusBadge status={info.getValue()} />,
      }),
    ]);
  }, [t]);

  return (
    <DataTable
      data={plugins}
      columns={columns}
      caption={t("caption")}
      getRowId={(row) => `${row.source}/${row.id}`}
      columnClassNames={NARROW_HIDDEN}
      testId="market-table"
      mobile={(plugin) => ({
        title: plugin.name,
        titleText: plugin.name,
        href: marketPluginHref(plugin.source, plugin.id),
        status: <MarketStatusBadge status={plugin.status} />,
        meta: plugin.description,
        metaText: plugin.description,
        data: { "market-plugin": plugin.id },
      })}
    />
  );
}

function Facet({
  label,
  all,
  value,
  options,
  onChange,
  testId,
}: {
  label: string;
  all: string;
  value: string | null;
  options: readonly string[];
  onChange: (value: string | null) => void;
  testId: string;
}) {
  return (
    <ToolbarField label={label}>
      {(id) => (
        <NativeSelect
          id={id}
          size="sm"
          className="max-md:w-full md:w-44"
          value={value ?? ""}
          onChange={(event) => onChange(event.target.value || null)}
          data-testid={testId}
        >
          <NativeSelectOption value="">{all}</NativeSelectOption>
          {options.map((option) => (
            <NativeSelectOption key={option} value={option}>
              {option}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      )}
    </ToolbarField>
  );
}

function MarketBody({ market, filters, setFilters }: { market: MarketList; filters: MarketFilters; setFilters: (next: MarketFilters) => void }) {
  const t = useTranslations("market");
  const tStates = useTranslations("states.noResults");
  const shown = filterPlugins(market.plugins, filters);
  const active: ActiveFilter[] = [
    ...(filters.q ? [{ label: tStates("search"), value: filters.q }] : []),
    ...(filters.industry ? [{ label: t("filters.industry"), value: filters.industry }] : []),
    ...(filters.category ? [{ label: t("filters.category"), value: filters.category }] : []),
  ];
  const count = activeFilters(filters)
    ? t("summary.filtered", { shown: shown.length, count: market.plugins.length })
    : t("summary.all", { count: market.plugins.length });
  return (
    <DataCard
      toolbar={
        <DataToolbar label={t("caption")} count={count} countTestId="market-summary">
          <SearchField
            value={filters.q}
            onCommit={(q) => setFilters({ ...filters, q })}
            label={t("filters.search")}
            placeholder={t("filters.placeholder")}
            clearLabel={t("filters.clear")}
            debounce={150}
            maxLength={200}
            className="sm:w-64 max-md:flex-1"
            testId="market-search"
          />
          <ToolbarFilters
            active={(filters.industry ? 1 : 0) + (filters.category ? 1 : 0)}
            summary={count}
            onClear={() => setFilters({ ...filters, industry: null, category: null })}
            testId="market-filters"
          >
            <div className="flex min-w-0 flex-wrap items-center gap-2 max-md:flex-col max-md:items-stretch max-md:gap-5">
              <Facet
                label={t("filters.industry")}
                all={t("filters.allIndustries")}
                value={filters.industry}
                options={market.industries}
                onChange={(industry) => setFilters({ ...filters, industry })}
                testId="market-industry"
              />
              <Facet
                label={t("filters.category")}
                all={t("filters.allCategories")}
                value={filters.category}
                options={market.categories}
                onChange={(category) => setFilters({ ...filters, category })}
                testId="market-category"
              />
            </div>
          </ToolbarFilters>
        </DataToolbar>
      }
    >
      {shown.length === 0 ? (
        <NoResults title={t("noResults")} filters={active} onClear={() => setFilters(NO_FILTERS)} />
      ) : (
        <PluginsTable plugins={shown} />
      )}
    </DataCard>
  );
}

/** The plugins of every market the hub reads, for every member, with search and industry and category filters. */
export function MarketPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("market");
  const params = useSearchParams();
  const pathname = usePathname();
  const filters = parseFilters(params);
  const state = useHubQuery(marketQuery(browserApi), initialError);
  const { data: me } = useQuery(whoamiQuery(browserApi));
  // The History API: Next.js syncs useSearchParams with it, and the list is filtered here without a round trip.
  const setFilters = (next: MarketFilters) => window.history.replaceState(null, "", `${pathname}${filtersSearch(next)}`);
  const manage = me?.admin ? (
    <Button asChild variant="outline" size="sm">
      <Link href={adminMarketHref()} data-testid="market-manage">
        <Settings2 aria-hidden="true" />
        {t("manage")}
      </Link>
    </Button>
  ) : null;

  return (
    <>
      <PageHeader
        title={t("title")}
        tags={state.status === "success" ? <Badge variant="secondary">{t("count", { count: state.data.plugins.length })}</Badge> : null}
        actions={manage}
      />
      <QueryView state={state} loading={<TableSkeleton rows={5} toolbar />}>
        {(market) =>
          market.plugins.length === 0 && market.markets.length === 0 ? (
            <EmptyState icon={Store} title={t("empty.title")} description={me?.admin ? t("empty.admin") : t("empty.member")} testId="market-empty">
              {manage}
            </EmptyState>
          ) : (
            <MarketBody market={market} filters={filters} setFilters={setFilters} />
          )
        }
      </QueryView>
    </>
  );
}
