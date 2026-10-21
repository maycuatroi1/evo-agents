"use client";

import { Clock, Eye, GitMerge, Lock, SlidersHorizontal } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useMemo } from "react";

import { DataCard, DataToolbar } from "@/components/data/data-card";
import { FacetGroup } from "@/components/data/facet-group";
import { ToolbarField, ToolbarFilters } from "@/components/data/filter-sheet";
import { MetricStrip } from "@/components/data/metric-strip";
import { Banner } from "@/components/feedback/banner";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { MERGE_QUEUE_REFRESH_MS, mergeQueueHref, mergeQueueQuery, type MergeQueueView } from "@/lib/merge-queue";

import { BreakersPanel, TrippedBanners } from "./breakers";
import { MergeQueueHeader, useMergeViewer } from "./chrome";
import {
  countByGroup,
  filterRequests,
  groupOf,
  type QueueFilters,
  queueRepos,
  queueSearch,
  readQueueFilters,
  requestName,
  STATE_GROUPS,
  type StateGroup,
  trippedBreakers,
  waitingSamples,
} from "./model";
import { QueueTable } from "./queue-table";
import { RequestSheet } from "./request-sheet";
import { SamplesPanel } from "./samples";

/** The page's filters, kept in its URL, and the link of a merge request opened over the list. */
function useQueueFilters() {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const filters = readQueueFilters(params);
  const hrefFor = useCallback(
    (next: QueueFilters): Route => {
      const search = queueSearch(next);
      return (search ? `${pathname}?${search}` : pathname) as Route;
    },
    [pathname],
  );
  const set = useCallback((next: QueueFilters) => router.replace(hrefFor(next), { scroll: false }), [router, hrefFor]);
  return { filters, set, hrefFor };
}

function Strip({ view }: { view: MergeQueueView }) {
  const t = useTranslations("mergeQueue.metrics");
  const counts = countByGroup(view.requests);
  const turn = view.requests.filter((request) => groupOf(request.state) === "turn");
  return (
    <MetricStrip
      label={t("label")}
      testId="merge-queue-summary"
      metrics={[
        { id: "waiting", label: t("waiting"), icon: Clock, value: counts.waiting, meta: t("waitingMeta") },
        {
          id: "turn",
          label: t("turn"),
          icon: GitMerge,
          value: counts.turn,
          tone: "running",
          live: true,
          meta: turn.length > 0 ? turn.map((request) => `${requestName(request)} ${request.repo}`).join(", ") : t("turnNone"),
        },
        { id: "blocked", label: t("blocked"), icon: Lock, value: counts.blocked, tone: "attention", meta: t("blockedMeta") },
        { id: "after", label: t("after"), icon: Eye, value: counts.after, meta: t("afterMeta") },
      ]}
    />
  );
}

function QueueBody({ project, view, admin, reader }: { project: string; view: MergeQueueView; admin: boolean; reader: boolean }) {
  const t = useTranslations("mergeQueue");
  const { filters, set, hrefFor } = useQueueFilters();
  const counts = countByGroup(view.requests);
  const repos = queueRepos(view.requests, view.breakers);
  const shown = useMemo(() => filterRequests(view.requests, filters), [view.requests, filters]);
  const hrefOf = useCallback((id: number) => hrefFor({ ...filters, open: id }), [hrefFor, filters]);
  const open = filters.open === null ? null : (view.requests.find((request) => request.id === filters.open) ?? null);
  const active = (filters.group ? 1 : 0) + (filters.repo ? 1 : 0);

  if (view.requests.length === 0 && view.breakers.length === 0 && view.samples.length === 0) {
    return (
      <EmptyState icon={GitMerge} title={t("empty.title")} description={t("empty.description")}>
        <Button asChild variant="secondary">
          <Link href={mergeQueueHref(project, "policy")} data-testid="merge-queue-empty-policy">
            <SlidersHorizontal aria-hidden="true" />
            {t("empty.policy")}
          </Link>
        </Button>
      </EmptyState>
    );
  }
  const groups = [
    { value: null, label: t("groups.all"), count: view.requests.length },
    ...STATE_GROUPS.map((group: StateGroup) => ({ value: group, label: t(`groups.${group}`), count: counts[group] })),
  ];
  return (
    <>
      <TrippedBanners project={project} breakers={trippedBreakers(view.breakers)} admin={admin} />
      {reader ? (
        <Banner icon={Eye} title={t("readOnly.title")} testId="merge-queue-read-only">
          {t("readOnly.text")}
        </Banner>
      ) : null}
      <Strip view={view} />
      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <section aria-labelledby="merge-queue-list-title" className="min-w-0">
          <h2 id="merge-queue-list-title" className="sr-only">
            {t("table.caption")}
          </h2>
          <DataCard testId="merge-queue-list">
            <DataToolbar label={t("table.search")} count={t("table.count", { count: shown.length })} countTestId="merge-queue-count">
              <ToolbarFilters active={active} onClear={() => set({ group: null, repo: null, open: null })} summary={t("table.count", { count: shown.length })}>
                <FacetGroup
                  label={t("groups.label")}
                  options={groups}
                  selected={filters.group}
                  onSelect={(value) => set({ ...filters, group: value as StateGroup | null, open: null })}
                  countLabel={(count) => t("table.count", { count })}
                  testId="merge-queue-groups"
                />
                {repos.length > 1 ? (
                  <ToolbarField label={t("table.repo")}>
                    {(id) => (
                      <NativeSelect
                        id={id}
                        value={filters.repo ?? ""}
                        onChange={(event) => set({ ...filters, repo: event.target.value || null, open: null })}
                        className="md:[&_select]:h-7 md:[&_select]:text-xs"
                        data-testid="merge-queue-repo"
                      >
                        <NativeSelectOption value="">{t("table.allRepos")}</NativeSelectOption>
                        {repos.map((repo) => (
                          <NativeSelectOption key={repo} value={repo}>
                            {repo}
                          </NativeSelectOption>
                        ))}
                      </NativeSelect>
                    )}
                  </ToolbarField>
                ) : null}
              </ToolbarFilters>
            </DataToolbar>
            <QueueTable requests={shown} hrefOf={hrefOf} empty={t("table.none")} />
          </DataCard>
        </section>
        <div className="flex min-w-0 flex-col gap-4">
          <BreakersPanel project={project} breakers={view.breakers} admin={admin} />
          <SamplesPanel project={project} samples={view.samples} admin={admin} />
        </div>
      </div>
      <RequestSheet
        project={project}
        request={open}
        missing={filters.open !== null && open === null ? filters.open : null}
        admin={admin}
        onClose={() => set({ ...filters, open: null })}
        hrefOf={hrefOf}
      />
    </>
  );
}

/**
 * A project's merge queue on GitLab, for any member with a grant on it, kept current while it is open: the breakers
 * that stopped a repo's merges first, the counts of where the merge requests stand, the merge requests themselves
 * (filtered by where they stand and by repo, each opening over the list with its ledger), the breaker of each repo and
 * the audit samples. An admin of the project resets a breaker, answers a sample, approves a PROD release and asks for
 * a revert; a reader reads.
 */
export function MergeQueuePage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const viewer = useMergeViewer(project);
  const state = useHubQuery({ ...mergeQueueQuery(browserApi, project), refetchInterval: MERGE_QUEUE_REFRESH_MS }, initialError, { live: true });
  const view = state.status === "success" ? state.data : null;
  const waiting = view ? trippedBreakers(view.breakers).length + waitingSamples(view.samples).length : 0;
  const admin = viewer?.admin ?? false;
  return (
    <div className="flex flex-col gap-6" data-testid="merge-queue-page" data-admin={admin ? "true" : "false"}>
      <MergeQueueHeader project={project} current="queue" waiting={waiting} />
      <QueryView state={state} loading={<ListSkeleton rows={6} metrics={4} />}>
        {(found) => <QueueBody project={project} view={found} admin={admin} reader={viewer !== null && !admin} />}
      </QueryView>
    </div>
  );
}
