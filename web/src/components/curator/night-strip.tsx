"use client";

import { Activity, CircleDollarSign, Lightbulb, MoonStar, PencilLine, ScanSearch } from "lucide-react";
import type { Route } from "next";
import { useTranslations } from "next-intl";
import Link from "next/link";

import { MetricStrip } from "@/components/data/metric-strip";
import { EmptyState } from "@/components/states/states";
import { Button } from "@/components/ui/button";

import { useCuratorRights } from "./hooks";
import { budgetShare, proposalSearch, NO_PROPOSAL_FILTERS } from "./model";
import { useDay, useMoney } from "./parts";
import { type CuratorStatus, curatorHref } from "./queries";

/** The CLI command that writes a charter, for whoever reads the empty state without the admin role. */
const CHARTER_COMMAND = "evo-agents hub curator charter set";

/** The project's proposals page with `filters` applied: from a figure, a night or the last review. */
export function proposalsHref(project: string, filters: Partial<typeof NO_PROPOSAL_FILTERS>): Route {
  return `${curatorHref(project, "proposals")}${proposalSearch({ ...NO_PROPOSAL_FILTERS, ...filters })}` as Route;
}

/** Tonight (or the last night, outside the window): its runs and cost against the charter's caps, what waits, the last review. */
export function NightStrip({ project, status }: { project: string; status: CuratorStatus }) {
  const t = useTranslations("curator.metrics");
  const money = useMoney();
  const day = useDay();
  const night = status.night;
  const last = status.last_review_run;
  if (!night) return null;
  const which = night.in_window ? t("tonight") : t("lastNight", { night: day(night.night) });
  return (
    <MetricStrip
      label={t("label")}
      testId="curator-metrics"
      metrics={[
        {
          id: "runs",
          label: night.in_window ? t("runsTonight") : t("runsLast"),
          icon: Activity,
          live: night.active_run_id !== null,
          // Running only while a run of the night shift is in flight; the runs that ended are a plain count.
          tone: night.active_run_id !== null ? "running" : undefined,
          value: night.runs,
          meta:
            night.active_run_id !== null
              ? t("runsActive", { id: night.active_run_id })
              : night.in_window
                ? t("runsMeta", { max: night.max_runs })
                : t("runsMetaLast", { max: night.max_runs, which }),
        },
        {
          id: "cost",
          label: t("cost"),
          icon: CircleDollarSign,
          value: night.cost_usd,
          display: money(night.cost_usd),
          meta: t("costMeta", { budget: money(night.budget_usd), share: Math.round(budgetShare(night.cost_usd, night.budget_usd) * 100) }),
        },
        {
          id: "waiting",
          label: t("waiting"),
          icon: Lightbulb,
          tone: "attention",
          value: status.open_proposals,
          href: proposalsHref(project, { state: "open" }),
          meta: status.open_proposals > 0 ? t("waitingMeta") : t("waitingNone"),
        },
        {
          id: "review",
          label: t("review"),
          icon: ScanSearch,
          value: last?.proposals ?? 0,
          extra: last ? t("reviewExtra", { count: last.proposals }) : null,
          href: last ? proposalsHref(project, { run: last.id }) : undefined,
          meta: last ? t("reviewMeta", { id: last.id, findings: last.findings, night: day(last.night) }) : t("reviewNone"),
        },
      ]}
    />
  );
}

/** A project without a charter: what the Curator does, and the way to give it one. */
export function NoCharter({ project }: { project: string }) {
  const t = useTranslations("curator.empty");
  const rights = useCuratorRights(project, null);
  return (
    <EmptyState
      icon={MoonStar}
      title={t("title")}
      description={
        <>
          <p>{t("description")}</p>
          {rights.charter ? null : (
            <p className="mt-2">
              {t.rich("reader", { command: () => <code className="rounded-xs bg-muted px-1 font-mono text-xs">{CHARTER_COMMAND}</code> })}
            </p>
          )}
        </>
      }
    >
      {rights.charter ? (
        <Button asChild>
          <Link href={`${curatorHref(project, "charter")}?edit=1` as Route} data-testid="curator-write-charter">
            <PencilLine aria-hidden="true" />
            {t("admin")}
          </Link>
        </Button>
      ) : null}
    </EmptyState>
  );
}
