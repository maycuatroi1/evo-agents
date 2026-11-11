"use client";

import { CircleCheck, CircleMinus, CirclePlay, Clock, Hourglass, MessageSquareReply, Repeat } from "lucide-react";
import Link from "next/link";
import type { Route } from "next";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { When } from "@/components/data/when";
import { type StatusLook, StatusPill } from "@/components/status/status-badge";
import { Badge } from "@/components/ui/badge";
import type { OpsBatch } from "@/lib/system-ops-queries";

/**
 * The state of a batch of the ops queue (`ops_queue.BATCH_STATES`): gathering, waiting for its thread, settled by the
 * hub alone, with the Orchestrator in its thread, done. Only the Ops tab shows one, so the map lives here.
 */
const BATCH_LOOKS = {
  open: { tone: "neutral", icon: Clock },
  ready: { tone: "neutral", icon: Hourglass },
  empty: { tone: "outline", icon: CircleMinus },
  running: { tone: "running", icon: CirclePlay, dot: "live" },
  done: { tone: "success", icon: CircleCheck },
} as const satisfies Record<OpsBatch["state"], StatusLook>;

function BatchState({ state }: { state: OpsBatch["state"] }) {
  const t = useTranslations("systems.ops.batchState");
  return <StatusPill look={BATCH_LOOKS[state]} label={t(state)} data-testid="ops-batch-state" data-status={state} />;
}

function ThreadLink({ batch }: { batch: OpsBatch }) {
  const t = useTranslations("systems.ops.batches");
  if (!batch.thread) return <span className="text-muted-foreground">{t("noThread")}</span>;
  return (
    <Link
      href={batch.thread as Route}
      className="inline-flex min-h-11 items-center gap-1.5 text-sm font-medium text-link underline-offset-4 hover:underline md:min-h-0"
      aria-label={t("threadFor", { id: batch.id })}
      data-testid="ops-batch-thread"
    >
      <MessageSquareReply className="size-4" aria-hidden="true" />
      {t("thread")}
    </Link>
  );
}

function Cause({ batch }: { batch: OpsBatch }) {
  const t = useTranslations("systems.ops.batches");
  return (
    <span className="flex min-w-0 items-center gap-2">
      <span className="truncate font-mono text-xs">{`${batch.kind} ${batch.cause}`}</span>
      {batch.recurring ? (
        <Badge variant="outline" className="shrink-0 gap-1 border-attention/40 text-attention" data-testid="ops-batch-recurring">
          <Repeat className="size-3" aria-hidden="true" />
          {t("recurring")}
        </Badge>
      ) : null}
    </span>
  );
}

const NARROW_HIDDEN = { items: "hidden md:table-cell", opened: "hidden lg:table-cell" };

/** The latest batches of the project's ops queue: the cause they gather, their state, items, when and their thread. */
export function OpsBatches({ batches }: { batches: OpsBatch[] }) {
  const t = useTranslations("systems.ops.batches");
  const columns = useMemo(() => {
    const helper = dataTableColumns<OpsBatch>();
    return helper.columns([
      helper.accessor("key", {
        header: () => t("columns.cause"),
        meta: { primary: true },
        cell: (info) => (
          <CellMain sub={info.row.original.subject} subTitle={info.row.original.subject}>
            <Cause batch={info.row.original} />
          </CellMain>
        ),
      }),
      helper.accessor("state", { header: () => t("columns.state"), cell: (info) => <BatchState state={info.getValue()} /> }),
      helper.display({
        id: "items",
        header: () => t("columns.items"),
        meta: { numeric: true },
        cell: (info) => (
          <span className="text-xs tabular-nums">{t("items", { items: info.row.original.items, pending: info.row.original.pending })}</span>
        ),
      }),
      helper.accessor((row) => new Date(row.opened_at), {
        id: "opened",
        header: () => t("columns.opened"),
        meta: { numeric: true },
        cell: (info) => <When value={info.row.original.opened_at} short />,
      }),
      helper.display({ id: "thread", header: () => t("columns.thread"), cell: (info) => <ThreadLink batch={info.row.original} /> }),
    ]);
  }, [t]);
  return (
    <DataTable
      data={batches}
      columns={columns}
      caption={t("caption")}
      getRowId={(row) => String(row.id)}
      columnClassNames={NARROW_HIDDEN}
      testId="ops-batches"
      mobile={(batch) => ({
        title: <Cause batch={batch} />,
        titleText: `${batch.kind} ${batch.cause}`,
        status: <BatchState state={batch.state} />,
        meta: <span className="font-mono">{batch.subject}</span>,
        metaText: batch.subject,
        actions: batch.thread ? <ThreadLink batch={batch} /> : null,
        data: { "batch-id": batch.id },
      })}
    />
  );
}
