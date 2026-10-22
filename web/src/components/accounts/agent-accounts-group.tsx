"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { TableSkeleton } from "@/components/states/states";
import { agentHref } from "@/lib/agent-queries";
import { browserApi } from "@/lib/api/browser";

import { NARROW_HIDDEN } from "./account-columns";
import { AccountGroup } from "./account-group";
import { agentFigureColumns, agentUsage } from "./agent-account-columns";
import { allAgentAccountsQuery, type ListedAgentAccount } from "./agent-queries";

/** The agent that owns a row: its avatar, its @handle as the link to its page, and its project. */
function AgentCell({ account }: { account: ListedAgentAccount }) {
  const t = useTranslations("accounts.groups.agent");
  const agent = { name: account.agent_name, handle: account.handle, color: account.color, icon_url: account.icon_url };
  return (
    <div className="flex min-w-0 items-center gap-2.5">
      <AgentAvatar agent={agent} size="sm" />
      <CellMain sub={account.project} subTitle={t("projectTitle", { project: account.project })} subTestId="agent-account-project">
        <Link
          href={agentHref(account.project, account.handle)}
          className="truncate font-mono text-[13px] underline-offset-4 hover:underline"
          aria-label={t("open", { handle: account.handle, project: account.project })}
          data-testid="agent-account-agent"
        >
          @{account.handle}
        </Link>
      </CellMain>
    </div>
  );
}

function AgentTable({ accounts, now }: { accounts: ListedAgentAccount[]; now: number }) {
  const t = useTranslations("accounts");
  const columns = useMemo(() => {
    const helper = dataTableColumns<ListedAgentAccount>();
    return helper.columns([
      helper.accessor((account) => `${account.project}/${account.handle}`, {
        id: "agent",
        header: () => t("groups.agent.agent"),
        sortFn: "alphanumeric",
        cell: (info) => <AgentCell account={info.row.original} />,
      }),
      ...agentFigureColumns<ListedAgentAccount>(t, now, new Set()),
    ]);
  }, [t, now]);
  return (
    <DataTable
      data={accounts}
      columns={columns}
      caption={t("groups.agent.title")}
      getRowId={(row) => `${row.project}/${row.handle}/${row.name}`}
      initialSorting={[{ id: "agent", desc: false }]}
      columnClassNames={NARROW_HIDDEN}
      detail={(account) => agentUsage(account, now)}
      testId="agent-accounts-group-table"
    />
  );
}

/**
 * The third group of the Accounts page: the accounts of the agents of every project the visitor holds a grant on,
 * each row with its agent's avatar, @handle and project and the way to the agent's page, where an admin changes it.
 * With `onlyIfAny`, nothing shows until there is one, for a visitor who has no account of their own.
 */
export function AgentAccountsGroup({ now, onlyIfAny = false }: { now: number; onlyIfAny?: boolean }) {
  const t = useTranslations("accounts.groups.agent");
  const query = useQuery(allAgentAccountsQuery(browserApi));
  if (query.isPending) return onlyIfAny ? null : <TableSkeleton rows={2} />;
  if (query.isError) {
    return (
      <p className="text-sm text-danger" role="alert" data-testid="accounts-group-agent-error">
        {t("loadFailed")}
      </p>
    );
  }
  if (onlyIfAny && query.data.length === 0) return null;
  return (
    <AccountGroup id="agent" count={query.data.length}>
      <AgentTable accounts={query.data} now={now} />
    </AccountGroup>
  );
}
