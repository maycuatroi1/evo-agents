"use client";

import { useTranslations } from "next-intl";
import { useCallback, useMemo } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";

import { StateCell } from "./account-cells";
import { AccountCell, type Actions, ActionButtons, CostCell, HeldByCell, NARROW_HIDDEN, RunsCell } from "./account-columns";
import { isClaudeAccount } from "./model";
import type { Account } from "./queries";
import { UsageLimits } from "./usage-limits";

/**
 * The model accounts: the runtime's own sign-in. Each row has its state (a pill only when something is wrong), the
 * projects and workers it serves, its runs, what they cost ("API value" for a Claude subscription), the runs holding
 * it, and its usage limits as bars under the row.
 */
export function ModelTable({ accounts, shared, now, onEdit, onPause, onRenew }: { accounts: Account[]; shared: Set<string>; now: number } & Actions) {
  const t = useTranslations("accounts");
  const columns = useMemo(() => {
    const helper = dataTableColumns<Account>();
    return helper.columns([
      helper.accessor("name", {
        header: () => t("columns.account"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => <AccountCell account={info.row.original} ranked={shared.has(info.row.original.env_var)} />,
      }),
      helper.accessor("state", { header: () => t("columns.state"), sortFn: "text", cell: (info) => <StateCell account={info.row.original} now={now} /> }),
      helper.display({
        id: "projects",
        header: () => t("columns.projects"),
        cell: (info) => <span className="font-mono text-xs">{info.row.original.projects.join(", ") || "-"}</span>,
      }),
      helper.display({
        id: "workers",
        header: () => t("columns.workers"),
        cell: (info) => {
          const workers = info.row.original.workers;
          return workers.length ? <span className="font-mono text-xs">{workers.join(", ")}</span> : <span className="text-fg-subtle">{t("anyWorker")}</span>;
        },
      }),
      helper.accessor("runs_7d", {
        id: "runs",
        header: () => t("columns.runs"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => <RunsCell day={info.row.original.runs_24h} week={info.row.original.runs_7d} />,
      }),
      helper.accessor((account) => account.cost_7d_usd ?? 0, {
        id: "cost",
        header: () => t("columns.cost"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => <CostCell account={info.row.original} />,
      }),
      helper.display({
        id: "heldBy",
        header: () => t("columns.heldBy"),
        cell: (info) => <HeldByCell held={info.row.original.held_by} />,
      }),
      helper.display({
        id: "actions",
        header: () => <span className="sr-only">{t("columns.actions")}</span>,
        meta: { actions: true },
        cell: (info) => <ActionButtons account={info.row.original} onEdit={onEdit} onPause={onPause} onRenew={onRenew} />,
      }),
    ]);
  }, [t, shared, now, onEdit, onPause, onRenew]);
  // A Claude account's usage limits, or any account's a run reported, under its row at every width.
  const usage = useCallback(
    (account: Account) => (account.last_usage || isClaudeAccount(account) ? <UsageLimits account={account} pageNow={now} /> : null),
    [now],
  );
  return (
    <DataTable
      data={accounts}
      columns={columns}
      caption={t("groups.model.title")}
      getRowId={(row) => row.name}
      initialSorting={[{ id: "name", desc: false }]}
      columnClassNames={NARROW_HIDDEN}
      detail={usage}
      testId="accounts-table"
    />
  );
}
