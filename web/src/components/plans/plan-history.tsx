"use client";

import { ArrowRight, GitCompareArrows, History } from "lucide-react";
import Form from "next/form";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ApiErrorState, LoadingState, PageSkeleton, TableSkeleton } from "@/components/states/states";
import { StatusBadge } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { planDiffQuery, planQuery, planRevisionsQuery } from "@/lib/plan-queries";
import { comparedPair, type Plan, type PlanRevision, planState, previousRevision } from "@/lib/plans";
import { cn } from "@/lib/utils";

import { revisionsHref } from "./links";
import { DiffView } from "./plan-diff";
import { PlanHeader, ReadOnlyNotice } from "./plan-header";
import { PlanNotFound } from "./plan-not-found";
import { VERBATIM_MONO } from "./prose";

/** The history shows this many revisions before "show all"; a busy plan has hundreds. */
const HISTORY_LIMIT = 20;

type Pair = { from: number; to: number };

function When({ iso }: { iso: string }) {
  const format = useFormatter();
  return (
    <time dateTime={iso} className="tabular-nums">
      {format.dateTime(new Date(iso), { dateStyle: "medium", timeStyle: "short" })}
    </time>
  );
}

function CompareForm({ project, planId, revisions, pair }: { project: string; planId: string; revisions: PlanRevision[]; pair: Pair }) {
  const t = useTranslations("plans.history");
  const fromId = useId();
  const toId = useId();
  const select = "h-10 w-full rounded-md border border-input bg-background px-2.5 font-mono text-sm";
  const options = revisions.map((revision) => (
    <option key={revision.revision} value={revision.revision}>
      {t("option", { revision: revision.revision, login: revision.actor })}
    </option>
  ));
  return (
    <Form
      action={revisionsHref(project, planId)}
      className="flex flex-col gap-3 rounded-md border bg-card shadow-raised p-4 sm:flex-row sm:items-end"
      aria-label={t("compareLabel")}
      data-testid="compare-form"
    >
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <label htmlFor={fromId} className="text-sm font-medium">
          {t("from")}
        </label>
        <select id={fromId} name="from" defaultValue={pair.from} className={select} key={`from-${pair.from}`}>
          {options}
        </select>
      </div>
      <ArrowRight className="hidden size-4 shrink-0 self-center text-muted-foreground sm:mt-6 sm:block" aria-hidden="true" />
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <label htmlFor={toId} className="text-sm font-medium">
          {t("to")}
        </label>
        <select id={toId} name="to" defaultValue={pair.to} className={select} key={`to-${pair.to}`}>
          {options}
        </select>
      </div>
      <Button type="submit" size="lg" data-testid="compare-submit">
        <GitCompareArrows aria-hidden="true" />
        {t("compare")}
      </Button>
    </Form>
  );
}

function RevisionMeta({ revision, label }: { revision: PlanRevision; label: string }) {
  const t = useTranslations("plans.history");
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1 rounded-md border bg-card px-3 py-2.5">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="font-mono text-sm font-medium">{t("revision", { revision: revision.revision })}</span>
      <span className="text-xs text-muted-foreground">
        <span className="font-mono text-foreground">{revision.actor}</span>, <When iso={revision.created_at} />
      </span>
      <span className={cn(VERBATIM_MONO, "text-xs [overflow-wrap:anywhere]")}>{revision.summary}</span>
    </div>
  );
}

function Comparison({ project, planId, revisions, pair }: { project: string; planId: string; revisions: PlanRevision[]; pair: Pair }) {
  const t = useTranslations("plans.history");
  const same = pair.from === pair.to;
  const state = useHubQuery({ ...planDiffQuery(browserApi, project, planId, pair.from, pair.to), enabled: !same });
  return (
    <section aria-labelledby="plan-diff-title" className="flex min-w-0 flex-col gap-3">
      <h2 id="plan-diff-title" className="text-base font-medium" data-testid="diff-title">
        {same ? t("compareTitle") : t("diffTitle", { from: pair.from, to: pair.to })}
      </h2>
      {revisions.length < 2 ? (
        <p className="rounded-md border border-dashed bg-card px-4 py-6 text-center text-sm text-muted-foreground">
          {t("onlyOne")}
        </p>
      ) : same ? (
        <p className="rounded-md border border-dashed bg-card px-4 py-6 text-center text-sm text-muted-foreground" data-testid="diff-same">
          {t("sameRevision")}
        </p>
      ) : (
        <QueryView state={state} loading={<TableSkeleton rows={6} />}>
          {(diff) => (
            <>
              <div className="flex flex-col gap-2 sm:flex-row">
                <RevisionMeta revision={diff.from_revision} label={t("fromLabel")} />
                <RevisionMeta revision={diff.to_revision} label={t("toLabel")} />
              </div>
              <DiffView diff={diff} />
            </>
          )}
        </QueryView>
      )}
    </section>
  );
}

function HistoryList({ project, planId, revisions, pair }: { project: string; planId: string; revisions: PlanRevision[]; pair: Pair }) {
  const t = useTranslations("plans.history");
  const [all, setAll] = useState(false);
  const listId = useId();
  const newest = [...revisions].sort((a, b) => b.revision - a.revision);
  const shown = all ? newest : newest.slice(0, HISTORY_LIMIT);
  return (
    <section aria-labelledby="plan-history-title" className="flex min-w-0 flex-col gap-3">
      <h2 id="plan-history-title" className="flex items-center gap-2 text-base font-medium">
        <History className="size-4 text-muted-foreground" aria-hidden="true" />
        {t("title")}
        <span className="font-mono text-xs text-muted-foreground tabular-nums">{revisions.length}</span>
      </h2>
      <ol id={listId} className="flex flex-col gap-2" data-testid="revision-list">
        {shown.map((revision, index) => {
          const previous = previousRevision(revisions, revision.revision);
          const current = previous !== null && pair.from === previous && pair.to === revision.revision;
          const older = newest[index + 1];
          const moved = older !== undefined && older.area !== revision.area;
          return (
            <li
              key={revision.revision}
              className={cn(
                "flex flex-col gap-1 rounded-md border bg-card px-3 py-2.5",
                current ? "border-brand/50 bg-surface-selected" : null,
              )}
              aria-current={current ? "true" : undefined}
              data-testid="revision-item"
              data-revision={revision.revision}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-mono text-sm font-medium">{t("revision", { revision: revision.revision })}</span>
                {moved ? <StatusBadge kind="plan" status={planState(revision.area, null)} /> : null}
              </div>
              <p className={cn(VERBATIM_MONO, "text-xs [overflow-wrap:anywhere]")} data-testid="revision-summary">
                {revision.summary}
              </p>
              <p className="text-xs text-muted-foreground">
                {t.rich("by", {
                  login: revision.actor,
                  who: (chunks) => <span className="font-mono text-foreground">{chunks}</span>,
                })}{" "}
                <When iso={revision.created_at} />
              </p>
              {previous !== null ? (
                current ? (
                  <span className="text-xs font-medium text-accent-foreground">{t("comparing")}</span>
                ) : (
                  <Link
                    href={revisionsHref(project, planId, { from: previous, to: revision.revision })}
                    className="w-fit rounded text-xs font-medium text-brand underline-offset-4 hover:underline"
                    data-testid="revision-compare"
                  >
                    {t("compareWithPrevious", { previous, revision: revision.revision })}
                  </Link>
                )
              ) : (
                <span className="text-xs text-muted-foreground">{t("first")}</span>
              )}
            </li>
          );
        })}
      </ol>
      {newest.length > HISTORY_LIMIT ? (
        <Button variant="outline" aria-expanded={all} aria-controls={listId} onClick={() => setAll((value) => !value)}>
          {all ? t("showFewer") : t("showAll", { count: newest.length })}
        </Button>
      ) : null}
    </section>
  );
}

function HistoryPage({
  project,
  plan,
  revisions,
  from,
  to,
}: {
  project: string;
  plan: Plan;
  revisions: PlanRevision[];
  from: number | null;
  to: number | null;
}) {
  const pair = comparedPair(revisions, from, to);
  return (
    <>
      <PlanHeader project={project} plan={plan} current="revisions" />
      <ReadOnlyNotice />
      {pair ? (
        <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_20rem]">
          <div className="flex min-w-0 flex-col gap-4">
            {revisions.length > 1 ? (
              <CompareForm project={project} planId={plan.plan_id} revisions={revisions} pair={pair} />
            ) : null}
            <Comparison project={project} planId={plan.plan_id} revisions={revisions} pair={pair} />
          </div>
          <HistoryList project={project} planId={plan.plan_id} revisions={revisions} pair={pair} />
        </div>
      ) : null}
    </>
  );
}

export function PlanHistory({
  project,
  planId,
  from,
  to,
  initialErrors,
  invalid = false,
}: {
  project: string;
  planId: string;
  from: number | null;
  to: number | null;
  initialErrors: { plan: ApiErrorInfo | null; revisions: ApiErrorInfo | null };
  invalid?: boolean;
}) {
  const plan = useHubQuery({ ...planQuery(browserApi, project, planId), enabled: !invalid }, initialErrors.plan);
  const revisions = useHubQuery(
    { ...planRevisionsQuery(browserApi, project, planId), enabled: !invalid },
    initialErrors.revisions,
  );
  const failed = plan.status === "error" ? plan : revisions.status === "error" ? revisions : null;
  if (invalid || failed?.error.status === 404) return <PlanNotFound project={project} planId={planId} />;
  if (failed) return <ApiErrorState error={failed.error} onRetry={failed.retry} />;
  if (plan.status !== "success" || revisions.status !== "success") {
    return (
      <LoadingState>
        <PageSkeleton />
      </LoadingState>
    );
  }
  return <HistoryPage project={project} plan={plan.data} revisions={revisions.data} from={from} to={to} />;
}
