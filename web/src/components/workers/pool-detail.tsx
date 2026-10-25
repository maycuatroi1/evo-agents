"use client";

import { KeyRound, ShieldOff, WifiOff } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { NAME_LINK } from "@/components/data/identifier";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton } from "@/components/states/states";
import { StatusBadge } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

import { Ago } from "./ago";
import { ChipList } from "./badges";
import { JoinTokenDialog } from "./join-token-dialog";
import { Note } from "./note";
import { NotReadyBadge } from "./pool-badges";
import { nodeProblem, poolHealth } from "./pool-model";
import { PoolSettings } from "./pool-settings";
import { PoolTokens } from "./pool-tokens";
import { type Pool, type PoolNode, poolQuery, workerHref } from "./queries";
import { SectionCard } from "./section-card";

/** One pool of the visitor's: its nodes and whether each takes runs, its join tokens, and its settings. */
export function PoolDetail({ name, initialError }: { name: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("workers.pools.detail");
  const state = useHubQuery(poolQuery(browserApi, name), initialError, { live: true });
  if (state.status === "error" && state.error.status === 404) {
    return <NotFoundState title={t("notFoundTitle")} description={t("notFoundDescription")} />;
  }
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(pool) => <PoolPage pool={pool} />}
    </QueryView>
  );
}

function PoolPage({ pool }: { pool: Pool }) {
  const t = useTranslations("workers.pools.detail");
  const format = useFormatter();
  const [joining, setJoining] = useState(false);
  const health = poolHealth(pool);

  return (
    <div className="flex flex-col gap-6" data-testid="pool-detail" data-pool-name={pool.name}>
      <PageHeader
        title={<span className="font-mono">{pool.name}</span>}
        sub={t("facts", { owner: pool.owner, date: format.dateTime(new Date(pool.created_at), { dateStyle: "medium" }) })}
        actions={
          <Button onClick={() => setJoining(true)} data-testid="pool-add-node">
            <KeyRound aria-hidden="true" />
            {t("addNode")}
          </Button>
        }
      />
      {health.notReady > 0 ? (
        <Note tone="warning" icon={ShieldOff} testId="pool-not-ready-note">
          <p>{t("notReadyNote", { count: health.notReady })}</p>
        </Note>
      ) : null}
      {health.offline > 0 ? (
        <Note tone="warning" icon={WifiOff} testId="pool-offline-note">
          <p>{t("offlineNote", { count: health.offline, days: pool.reap_after_days })}</p>
        </Note>
      ) : null}
      <div className="grid gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Nodes pool={pool} onAdd={() => setJoining(true)} />
        <Scope pool={pool} />
      </div>
      <PoolTokens pool={pool.name} onCreate={() => setJoining(true)} />
      <PoolSettings pool={pool} />
      <JoinTokenDialog pool={pool.name} open={joining} onOpenChange={setJoining} />
    </div>
  );
}

function NodeRow({ node }: { node: PoolNode }) {
  const t = useTranslations("workers.pools.detail");
  const problem = nodeProblem(node);
  return (
    <li className="flex min-w-0 flex-col gap-1 py-3 first:pt-0 last:pb-0" data-testid="pool-node" data-node-name={node.name}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5">
        <Link href={workerHref(node.id)} className={cn(NAME_LINK, "min-w-0 truncate font-mono text-[13px]")} title={node.name}>
          {node.name}
        </Link>
        {problem === "offline" || problem === "draining" ? <StatusBadge kind="worker" status={problem} /> : null}
        <NotReadyBadge ready={node.ready || problem === "offline"} reason={node.ready_reason} />
        <span className="ml-auto text-xs text-muted-foreground">
          <Ago value={node.last_heartbeat_at} never={t("never")} />
        </span>
      </div>
      <p className="text-xs text-muted-foreground [overflow-wrap:anywhere]">
        {t("nodeLine", { hostname: node.hostname, version: node.agent_version })}
      </p>
      {!node.ready && node.ready_reason ? (
        <p className="text-xs text-attention [overflow-wrap:anywhere]" data-testid="pool-node-reason">
          {node.ready_reason}
        </p>
      ) : null}
    </li>
  );
}

function Nodes({ pool, onAdd }: { pool: Pool; onAdd: () => void }) {
  const t = useTranslations("workers.pools.detail");
  return (
    <SectionCard title={t("nodes")} count={pool.nodes.length} description={t("nodesDescription")} testId="pool-nodes">
      {pool.nodes.length === 0 ? (
        <div className="flex flex-col items-start gap-3 rounded-md border border-dashed px-4 py-4 text-sm text-muted-foreground">
          <p>{t("noNodes")}</p>
          <Button variant="outline" size="sm" onClick={onAdd} data-testid="pool-nodes-add">
            <KeyRound aria-hidden="true" />
            {t("addNode")}
          </Button>
        </div>
      ) : (
        <ul className="flex flex-col divide-y">
          {pool.nodes.map((node) => (
            <NodeRow key={node.id} node={node} />
          ))}
        </ul>
      )}
    </SectionCard>
  );
}

function Scope({ pool }: { pool: Pool }) {
  const t = useTranslations("workers.pools.detail");
  const rows: [string, ReactNode][] = [
    [t("projects"), <ChipList key="projects" items={pool.projects} empty="-" />],
    [t("slots"), <span key="slots" className="font-mono tabular-nums">{pool.slots}</span>],
    [t("labels"), <ChipList key="labels" items={[...pool.labels, `pool=${pool.name}`]} empty="-" />],
    [t("taints"), <ChipList key="taints" items={pool.taints} empty={t("noTaints")} />],
    [t("accepted"), <ChipList key="accepted" items={pool.accepted_findings} empty={t("noAccepted")} />],
    [t("reap"), <span key="reap">{t("reapDays", { days: pool.reap_after_days })}</span>],
  ];
  return (
    <SectionCard title={t("scope")} description={t("scopeDescription")} testId="pool-scope">
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2.5 text-sm">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="min-w-0">{value}</dd>
          </div>
        ))}
      </dl>
    </SectionCard>
  );
}
