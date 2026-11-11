"use client";

import { Gauge, Inbox, Info, OctagonPause } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Banner } from "@/components/feedback/banner";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { opsAlert, opsBatchesQuery, type SystemOps, systemOpsQuery } from "@/lib/system-ops-queries";
import type { SystemOut } from "@/lib/systems";

import { SetDefaultButton, useSetDefault } from "./default-system";
import { OpsBatches } from "./ops-batches";
import { PauseOps, ResumeOps } from "./ops-pause";
import { OpsSettings } from "./ops-settings";

function Section({ title, sub, actions, children, testId }: { title: string; sub?: ReactNode; actions?: ReactNode; children: ReactNode; testId: string }) {
  return (
    <section className="flex flex-col gap-4 rounded-lg border bg-card p-4 sm:p-5" aria-label={title} data-testid={testId}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <h2 className="text-[15px] font-semibold text-balance">{title}</h2>
          {sub ? <div className="text-[13px] text-pretty text-muted-foreground">{sub}</div> : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

/** Paused or at the cap: why, since when, and what an admin can do. Nothing while the ops run as they should. */
function OpsAlert({ project, ops, admin }: { project: string; ops: SystemOps; admin: boolean }) {
  const t = useTranslations("systems.ops.alert");
  const format = useFormatter();
  const alert = opsAlert(ops);
  const usd = (value: number) => format.number(value, { style: "currency", currency: "USD" });
  if (alert === "paused") {
    const since = ops.paused_at ? format.dateTime(new Date(ops.paused_at), { dateStyle: "medium", timeStyle: "short" }) : null;
    return (
      <Banner
        icon={OctagonPause}
        tone="attention"
        title={t("pausedTitle")}
        actions={admin ? <ResumeOps project={project} slug={ops.slug} /> : null}
        testId="ops-paused"
      >
        {[ops.paused_reason, since ? t("since", { since }) : null].filter(Boolean).join(". ")}
        <span className="block">{admin ? t("pausedAdmin") : t("pausedReader")}</span>
      </Banner>
    );
  }
  if (alert === "capped") {
    return (
      <Banner icon={Gauge} tone="attention" title={t("cappedTitle")} testId="ops-capped">
        {t("cappedText", { spent: usd(ops.spent_usd), cap: usd(ops.ops_daily_usd) })}
      </Banner>
    );
  }
  return null;
}

/** A system that is not the project's default: its ops settings are kept but the hub queues nothing for it. */
function NotDefault({ project, system }: { project: string; system: SystemOut }) {
  const t = useTranslations("systems.ops");
  const setDefault = useSetDefault(project);
  const { mutate } = setDefault;
  return (
    <Banner
      icon={Info}
      title={t("notDefaultTitle")}
      actions={
        system.can_change ? <SetDefaultButton name={system.name} busy={setDefault.isPending} onClick={() => mutate(system.slug)} /> : null
      }
      testId="ops-not-default"
    >
      {t("notDefaultText")}
    </Banner>
  );
}

/** What the queue holds now, by state, and what the ops threads spent of their cap in the last 24 hours. */
function OpsSummary({ ops }: { ops: SystemOps }) {
  const t = useTranslations("systems.ops.summary");
  const format = useFormatter();
  const usd = (value: number) => format.number(value, { style: "currency", currency: "USD" });
  const waiting = (ops.batches.open ?? 0) + (ops.batches.ready ?? 0);
  const facts = [
    { id: "state", label: t("state"), value: ops.ops.enabled ? (ops.paused ? t("paused") : t("on")) : t("off") },
    { id: "spent", label: t("spent"), value: t("spentOf", { spent: usd(ops.spent_usd), cap: usd(ops.ops_daily_usd) }) },
    { id: "waiting", label: t("waiting"), value: format.number(waiting) },
    { id: "running", label: t("running"), value: format.number(ops.batches.running ?? 0) },
  ];
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4" data-testid="ops-summary">
      {facts.map((fact) => (
        <div key={fact.id} className="flex min-w-0 flex-col gap-0.5">
          <dt className="text-xs text-muted-foreground">{fact.label}</dt>
          <dd className="text-sm font-medium tabular-nums [overflow-wrap:anywhere]" data-testid={`ops-summary-${fact.id}`}>
            {fact.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function RecentBatches({ project, slug }: { project: string; slug: string }) {
  const t = useTranslations("systems.ops.batches");
  const state = useHubQuery(opsBatchesQuery(browserApi, project, slug));
  return (
    <Section title={t("title")} sub={t("sub")} testId="ops-recent">
      <QueryView state={state} loading={<TableSkeleton rows={3} />}>
        {(batches) =>
          batches.length ? (
            <OpsBatches batches={batches} />
          ) : (
            <EmptyState icon={Inbox} title={t("emptyTitle")} description={t("emptyText")} testId="ops-batches-empty" />
          )
        }
      </QueryView>
    </Section>
  );
}

/**
 * The Ops tab of a system (docs/hub.md, Agent systems): how the Orchestrator of the project's default system handles
 * the project's operations events. A banner says when it is paused or at its daily cap, or that this system is not
 * the default; under it what the queue holds and spent, the settings (an admin changes them, as the system's next
 * revision), and the latest batches with their threads.
 */
export function OpsPanel({ project, system }: { project: string; system: SystemOut }) {
  const t = useTranslations("systems.ops");
  const state = useHubQuery(systemOpsQuery(browserApi, project, system.slug));
  const admin = system.can_change;
  return (
    <div className="flex flex-col gap-4" data-testid="ops-panel">
      <QueryView state={state} loading={<Skeleton className="h-64 w-full rounded-lg" />}>
        {(ops) => (
          <>
            {ops.default ? <OpsAlert project={project} ops={ops} admin={admin} /> : <NotDefault project={project} system={system} />}
            <Section
              title={t("title")}
              sub={ops.default ? t("subDefault") : t("subOther")}
              actions={admin && ops.default && ops.ops.enabled && !ops.paused ? <PauseOps project={project} slug={system.slug} /> : null}
              testId="ops-section"
            >
              {ops.default ? <OpsSummary ops={ops} /> : null}
              <OpsSettings key={system.revision} project={project} system={system} ops={ops} canChange={admin} />
            </Section>
            {ops.default ? <RecentBatches project={project} slug={system.slug} /> : null}
          </>
        )}
      </QueryView>
    </div>
  );
}
