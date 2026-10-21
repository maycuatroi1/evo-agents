"use client";

import { RadioTower } from "lucide-react";
import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { DataCard } from "@/components/data/data-card";
import { Banner } from "@/components/feedback/banner";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { type SignalKey, signalKeysQuery } from "@/lib/signal-queries";

import { KeysTable } from "./keys-table";
import { type SignalsQuery, searchOf } from "./model";
import { Series } from "./series";

function base(project: string): string {
  return `/p/${encodeURIComponent(project)}/signals`;
}

function SignalsView({ project, keys, query, seriesError }: { project: string; keys: SignalKey[]; query: SignalsQuery; seriesError: ApiErrorInfo | null }) {
  const t = useTranslations("signals.page");
  const router = useRouter();
  const go = useCallback((next: SignalsQuery) => router.replace(`${base(project)}${searchOf(next)}` as Route, { scroll: false }), [project, router]);
  const hrefOf = useCallback((key: string) => `${base(project)}${searchOf({ key, tenant: null, days: query.days })}` as Route, [project, query.days]);
  const selected = query.key ? keys.find((key) => key.name === query.key) : undefined;
  if (keys.length === 0) {
    return <EmptyState icon={RadioTower} title={t("emptyTitle")} description={t("emptyText")} testId="signals-empty" />;
  }
  return (
    <div className="flex min-w-0 flex-col gap-6">
      <section aria-labelledby="signal-keys-title">
        <h2 id="signal-keys-title" className="sr-only">
          {t("keysHeading")}
        </h2>
        <DataCard testId="signal-keys-card">
          <KeysTable project={project} keys={keys} selected={query.key} hrefOf={hrefOf} />
        </DataCard>
      </section>
      {query.key && !selected ? (
        <Banner icon={RadioTower} title={t("unknownKey", { key: query.key })} testId="signals-unknown-key">
          {t("unknownKeyText")}
        </Banner>
      ) : null}
      {selected ? <Series key={selected.name} project={project} signalKey={selected} query={query} onChange={go} initialError={seriesError} /> : null}
      {!query.key ? <p className="text-[13px] text-muted-foreground">{t("pick")}</p> : null}
    </div>
  );
}

/**
 * A project's signals (/p/{project}/signals): the keys of its registry with their latest value, and one key's series
 * over a span of days, of one tenant when asked (`?key=&tenant=&days=`), the page the value check links from the
 * evidence it writes. Services send signals with an ingest key; members read them here. It shows values and their
 * series over time; it analyses nothing.
 */
export function SignalsPage({
  project,
  query,
  initialError,
  seriesError,
}: {
  project: string;
  query: SignalsQuery;
  initialError: ApiErrorInfo | null;
  seriesError: ApiErrorInfo | null;
}) {
  const t = useTranslations("signals.page");
  const state = useHubQuery(signalKeysQuery(browserApi, project), initialError);
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="signals-page">
      <PageHeader title={t("title")} />
      <QueryView state={state} loading={<TableSkeleton rows={3} />}>
        {(keys) => <SignalsView project={project} keys={keys} query={query} seriesError={seriesError} />}
      </QueryView>
    </div>
  );
}
