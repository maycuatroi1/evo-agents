"use client";

import type { Route } from "next";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo } from "react";

import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { projectHref } from "@/components/shell/nav";
import { cn } from "@/lib/utils";

import { DeletedBadge, LabelBadge, MemoryStatusBadge, MemoryTypeBadge } from "./badges";
import { memoryStatus, memoryTitle } from "./memory-meta";
import type { Memory, MemoryScope } from "./queries";

const NARROW_HIDDEN = {
  location: "hidden md:table-cell",
  updated: "hidden lg:table-cell",
  label: "hidden sm:table-cell",
  read: "hidden xl:table-cell",
};

export function memoryHref(scope: MemoryScope, id: number): Route {
  return scope.kind === "project" ? projectHref(scope.project, `memories/${id}`) : (`/memories/${id}` as Route);
}

export function memoriesHref(scope: MemoryScope): Route {
  return scope.kind === "project" ? projectHref(scope.project, "memories") : ("/memories" as Route);
}

export type MemoryRow = Memory & { rank?: number };

/**
 * A list of memories as a table: each a link to its page, with its type, location, label and last change. For an
 * agent's memories (`agent`), also where each stands (active, stale, archived or promoted) and when the agent last read it.
 */
export function MemoryTable({
  rows,
  scope,
  ranked,
  unrestricted,
  agent = null,
}: {
  rows: MemoryRow[];
  scope: MemoryScope;
  ranked: boolean;
  unrestricted?: string;
  agent?: string | null;
}) {
  const t = useTranslations("memories");
  const format = useFormatter();
  const columns = useMemo(() => {
    const helper = dataTableColumns<MemoryRow>();
    const agentColumns = agent
      ? [
          helper.accessor((row) => memoryStatus(row), {
            id: "status",
            header: () => t("columns.status"),
            sortFn: "text",
            cell: (info) => (info.row.original.deleted ? <DeletedBadge /> : <MemoryStatusBadge status={info.getValue()} />),
          }),
          helper.accessor((row) => (row.read_at ? new Date(row.read_at) : new Date(0)), {
            id: "read",
            header: () => t("columns.read"),
            sortFn: "datetime",
            cell: (info) => <When value={info.row.original.read_at} never={t("agent.neverRead")} short />,
          }),
        ]
      : [];
    return helper.columns([
      helper.accessor((row) => memoryTitle(row.name, row.body).title, {
        id: "name",
        header: () => t("columns.name"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => {
          const row = info.row.original;
          const { title, description } = memoryTitle(row.name, row.body);
          // One line under the title: what the memory is about, or its file name when the title is not the name.
          const sub = description ?? (title !== row.name ? <span className="font-mono">{row.name}</span> : null);
          return (
            <CellMain sub={sub} subTitle={description ?? row.name}>
              <Link
                href={memoryHref(scope, row.id)}
                className={cn(NAME_LINK, "truncate")}
                title={title !== row.name ? `${title} (${row.name})` : title}
                data-memory-name={row.name}
              >
                {title}
              </Link>
            </CellMain>
          );
        },
      }),
      helper.accessor("type", {
        header: () => t("columns.type"),
        sortFn: "text",
        cell: (info) => <MemoryTypeBadge type={info.getValue()} />,
      }),
      ...agentColumns,
      helper.accessor("location", {
        header: () => t("columns.location"),
        sortFn: "text",
        cell: (info) => <span className="font-mono text-xs text-foreground">{info.getValue()}</span>,
      }),
      helper.accessor((row) => String(row.label.level ?? ""), {
        id: "label",
        header: () => t("columns.label"),
        enableSorting: false,
        cell: (info) => <LabelBadge label={info.row.original.label} unrestricted={unrestricted} />,
      }),
      helper.accessor((row) => new Date(row.updated_at), {
        id: "updated",
        header: () => t("columns.updated"),
        sortFn: "datetime",
        meta: { numeric: true },
        cell: (info) => (
          <div className="flex flex-col items-end gap-px">
            <time dateTime={info.row.original.updated_at} className="whitespace-nowrap">
              {format.dateTime(info.getValue(), { dateStyle: "medium", timeStyle: "short" })}
            </time>
            <span className="text-xs leading-4 whitespace-nowrap text-fg-subtle">
              {t("by", { login: info.row.original.updated_by })}
            </span>
          </div>
        ),
      }),
    ]).filter((column) => scope.kind === "project" || column.id !== "label"); // a personal memory has no label
  }, [t, format, scope, unrestricted, agent]);

  return (
    <DataTable
      key={`${ranked ? "ranked" : "browse"}-${agent ?? ""}`}
      data={rows}
      columns={columns}
      caption={agent ? t("captionAgent", { handle: agent }) : ranked ? t("captionSearch") : t("caption")}
      getRowId={(row) => String(row.id)}
      initialSorting={ranked ? [] : [{ id: "updated", desc: true }]}
      columnClassNames={NARROW_HIDDEN}
      testId="memories-table"
      // On a phone: the memory's title opening it, its type (or, for an agent's, where it stands), and what it is about.
      mobile={(row) => {
        const { title, description } = memoryTitle(row.name, row.body);
        return {
          title,
          titleText: title !== row.name ? `${title} (${row.name})` : title,
          href: memoryHref(scope, row.id),
          status: agent ? <MemoryStatusBadge status={memoryStatus(row)} /> : <MemoryTypeBadge type={row.type} />,
          meta: description ?? <span className="font-mono">{row.name}</span>,
          metaText: description ?? row.name,
          data: { "memory-name": row.name },
        };
      }}
    />
  );
}
