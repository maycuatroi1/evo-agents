"use client";

import Link from "next/link";
import type { Route } from "next";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import type { QueuedMergeRequest } from "@/lib/merge-queue";
import { cn } from "@/lib/utils";

import { MergeStateBadge, SourceTag } from "./badges";
import { groupOf, requestName } from "./model";

const NARROW_HIDDEN = {
  repo: "hidden md:table-cell",
  source: "hidden lg:table-cell",
  updated: "hidden sm:table-cell",
};

/** The second line of a row: why it waits or is blocked, or where it goes. */
function lineOf(request: QueuedMergeRequest, into: string): { text: string; danger: boolean } {
  if (request.reason) return { text: request.reason, danger: groupOf(request.state) === "blocked" || request.state === "reverting" || request.state === "rolling_back" };
  return { text: into, danger: false };
}

/**
 * The merge requests of the queue, newest first: the title with its `!iid` and why it waits (in `danger` when it is
 * blocked or undone), the repo and target branch, where it comes from, its state and when it last moved. A title opens
 * the merge request in the sheet over the list; on a phone each row is one tap to it.
 */
export function QueueTable({ requests, hrefOf, empty }: { requests: QueuedMergeRequest[]; hrefOf: (id: number) => Route; empty: string }) {
  const t = useTranslations("mergeQueue.table");
  const columns = useMemo(() => {
    const helper = dataTableColumns<QueuedMergeRequest>();
    return helper.columns([
      helper.accessor((row) => row.title ?? requestName(row), {
        id: "title",
        header: () => t("request"),
        sortFn: "text",
        meta: { primary: true },
        cell: (info) => {
          const request = info.row.original;
          const line = lineOf(request, t("into", { repo: request.repo, branch: request.target_branch }));
          return (
            <CellMain sub={line.text} subTitle={line.text} danger={line.danger} subTestId="merge-row-reason">
              <span className="shrink-0 font-mono text-xs text-muted-foreground">{requestName(request)}</span>
              <Link href={hrefOf(request.id)} scroll={false} className={cn(NAME_LINK, "truncate")} title={request.title ?? undefined} data-testid="merge-row-open">
                {request.title ?? requestName(request)}
              </Link>
            </CellMain>
          );
        },
      }),
      helper.accessor("repo", {
        header: () => t("repo"),
        sortFn: "alphanumeric",
        cell: (info) => (
          <span className="flex min-w-0 flex-col gap-px">
            <span className="truncate font-mono text-[13px] text-foreground">{info.getValue()}</span>
            <span className="truncate font-mono text-xs text-fg-subtle">{info.row.original.target_branch}</span>
          </span>
        ),
      }),
      helper.accessor("source", {
        header: () => t("source"),
        sortFn: "text",
        cell: (info) => (
          <span className="flex min-w-0 flex-col items-start gap-1">
            <SourceTag source={info.getValue()} />
            {info.row.original.profile ? <span className="truncate font-mono text-xs text-fg-subtle">{info.row.original.profile}</span> : null}
          </span>
        ),
      }),
      helper.accessor("state", {
        header: () => t("state"),
        sortFn: "text",
        cell: (info) => <MergeStateBadge state={info.getValue()} />,
      }),
      helper.accessor((row) => new Date(row.updated_at), {
        id: "updated",
        header: () => t("updated"),
        sortFn: "datetime",
        meta: { numeric: true },
        cell: (info) => <When value={info.row.original.updated_at} short />,
      }),
    ]);
  }, [t, hrefOf]);

  return (
    <DataTable
      data={requests}
      columns={columns}
      caption={t("caption")}
      getRowId={(row) => String(row.id)}
      columnClassNames={NARROW_HIDDEN}
      empty={empty}
      testId="merge-queue-table"
      mobile={(request) => {
        const line = lineOf(request, t("into", { repo: request.repo, branch: request.target_branch }));
        return {
          title: `${requestName(request)} ${request.title ?? ""}`.trim(),
          titleText: request.title ?? requestName(request),
          href: hrefOf(request.id),
          status: <MergeStateBadge state={request.state} />,
          meta: line.text,
          metaText: line.text,
          danger: line.danger,
          data: { "merge-id": request.id },
        };
      }}
    />
  );
}
