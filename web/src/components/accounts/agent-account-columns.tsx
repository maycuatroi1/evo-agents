"use client";

import { Pause, Pencil, Play, RefreshCw, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { dataTableColumns } from "@/components/data/data-table";
import { Button } from "@/components/ui/button";

import { StateCell } from "./account-cells";
import { AccountCell, CostCell, HeldByCell, RunsCell } from "./account-columns";
import { preloadAgentAccountDialog } from "./agent-account-lazy";
import type { AgentAccount } from "./agent-queries";
import { isClaudeAccount } from "./model";
import { UsageLimits } from "./usage-limits";

/**
 * The columns an agent's account shows on the agent's card and in the Agent accounts group of the Accounts page, with
 * the Accounts page's cells: its provider and variable, its state (a pill only when something is wrong), the workers
 * it is bound to, its runs, what they cost (a model account's only), the runs holding it; its usage under the row.
 */
type Translate = ReturnType<typeof useTranslations<"accounts">>;

export function agentFigureColumns<T extends AgentAccount>(t: Translate, now: number, shared: Set<string>) {
  const helper = dataTableColumns<T>();
  return helper.columns([
    helper.accessor((account) => account.name, {
      id: "name",
      header: () => t("columns.account"),
      sortFn: "alphanumeric",
      meta: { primary: true },
      cell: (info) => <AccountCell account={info.row.original} ranked={shared.has(info.row.original.env_var)} />,
    }),
    helper.accessor((account) => account.state, {
      id: "state",
      header: () => t("columns.state"),
      sortFn: "text",
      cell: (info) => <StateCell account={info.row.original} now={now} />,
    }),
    helper.display({
      id: "workers",
      header: () => t("columns.workers"),
      cell: (info) => <span className="font-mono text-xs" data-testid="agent-account-bound">{info.row.original.workers.join(", ")}</span>,
    }),
    helper.accessor((account) => account.runs_7d, {
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
      cell: (info) =>
        info.row.original.category === "model" ? <CostCell account={info.row.original} /> : <span className="text-fg-subtle">-</span>,
    }),
    helper.display({ id: "heldBy", header: () => t("columns.heldBy"), cell: (info) => <HeldByCell held={info.row.original.held_by} /> }),
  ]);
}

/** The variables two or more of `accounts` set: those rows say their weight and priority. */
export function sharedAgentVariables(accounts: readonly AgentAccount[]): Set<string> {
  const seen = new Map<string, number>();
  for (const account of accounts) seen.set(account.env_var, (seen.get(account.env_var) ?? 0) + 1);
  return new Set([...seen].filter(([, count]) => count > 1).map(([variable]) => variable));
}

/** A model account's usage limits, or any account's a run reported, under its row. */
export function agentUsage(account: AgentAccount, now: number) {
  return account.last_usage || (account.category === "model" && isClaudeAccount(account)) ? <UsageLimits account={account} pageNow={now} /> : null;
}

export type AgentActions = {
  onRenew: (account: AgentAccount) => void;
  onEdit: (account: AgentAccount) => void;
  onPause: (account: AgentAccount) => void;
  onDelete: (account: AgentAccount) => void;
};

/** Renew, Edit, Pause or Resume, and Delete of one of an agent's accounts, for an admin of its project. */
export function AgentActionButtons({ account, onRenew, onEdit, onPause, onDelete }: { account: AgentAccount } & AgentActions) {
  const t = useTranslations("accounts");
  const paused = account.paused_at !== null;
  const button = (label: string, onClick: () => void, testId: string, preload = false) => ({
    type: "button" as const,
    variant: "ghost" as const,
    size: "sm" as const,
    "aria-label": label,
    onClick,
    onPointerEnter: preload ? preloadAgentAccountDialog : undefined,
    onFocus: preload ? preloadAgentAccountDialog : undefined,
    "data-testid": testId,
  });
  return (
    <>
      <Button {...button(t("agent.renewLabel", { name: account.name }), () => onRenew(account), "agent-account-renew", true)}>
        <RefreshCw aria-hidden="true" />
        <span className="hidden sm:inline">{t("agent.renew")}</span>
      </Button>
      <Button {...button(t("agent.editLabel", { name: account.name }), () => onEdit(account), "agent-account-edit", true)}>
        <Pencil aria-hidden="true" />
        <span className="hidden sm:inline">{t("edit")}</span>
      </Button>
      <Button {...button(t(paused ? "resumeLabel" : "pauseLabel", { name: account.name }), () => onPause(account), paused ? "agent-account-resume" : "agent-account-pause")}>
        {paused ? <Play aria-hidden="true" /> : <Pause aria-hidden="true" />}
        <span className="hidden sm:inline">{t(paused ? "resume" : "pause")}</span>
      </Button>
      <Button {...button(t("agent.deleteLabel", { name: account.name }), () => onDelete(account), "agent-account-delete")} variant="quiet-danger">
        <Trash2 aria-hidden="true" />
        <span className="hidden sm:inline">{t("agent.delete")}</span>
      </Button>
    </>
  );
}
