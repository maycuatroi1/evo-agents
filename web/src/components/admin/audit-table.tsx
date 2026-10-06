"use client";

import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useCallback, useMemo } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { projectHref } from "@/components/shell/nav";

import { actionKey, type AuditRow, memberHref } from "./data";

const NARROW_HIDDEN = { id: "hidden md:table-cell", project: "hidden sm:table-cell", token: "hidden lg:table-cell" };

type ActionMessage = Parameters<ReturnType<typeof useTranslations<"admin.audit.actions">>>[0];

/** How people read an action, or null for one this web has no words for (a later release's). */
export function useActionName(): (action: string) => string | null {
  const t = useTranslations("admin.audit.actions");
  return useCallback(
    (action: string) => {
      const key = actionKey(action) as ActionMessage; // checked by t.has before use
      return t.has(key) ? t(key) : null;
    },
    [t],
  );
}

/** An action as people read it, with its stable name beside it. */
export function ActionLabel({ action }: { action: string }) {
  const name = useActionName()(action);
  return (
    <span className="flex flex-col gap-0.5">
      {name ? <span className="font-medium">{name}</span> : null}
      <code className="font-mono text-xs text-muted-foreground">{action}</code>
    </span>
  );
}

/** Audit rows in the server's order, newest first, with seconds: rows a moment apart must read apart. */
export function AuditTable({ rows, caption, testId = "audit-table" }: { rows: AuditRow[]; caption: string; testId?: string }) {
  const t = useTranslations("admin.audit");
  const format = useFormatter();
  const columns = useMemo(() => {
    const helper = dataTableColumns<AuditRow>();
    return helper.columns([
      helper.accessor("id", {
        header: () => t("columns.id"),
        enableSorting: false,
        cell: (info) => (
          <span className="font-mono text-xs text-muted-foreground tabular-nums" data-audit-id={info.getValue()}>
            #{info.getValue()}
          </span>
        ),
      }),
      helper.accessor("at", {
        header: () => t("columns.at"),
        enableSorting: false,
        cell: (info) => (
          <time dateTime={info.getValue()} className="whitespace-nowrap tabular-nums">
            {format.dateTime(new Date(info.getValue()), { dateStyle: "medium", timeStyle: "medium" })}
          </time>
        ),
      }),
      helper.accessor("actor", {
        header: () => t("columns.actor"),
        enableSorting: false,
        cell: (info) => {
          const actor = info.getValue();
          if (!actor) return <span className="text-muted-foreground italic">{t("hub")}</span>;
          return (
            <Link href={memberHref(actor)} className="font-medium text-brand underline-offset-4 hover:underline" data-actor={actor}>
              {actor}
            </Link>
          );
        },
      }),
      helper.accessor("action", {
        header: () => t("columns.action"),
        enableSorting: false,
        cell: (info) => <ActionLabel action={info.getValue()} />,
      }),
      helper.accessor("project", {
        header: () => t("columns.project"),
        enableSorting: false,
        cell: (info) => {
          const project = info.getValue();
          if (!project) return <span className="text-muted-foreground">{t("noProject")}</span>;
          return (
            <Link href={projectHref(project)} className="font-mono text-xs text-brand underline-offset-4 hover:underline">
              {project}
            </Link>
          );
        },
      }),
      helper.accessor("target", {
        header: () => t("columns.target"),
        enableSorting: false,
        cell: (info) => <code className="inline-block min-w-[16ch] font-mono text-xs break-all whitespace-normal">{info.getValue()}</code>,
      }),
      helper.accessor("token_id", {
        id: "token",
        header: () => t("columns.token"),
        enableSorting: false,
        cell: (info) => {
          const id = info.getValue();
          return id ? <span className="font-mono text-xs text-muted-foreground tabular-nums">#{id}</span> : null;
        },
      }),
    ]);
  }, [t, format]);
  return (
    <DataTable
      data={rows}
      columns={columns}
      caption={caption}
      getRowId={(row) => String(row.id)}
      columnClassNames={NARROW_HIDDEN}
      testId={testId}
    />
  );
}
