"use client";

import { ChartLine, X } from "lucide-react";
import dynamic from "next/dynamic";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { DataCard } from "@/components/data/data-card";
import { Segmented } from "@/components/data/segmented";
import { When } from "@/components/data/when";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { MAX_TENANT_CHARS, type Signal, type SignalKey, signalsQuery } from "@/lib/signal-queries";

import { KindTag } from "./keys-table";
import { pointsOf, type Range, RANGES, SERIES_HEIGHT, type SignalsQuery, seriesFilter } from "./model";

const SeriesChart = dynamic(() => import("./series-chart"), {
  ssr: false,
  loading: () => <Skeleton className="w-full rounded-md" style={{ height: SERIES_HEIGHT }} />,
});

/** The tenant a series is read for: a field that applies on Enter, and a button that clears it. */
function TenantFilter({ tenant, onChange }: { tenant: string | null; onChange: (tenant: string | null) => void }) {
  const t = useTranslations("signals.series");
  const id = useId();
  const [value, setValue] = useState(tenant ?? "");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onChange(value.trim() || null);
  };
  return (
    <form onSubmit={submit} className="flex items-end gap-2" role="search" aria-label={t("tenantSearch")} data-testid="series-tenant-form">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={id}>{t("tenant")}</Label>
        <Input id={id} value={value} onChange={(event) => setValue(event.target.value)} maxLength={MAX_TENANT_CHARS} placeholder={t("tenantPlaceholder")} className="w-48" data-testid="series-tenant" />
      </div>
      <Button type="submit" variant="outline">
        {t("apply")}
      </Button>
      {tenant ? (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={t("clearTenant", { tenant })}
          onClick={() => {
            setValue("");
            onChange(null);
          }}
          data-testid="series-tenant-clear"
        >
          <X aria-hidden="true" />
        </Button>
      ) : null}
    </form>
  );
}

/** The values of a series as a table, the newest first: when, the value or the feedback, the tenant, the source. */
function ValuesTable({ signals, signalKey }: { signals: readonly Signal[]; signalKey: SignalKey }) {
  const t = useTranslations("signals.series");
  const format = useFormatter();
  return (
    <Table scrollLabel={t("tableLabel", { key: signalKey.name })} className="text-[13px] leading-[18px]" data-testid="series-table">
      <TableCaption className="sr-only">{t("tableLabel", { key: signalKey.name })}</TableCaption>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead className="h-9 bg-surface-sunken pl-4 text-xs">{t("measuredAt")}</TableHead>
          <TableHead className="h-9 bg-surface-sunken text-xs">{signalKey.kind === "number" ? t("value") : t("feedback")}</TableHead>
          <TableHead className="h-9 bg-surface-sunken text-xs">{t("tenant")}</TableHead>
          <TableHead className="h-9 bg-surface-sunken pr-4 text-xs max-md:hidden">{t("source")}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {signals.map((signal) => (
          <TableRow key={signal.id} data-testid="series-row" data-signal={signal.id}>
            <TableCell className="pl-4 text-muted-foreground">
              <When value={signal.measured_at} short />
            </TableCell>
            <TableCell className="whitespace-normal text-foreground">
              {signal.value !== null ? (
                <span className="tabular-nums">
                  {format.number(signal.value, { maximumFractionDigits: 4 })}
                  {signalKey.unit ? <span className="ml-1 text-xs text-muted-foreground">{signalKey.unit}</span> : null}
                </span>
              ) : (
                <span className="line-clamp-3 [overflow-wrap:anywhere]">{signal.text}</span>
              )}
              {signal.synthetic ? <span className="ml-2 text-xs text-review">{t("synthetic")}</span> : null}
            </TableCell>
            <TableCell className="text-muted-foreground">{signal.tenant ?? "-"}</TableCell>
            <TableCell className="pr-4 text-muted-foreground max-md:hidden">{signal.ingest_key ?? signal.login ?? signal.source}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

/**
 * One key's signals over the span picked (7, 30 or 90 days, or a year) and of one tenant when given: numbers as a line
 * over time and as a table; feedback as the table of what people said. It shows values and their series, no analysis.
 */
export function Series({
  project,
  signalKey,
  query,
  onChange,
  initialError,
}: {
  project: string;
  signalKey: SignalKey;
  query: SignalsQuery;
  onChange: (query: SignalsQuery) => void;
  initialError: ApiErrorInfo | null;
}) {
  const t = useTranslations("signals.series");
  const state = useHubQuery(signalsQuery(browserApi, project, seriesFilter(query)), initialError);
  const ranges = RANGES.map((days) => ({ value: String(days) as `${Range}`, label: t(`range.${days}`), testId: `series-range-${days}` }));
  return (
    <section aria-labelledby="series-title" className="flex min-w-0 flex-col gap-4" data-testid="signal-series" data-key={signalKey.name}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
        <h2 id="series-title" className="min-w-0 font-mono text-[15px] leading-[22px] font-medium [overflow-wrap:anywhere]">
          {signalKey.name}
        </h2>
        <KindTag kind={signalKey.kind} />
        {signalKey.description ? <p className="w-full text-[13px] text-muted-foreground [overflow-wrap:anywhere]">{signalKey.description}</p> : null}
      </div>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <TenantFilter key={query.tenant ?? ""} tenant={query.tenant} onChange={(tenant) => onChange({ ...query, tenant })} />
        <Segmented label={t("rangeLabel")} value={String(query.days) as `${Range}`} options={ranges} onChange={(days) => onChange({ ...query, days: Number(days) as Range })} />
      </div>
      <QueryView state={state} loading={<TableSkeleton rows={4} />}>
        {(signals) => {
          if (signals.length === 0) {
            return (
              <EmptyState
                icon={ChartLine}
                title={t("emptyTitle")}
                description={query.tenant ? t("emptyTenant", { tenant: query.tenant, days: query.days }) : t("empty", { days: query.days })}
                testId="series-empty"
              />
            );
          }
          const points = pointsOf(signals);
          return (
            <div className="flex min-w-0 flex-col gap-4">
              <p className="text-xs text-muted-foreground" data-testid="series-count">
                {t("count", { count: signals.length, days: query.days })}
              </p>
              {signalKey.kind === "number" && points.length > 0 ? (
                <div className="rounded-md border bg-card p-4 shadow-raised">
                  <SeriesChart points={points} unit={signalKey.unit} label={t("chartLabel", { key: signalKey.name, count: points.length })} />
                </div>
              ) : null}
              <DataCard testId="series-card">
                <ValuesTable signals={signals} signalKey={signalKey} />
              </DataCard>
            </div>
          );
        }}
      </QueryView>
    </section>
  );
}
