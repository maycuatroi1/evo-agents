"use client";

import { Boxes, Plus } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton } from "@/components/states/states";
import { STATUS_LOOKS, StatusPill } from "@/components/status/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

import { ChipList } from "./badges";
import { NOT_READY_LOOK } from "./pool-badges";
import { PoolDialog } from "./pool-dialog";
import { type PoolHealth, poolHealth } from "./pool-model";
import { type Pool, poolHref, poolsQuery } from "./queries";

const NARROW_HIDDEN = {
  projects: "hidden md:table-cell",
  slots: "hidden sm:table-cell",
  taints: "hidden lg:table-cell",
};

/**
 * What about a pool's nodes needs a person: those the doctor holds back and those offline. Nothing shows while every
 * node takes runs (web/DESIGN.md, States).
 */
export function PoolTrouble({ health }: { health: PoolHealth }) {
  const t = useTranslations("workers.pools");
  if (health.notReady === 0 && health.offline === 0) return null;
  return (
    <span className="flex flex-wrap items-center gap-1.5">
      {health.notReady > 0 ? (
        <StatusPill look={NOT_READY_LOOK} label={t("notReadyCount", { count: health.notReady })} data-testid="pool-not-ready" />
      ) : null}
      {health.offline > 0 ? (
        <StatusPill look={STATUS_LOOKS.worker.offline} label={t("offlineCount", { count: health.offline })} data-testid="pool-offline" />
      ) : null}
    </span>
  );
}

function PoolsTable({ pools }: { pools: Pool[] }) {
  const t = useTranslations("workers.pools");
  const columns = useMemo(() => {
    const helper = dataTableColumns<Pool>();
    return helper.columns([
      helper.accessor("name", {
        header: () => t("columns.pool"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => {
          const pool = info.row.original;
          const sub = t("ownerLine", { owner: pool.owner });
          return (
            <CellMain sub={sub} subTitle={sub}>
              <Link href={poolHref(pool.name)} className={cn(NAME_LINK, "truncate font-mono text-[13px]")} title={pool.name} data-pool-name={pool.name}>
                {pool.name}
              </Link>
            </CellMain>
          );
        },
      }),
      helper.accessor((row) => row.nodes.length, {
        id: "nodes",
        header: () => t("columns.nodes"),
        sortFn: "basic",
        cell: (info) => {
          const health = poolHealth(info.row.original);
          return (
            <span className="flex flex-wrap items-center gap-1.5">
              <span className="font-mono text-xs tabular-nums">{t("nodesCount", { count: health.nodes })}</span>
              <PoolTrouble health={health} />
            </span>
          );
        },
      }),
      helper.display({
        id: "projects",
        header: () => t("columns.projects"),
        cell: (info) => <ChipList items={info.row.original.projects} empty="-" compact />,
      }),
      helper.accessor("slots", {
        header: () => t("columns.slots"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => <span className="font-mono text-xs tabular-nums">{info.getValue()}</span>,
      }),
      helper.display({
        id: "taints",
        header: () => t("columns.taints"),
        cell: (info) => <ChipList items={info.row.original.taints} empty="-" compact />,
      }),
    ]);
  }, [t]);

  return (
    <DataTable
      data={pools}
      columns={columns}
      caption={t("caption")}
      getRowId={(row) => String(row.id)}
      columnClassNames={NARROW_HIDDEN}
      testId="pools-table"
      mobile={(pool) => {
        const meta = t("mobileMeta", { nodes: pool.nodes.length, slots: pool.slots });
        return {
          title: pool.name,
          titleText: pool.name,
          titleClassName: "font-mono text-[13px]",
          href: poolHref(pool.name),
          status: <PoolTrouble health={poolHealth(pool)} />,
          meta,
          metaText: meta,
          data: { "pool-name": pool.name },
        };
      }}
    />
  );
}

/**
 * The visitor's worker pools: a pool gives every node that joins it the same projects, slots, labels and taints, and
 * a join token lets a fresh machine join it with one command. A pool's row shows only what needs a person.
 */
export function PoolsPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("workers.pools");
  const state = useHubQuery(poolsQuery(browserApi), initialError, { live: true });
  const [creating, setCreating] = useState(false);

  return (
    <>
      <PageHeader
        title={t("title")}
        sub={t("description")}
        tags={state.status === "success" ? <Badge variant="secondary">{t("count", { count: state.data.length })}</Badge> : null}
        actions={
          <Button onClick={() => setCreating(true)} data-testid="pools-create">
            <Plus aria-hidden="true" />
            {t("createButton")}
          </Button>
        }
      />
      <QueryView state={state} loading={<ListSkeleton />}>
        {(pools) =>
          pools.length === 0 ? (
            <EmptyState icon={Boxes} title={t("empty.title")} description={t("empty.description")}>
              <Button onClick={() => setCreating(true)} data-testid="pools-empty-create">
                <Plus aria-hidden="true" />
                {t("createButton")}
              </Button>
            </EmptyState>
          ) : (
            <section aria-labelledby="pools-list-title">
              <h2 id="pools-list-title" className="sr-only">
                {t("caption")}
              </h2>
              <DataCard>
                <PoolsTable pools={pools} />
              </DataCard>
            </section>
          )
        }
      </QueryView>
      <PoolDialog open={creating} onOpenChange={setCreating} />
    </>
  );
}
