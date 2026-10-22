"use client";

import { useQuery } from "@tanstack/react-query";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { DeletedBadge, MemoryStatusBadge, MemoryTypeBadge } from "./badges";
import { memoryStatus, memoryTitle } from "./memory-meta";
import { memoriesHref, memoryHref } from "./memory-table";
import { ProvenanceLine } from "./provenance";
import { latestRevisionQuery, type Memory, memoryListQuery } from "./queries";

const NARROW_HIDDEN = { read: "hidden lg:table-cell", revision: "hidden md:table-cell" };

/** The memory's latest revision and where it came from: its number, the run, the person at its origin and the hop. */
function LatestRevision({ project, memory }: { project: string; memory: Memory }) {
  const t = useTranslations("memories.agent");
  const state = useQuery(latestRevisionQuery(browserApi, memory.id, memory.revision));
  return (
    <div className="flex min-w-0 flex-col gap-0.5" data-testid="agent-memory-revision" data-revision={memory.revision}>
      <span className="text-[13px] whitespace-nowrap text-foreground">
        {t("revision", { revision: memory.revision })} <span className="text-fg-subtle">{t("by", { login: memory.updated_by })}</span>
      </span>
      {state.data?.provenance ? (
        <ProvenanceLine project={project} provenance={state.data.provenance} />
      ) : state.isPending ? (
        <Skeleton className="h-3 w-32" />
      ) : (
        <span className="text-xs text-fg-subtle">{t("noRun")}</span>
      )}
    </div>
  );
}

function AgentMemoryTable({ project, handle, rows }: { project: string; handle: string; rows: Memory[] }) {
  const t = useTranslations("memories.agent");
  const tColumns = useTranslations("memories.columns");
  const columns = useMemo(() => {
    const helper = dataTableColumns<Memory>();
    return helper.columns([
      helper.accessor((row) => memoryTitle(row.name, row.body).title, {
        id: "name",
        header: () => tColumns("name"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => {
          const row = info.row.original;
          const { title, description } = memoryTitle(row.name, row.body);
          return (
            <CellMain sub={description ?? <span className="font-mono">{row.name}</span>} subTitle={description ?? row.name}>
              <Link href={memoryHref({ kind: "project", project }, row.id)} className={cn(NAME_LINK, "truncate")} title={title} data-memory-name={row.name}>
                {title}
              </Link>
            </CellMain>
          );
        },
      }),
      helper.accessor("type", {
        header: () => tColumns("type"),
        sortFn: "text",
        cell: (info) => <MemoryTypeBadge type={info.getValue()} />,
      }),
      helper.accessor((row) => memoryStatus(row), {
        id: "status",
        header: () => t("status"),
        sortFn: "text",
        cell: (info) => (info.row.original.deleted ? <DeletedBadge /> : <MemoryStatusBadge status={info.getValue()} />),
      }),
      helper.accessor((row) => (row.read_at ? new Date(row.read_at) : new Date(0)), {
        id: "read",
        header: () => t("read"),
        sortFn: "datetime",
        cell: (info) => <When value={info.row.original.read_at} never={t("neverRead")} short />,
      }),
      helper.accessor((row) => new Date(row.updated_at), {
        id: "revision",
        header: () => t("latest"),
        sortFn: "datetime",
        cell: (info) => <LatestRevision project={project} memory={info.row.original} />,
      }),
    ]);
  }, [t, tColumns, project]);
  return (
    <DataTable
      data={rows}
      columns={columns}
      caption={t("caption", { handle })}
      getRowId={(row) => String(row.id)}
      initialSorting={[{ id: "revision", desc: true }]}
      columnClassNames={NARROW_HIDDEN}
      testId="agent-memories-table"
      mobile={(row) => {
        const { title, description } = memoryTitle(row.name, row.body);
        return {
          title,
          href: memoryHref({ kind: "project", project }, row.id),
          status: row.deleted ? <DeletedBadge /> : <MemoryStatusBadge status={memoryStatus(row)} />,
          meta: description ?? <span className="font-mono">{row.name}</span>,
          metaText: description ?? row.name,
          data: { "memory-name": row.name },
        };
      }}
    />
  );
}

/**
 * The memories of agent `handle` of `project` (web/DESIGN.md, Agents): each with its type, where it stands (active,
 * stale, archived or promoted by the Curator), when the agent last opened it and its latest revision with the run that
 * wrote it, the person at that run's origin and its hop; each a link to the memory's page. The agent's page receives
 * it as a slot, so the agents feature does not import this one. A link under the table opens the project's memories
 * filtered by the agent.
 */
export function AgentMemories({ project, handle }: { project: string; handle: string }) {
  const t = useTranslations("memories.agent");
  const state = useQuery(memoryListQuery(browserApi, { kind: "project", project }, handle));
  if (state.isPending) return <Skeleton className="h-24 w-full" />;
  if (state.isError) {
    return (
      <p className="text-sm text-danger" role="alert" data-testid="agent-memories-failed">
        {t("failed")}
      </p>
    );
  }
  const rows = state.data.items;
  return (
    <div className="flex flex-col gap-2" data-testid="agent-memories">
      {rows.length === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid="agent-memories-empty">
          {t("empty", { handle })}
        </p>
      ) : (
        <AgentMemoryTable project={project} handle={handle} rows={rows} />
      )}
      <p className="text-xs text-fg-subtle">
        {t.rich("scope", {
          link: (chunks) => (
            <Link
              href={`${memoriesHref({ kind: "project", project })}?agent=${encodeURIComponent(handle)}` as Route}
              className="text-foreground underline decoration-border underline-offset-2 hover:text-brand"
              data-testid="agent-memories-all"
            >
              {chunks}
            </Link>
          ),
        })}
      </p>
    </div>
  );
}
