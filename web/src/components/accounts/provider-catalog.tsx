"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronDown, Search } from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { ProviderMark } from "./provider-mark";
import { CATALOG_FILTERS, type CatalogFilter, catalogMatches, filterCounts, hostOf } from "./provider-model";
import { type CatalogProvider, catalogQuery } from "./provider-queries";

/** How many entries the catalog draws at first, and how many more each Show more adds. */
const PAGE = 24;

/**
 * The catalog of Add provider: a search by name and id, a filter by group with how many each holds, then the entries
 * as buttons, a page at a time. Claude signs in on a worker; any other entry opens the provider form.
 */
export function ProviderCatalog({ onPick }: { onPick: (entry: CatalogProvider) => void }) {
  const t = useTranslations("accounts.provider.catalog");
  const catalog = useQuery(catalogQuery(browserApi));
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<CatalogFilter>("all");
  const [shown, setShown] = useState(PAGE);
  const entries = useMemo(() => catalog.data?.providers ?? [], [catalog.data]);
  const counts = useMemo(() => filterCounts(entries), [entries]);
  const found = useMemo(() => catalogMatches(entries, query, filter), [entries, query, filter]);
  const page = found.slice(0, shown);

  return (
    <>
      <DialogHeader>
        <DialogTitle className="text-lg font-semibold">{t("title")}</DialogTitle>
        <DialogDescription className="text-pretty">{t("description")}</DialogDescription>
      </DialogHeader>
      <div className="flex flex-col gap-3">
        <div className="relative">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-fg-subtle" aria-hidden="true" />
          <Input
            type="search"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setShown(PAGE);
            }}
            placeholder={t("search")}
            aria-label={t("search")}
            autoComplete="off"
            spellCheck={false}
            className="h-9 pl-8"
            data-testid="provider-catalog-search"
          />
        </div>
        <div className="flex flex-wrap gap-1.5" role="group" aria-label={t("groups")}>
          {CATALOG_FILTERS.map((item) => (
            <Button
              key={item}
              type="button"
              size="sm"
              variant={filter === item ? "secondary" : "ghost"}
              aria-pressed={filter === item}
              onClick={() => {
                setFilter(item);
                setShown(PAGE);
              }}
              className={cn("h-8 gap-1.5", filter === item && "ring-1 ring-border")}
              data-testid="provider-catalog-group"
              data-filter={item}
            >
              {t(`filters.${item}`)}
              <span className="text-xs text-fg-muted tabular-nums">{counts[item]}</span>
            </Button>
          ))}
        </div>
        <p className="text-xs text-fg-muted" aria-live="polite" data-testid="provider-catalog-count">
          {catalog.isSuccess ? t("count", { count: found.length }) : ""}
        </p>
      </div>
      {catalog.isPending ? (
        <ul className="grid gap-2 sm:grid-cols-2" aria-busy="true" aria-label={t("loading")} data-testid="provider-catalog-loading">
          {Array.from({ length: 6 }, (_, index) => (
            <li key={index}>
              <Skeleton className="h-16 w-full" />
            </li>
          ))}
        </ul>
      ) : catalog.isError ? (
        <div className="flex flex-col items-start gap-2" role="alert" data-testid="provider-catalog-error">
          <p className="text-sm text-danger">{t("loadFailed")}</p>
          <Button type="button" variant="outline" size="sm" onClick={() => void catalog.refetch()}>
            {t("retry")}
          </Button>
        </div>
      ) : found.length === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-fg-muted" data-testid="provider-catalog-empty">
          {t("noMatch")}
        </p>
      ) : (
        <div className="flex flex-col gap-3">
          <ul className="grid gap-2 sm:grid-cols-2" data-testid="provider-catalog">
            {page.map((entry) => (
              <li key={entry.id} className="min-w-0">
                <CatalogEntry entry={entry} onPick={onPick} />
              </li>
            ))}
          </ul>
          {found.length > page.length ? (
            <Button type="button" variant="outline" className="self-center" onClick={() => setShown(shown + PAGE)} data-testid="provider-catalog-more">
              <ChevronDown aria-hidden="true" />
              {t("more", { count: found.length - page.length })}
            </Button>
          ) : null}
        </div>
      )}
    </>
  );
}

function CatalogEntry({ entry, onPick }: { entry: CatalogProvider; onPick: (entry: CatalogProvider) => void }) {
  const t = useTranslations("accounts.provider.catalog");
  const host = entry.via === "login" ? null : hostOf(entry.base_url);
  return (
    <button
      type="button"
      onClick={() => onPick(entry)}
      className="flex w-full min-w-0 items-center gap-3 rounded-md border bg-card px-3 py-2.5 text-left transition-colors outline-none hover:bg-accent focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
      data-testid="provider-catalog-entry"
      data-provider-id={entry.id}
    >
      <ProviderMark id={entry.id} entry={entry} size="sm" />
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="flex flex-wrap items-center gap-1.5">
          <span className="text-sm font-medium [overflow-wrap:anywhere]">{entry.name}</span>
          {entry.free ? <Badge variant="success">{t("free")}</Badge> : null}
        </span>
        <span className="truncate font-mono text-xs text-fg-muted">
          {entry.via === "login" ? t("login") : (host ?? (entry.group === "custom" ? t("custom") : entry.id))}
        </span>
      </span>
    </button>
  );
}
