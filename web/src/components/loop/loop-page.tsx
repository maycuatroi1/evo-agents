"use client";

import { useQuery } from "@tanstack/react-query";
import { Binoculars, CircleCheck, CircleOff, Clock, Eye, Goal, IterationCw, ListTree, MessageSquare, OctagonPause, Rows3 } from "lucide-react";
import Link from "next/link";
import type { Route } from "next";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { DataCard, DataToolbar } from "@/components/data/data-card";
import { FacetGroup } from "@/components/data/facet-group";
import { ToolbarField, ToolbarFilters } from "@/components/data/filter-sheet";
import { MetricStrip } from "@/components/data/metric-strip";
import { Segmented } from "@/components/data/segmented";
import { Banner } from "@/components/feedback/banner";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton, TableSkeleton } from "@/components/states/states";
import { StatusPill } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import {
  goalsHref,
  goalsQuery,
  ITEM_KINDS,
  ITEM_STATES,
  LOOP_ITEMS_LIMIT,
  LOOP_REFRESH_MS,
  loopItemsQuery,
  loopQuery,
  type ProjectLoop,
} from "@/lib/loop-queries";

import { ChainList } from "./chain-list";
import { useLoopViewer } from "./hooks";
import { ItemsTable } from "./items-table";
import { BreakerBanner, LoopSwitch } from "./loop-switch";
import { byState, countOf, itemsFilterOf, type LoopFilters, loopSearch, type LoopView, readLoopFilters, STATE_LOOKS, totalCount } from "./model";

/** The Loop's own state beside the page's title: on, off, or stopped by its breaker. */
function LoopStatePill({ loop }: { loop: ProjectLoop }) {
  const t = useTranslations("loop.page");
  if (loop.breaker === "tripped") return <StatusPill size="lg" look={{ tone: "attention", icon: OctagonPause }} label={t("stopped")} data-testid="loop-state" data-state="stopped" />;
  if (!loop.enabled) return <StatusPill size="lg" look={{ tone: "outline", icon: CircleOff }} label={t("off")} data-testid="loop-state" data-state="off" />;
  return <StatusPill size="lg" look={{ tone: "success", icon: IterationCw }} label={t("on")} data-testid="loop-state" data-state="on" />;
}

function Strip({ loop }: { loop: ProjectLoop }) {
  const t = useTranslations("loop.metrics");
  const count = (state: Parameters<typeof countOf>[1]) => countOf(loop.items, state);
  return (
    <MetricStrip
      label={t("label")}
      testId="loop-summary"
      metrics={[
        { id: "running", label: t("running"), icon: IterationCw, value: count("running"), tone: "running", live: true, meta: t("runningMeta") },
        { id: "waiting_human", label: t("waiting_human"), icon: MessageSquare, value: count("waiting_human"), tone: "attention", meta: t("waitingMeta") },
        { id: "queued", label: t("queued"), icon: Clock, value: count("queued"), meta: t("queuedMeta") },
        { id: "watching", label: t("watching"), icon: Binoculars, value: count("watching"), meta: t("watchingMeta") },
        { id: "done", label: t("done"), icon: CircleCheck, value: count("done"), extra: count("cancelled") ? t("cancelled", { count: count("cancelled") }) : null },
      ]}
    />
  );
}

function useLoopFilters() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const filters = readLoopFilters(params);
  const set = useCallback(
    (next: LoopFilters) => {
      const search = loopSearch(next);
      router.replace((search ? `${pathname}?${search}` : pathname) as Route, { scroll: false });
    },
    [router, pathname],
  );
  return { filters, set };
}

/** The work items, by state or by chain, filtered by state, kind and goal from the page's address. */
function ItemsCard({ project, loop, itemsError }: { project: string; loop: ProjectLoop; itemsError: ApiErrorInfo | null }) {
  const t = useTranslations("loop.items");
  const state = useTranslations("loop.state");
  const kind = useTranslations("loop.kind");
  const { filters, set } = useLoopFilters();
  const items = useHubQuery({ ...loopItemsQuery(browserApi, project, itemsFilterOf(filters)), refetchInterval: LOOP_REFRESH_MS }, itemsError);
  const { data: goals } = useQuery(goalsQuery(browserApi, project));
  const active = (filters.state ? 1 : 0) + (filters.kind ? 1 : 0) + (filters.goal ? 1 : 0);
  const shown = items.status === "success" ? items.data.items : [];
  const states = [
    { value: null, label: t("all"), count: totalCount(loop.items) },
    ...ITEM_STATES.map((value) => ({ value, label: state(value), icon: STATE_LOOKS[value].icon, count: countOf(loop.items, value) })),
  ];
  const views: { value: LoopView; label: string; icon: typeof Rows3; testId: string }[] = [
    { value: "state", label: t("byState"), icon: Rows3, testId: "loop-view-state" },
    { value: "chain", label: t("byChain"), icon: ListTree, testId: "loop-view-chain" },
  ];
  return (
    <section aria-labelledby="loop-items-title" className="min-w-0">
      <h2 id="loop-items-title" className="sr-only">
        {t("heading")}
      </h2>
      <DataCard
        testId="loop-items"
        busy={items.status === "loading"}
        footer={items.status === "success" && items.data.more ? <p className="text-xs text-fg-subtle">{t("more", { count: LOOP_ITEMS_LIMIT })}</p> : null}
      >
        <DataToolbar label={t("search")} count={t("count", { count: shown.length })} countTestId="loop-items-count">
          <ToolbarFilters active={active} onClear={() => set({ ...filters, state: null, kind: null, goal: null })} summary={t("count", { count: shown.length })}>
            <FacetGroup
              label={t("stateLabel")}
              options={states}
              selected={filters.state}
              onSelect={(value) => set({ ...filters, state: value as LoopFilters["state"] })}
              countLabel={(count) => t("count", { count })}
              testId="loop-states"
            />
            <ToolbarField label={t("kindLabel")}>
              {(id) => (
                <NativeSelect
                  id={id}
                  value={filters.kind ?? ""}
                  onChange={(event) => set({ ...filters, kind: (event.target.value || null) as LoopFilters["kind"] })}
                  className="md:[&_select]:h-7 md:[&_select]:text-xs"
                  data-testid="loop-kind"
                >
                  <NativeSelectOption value="">{t("allKinds")}</NativeSelectOption>
                  {ITEM_KINDS.map((value) => (
                    <NativeSelectOption key={value} value={value}>
                      {kind(value)}
                    </NativeSelectOption>
                  ))}
                </NativeSelect>
              )}
            </ToolbarField>
            {goals && goals.length > 0 ? (
              <ToolbarField label={t("goalLabel")}>
                {(id) => (
                  <NativeSelect
                    id={id}
                    value={filters.goal ?? ""}
                    onChange={(event) => set({ ...filters, goal: event.target.value || null })}
                    className="md:[&_select]:h-7 md:[&_select]:text-xs"
                    data-testid="loop-goal"
                  >
                    <NativeSelectOption value="">{t("allGoals")}</NativeSelectOption>
                    {goals.map((goal) => (
                      <NativeSelectOption key={goal.code} value={goal.code}>
                        {goal.code}
                      </NativeSelectOption>
                    ))}
                  </NativeSelect>
                )}
              </ToolbarField>
            ) : null}
          </ToolbarFilters>
          <Segmented label={t("viewLabel")} value={filters.view} options={views} onChange={(view) => set({ ...filters, view })} />
        </DataToolbar>
        <QueryView state={items} loading={<TableSkeleton rows={6} />}>
          {(found) =>
            filters.view === "chain" ? (
              <ChainList project={project} items={found.items} />
            ) : (
              <ItemsTable project={project} items={byState(found.items)} caption={t("caption", { project })} />
            )
          }
        </QueryView>
      </DataCard>
    </section>
  );
}

function LoopBody({ project, loop, admin, reader, itemsError }: { project: string; loop: ProjectLoop; admin: boolean; reader: boolean; itemsError: ApiErrorInfo | null }) {
  const t = useTranslations("loop.page");
  const total = totalCount(loop.items);
  if (!loop.enabled && total === 0) {
    return (
      <EmptyState icon={IterationCw} title={t("emptyTitle")} description={t("emptyText")} testId="loop-empty">
        {admin ? <LoopSwitch project={project} loop={loop} /> : null}
        <Button asChild variant="secondary">
          <Link href={goalsHref(project)} data-testid="loop-empty-goals">
            <Goal aria-hidden="true" />
            {t("goals")}
          </Link>
        </Button>
      </EmptyState>
    );
  }
  return (
    <>
      <BreakerBanner project={project} loop={loop} admin={admin} />
      {!loop.enabled ? (
        <Banner icon={CircleOff} title={t("offTitle")} testId="loop-off">
          {t("offText")}
        </Banner>
      ) : null}
      {reader ? (
        <Banner icon={Eye} title={t("readOnlyTitle")} testId="loop-read-only">
          {t("readOnlyText")}
        </Banner>
      ) : null}
      <Strip loop={loop} />
      <ItemsCard project={project} loop={loop} itemsError={itemsError} />
    </>
  );
}

/**
 * A project's Loop, for any member with a grant on it, kept current while it is open: whether it is on and its breaker,
 * its work items by state, then the work items themselves by state or by chain, each with why it waits. An admin turns
 * it on or off and resets its breaker; everyone else reads.
 */
export function LoopPage({ project, initialError, itemsError }: { project: string; initialError: ApiErrorInfo | null; itemsError: ApiErrorInfo | null }) {
  const t = useTranslations("loop.page");
  const viewer = useLoopViewer(project);
  const state = useHubQuery({ ...loopQuery(browserApi, project), refetchInterval: LOOP_REFRESH_MS }, initialError, { live: true });
  const loop = state.status === "success" ? state.data : null;
  const admin = viewer?.admin ?? false;
  return (
    <div className="flex flex-col gap-6" data-testid="loop-page" data-admin={admin ? "true" : "false"}>
      <PageHeader
        title={t("title")}
        status={loop ? <LoopStatePill loop={loop} /> : null}
        actions={loop && admin && (loop.enabled || totalCount(loop.items) > 0) ? <LoopSwitch project={project} loop={loop} /> : null}
        testId="loop-header"
      />
      <QueryView state={state} loading={<ListSkeleton rows={6} metrics={5} />}>
        {(found) => <LoopBody project={project} loop={found} admin={admin} reader={viewer !== null && !admin} itemsError={itemsError} />}
      </QueryView>
    </div>
  );
}
