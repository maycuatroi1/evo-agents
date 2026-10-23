"use client";

import { Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";

import { DataCard } from "@/components/data/data-card";
import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { ConfirmAction } from "@/components/feedback/confirm-action";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";

import { NARROW_HIDDEN } from "./account-columns";
import { AgentActionButtons, type AgentActions, agentFigureColumns, agentUsage, sharedAgentVariables } from "./agent-account-columns";
import type { AgentDialogKind } from "./agent-account-dialog";
import { LazyAgentAccountDialog, preloadAgentAccountDialog } from "./agent-account-lazy";
import { useAgentAccountFailure, useChangeAgentAccount, useDeleteAgentAccount, useProjectAdmin } from "./agent-hooks";
import { type AgentAccount, agentAccountsQuery, MAX_ACCOUNTS_PER_AGENT } from "./agent-queries";

type Dialog = { kind: AgentDialogKind; account: AgentAccount | null; open: boolean };

function AccountsTable({ accounts, admin, now, onRenew, onEdit, onPause, onDelete }: { accounts: AgentAccount[]; admin: boolean; now: number } & AgentActions) {
  const t = useTranslations("accounts");
  const shared = useMemo(() => sharedAgentVariables(accounts), [accounts]);
  const columns = useMemo(() => {
    const figures = agentFigureColumns<AgentAccount>(t, now, shared);
    if (!admin) return figures;
    const helper = dataTableColumns<AgentAccount>();
    return helper.columns([
      ...figures,
      helper.display({
        id: "actions",
        header: () => <span className="sr-only">{t("columns.actions")}</span>,
        meta: { actions: true },
        cell: (info) => <AgentActionButtons account={info.row.original} onRenew={onRenew} onEdit={onEdit} onPause={onPause} onDelete={onDelete} />,
      }),
    ]);
  }, [t, now, shared, admin, onRenew, onEdit, onPause, onDelete]);
  return (
    <DataTable
      data={accounts}
      columns={columns}
      caption={t("agent.title")}
      getRowId={(row) => row.name}
      initialSorting={[{ id: "name", desc: false }]}
      columnClassNames={NARROW_HIDDEN}
      detail={(account) => agentUsage(account, now)}
      testId="agent-accounts-table"
    />
  );
}

/**
 * The Accounts card of an agent's page (docs/credentials.md, section "Accounts of an agent"): the env secrets the
 * agent owns, which its runs take on the workers each is bound to, with the figures of the Accounts page. An admin of
 * the project adds, renews, edits, pauses, resumes and deletes them here; every other member reads them. The route
 * shows it for a live agent that is not the project's default, which takes none.
 */
export function AgentAccountsCard({ project, handle }: { project: string; handle: string }) {
  const t = useTranslations("accounts");
  const admin = useProjectAdmin(project);
  const state = useHubQuery(agentAccountsQuery(browserApi, project, handle));
  const accounts = state.status === "success" ? state.data : null;
  const [now] = useState(() => Date.now());
  // Null until a dialog first opens: it mounts, and loads, only then.
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [deleting, setDeleting] = useState<{ account: AgentAccount; open: boolean } | null>(null);
  const change = useChangeAgentAccount(project, handle);
  const remove = useDeleteAgentAccount(project, handle);
  const failureOf = useAgentAccountFailure();
  const { mutateAsync: changeAsync } = change;

  const onRenew = useCallback((account: AgentAccount) => setDialog({ kind: "renew", account, open: true }), []);
  const onEdit = useCallback((account: AgentAccount) => setDialog({ kind: "edit", account, open: true }), []);
  const onDelete = useCallback(
    (account: AgentAccount) => {
      remove.reset();
      setDeleting({ account, open: true });
    },
    [remove],
  );
  const onPause = useCallback(
    async (account: AgentAccount) => {
      const paused = account.paused_at === null;
      const name = account.name;
      try {
        await changeAsync({ name, body: { paused } });
      } catch (error) {
        notifyFailure(t(paused ? "pauseFailed" : "resumeFailed", { name }), failureOf(error));
        return;
      }
      notify({ tone: "success", text: t(paused ? "paused" : "resumed", { name }), description: t(paused ? "pausedText" : "resumedText") });
    },
    [changeAsync, failureOf, t],
  );
  const pause = useCallback((account: AgentAccount) => void onPause(account), [onPause]);
  const confirmDelete = async (account: AgentAccount) => {
    try {
      await remove.mutateAsync(account.name);
    } catch {
      return; // shown in the confirmation, which stays open
    }
    setDeleting(null);
    notify({ tone: "success", text: t("agent.deleted", { name: account.name }), description: t("agent.deletedText", { handle }) });
  };
  const full = (accounts?.length ?? 0) >= MAX_ACCOUNTS_PER_AGENT;

  return (
    <Card data-testid="agent-accounts">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <h2>{t("agent.title")}</h2>
          {accounts ? (
            <Badge variant="secondary" data-testid="agent-accounts-count">
              {accounts.length}
            </Badge>
          ) : null}
        </CardTitle>
        <CardDescription className="text-pretty">{t(admin ? "agent.description" : "agent.readOnly", { handle, project })}</CardDescription>
        {admin ? (
          <CardAction>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setDialog({ kind: "add", account: null, open: true })}
              onPointerEnter={preloadAgentAccountDialog}
              onFocus={preloadAgentAccountDialog}
              disabled={full}
              title={full ? t("agent.full", { max: MAX_ACCOUNTS_PER_AGENT }) : undefined}
              data-testid="agent-account-add"
            >
              <Plus aria-hidden="true" />
              {t("agent.add")}
            </Button>
          </CardAction>
        ) : null}
      </CardHeader>
      <CardContent>
        <QueryView state={state} loading={<TableSkeleton rows={2} />}>
          {(list) =>
            list.length === 0 ? (
              <p className="rounded-md border border-dashed px-3 py-4 text-sm text-pretty text-fg-subtle" data-testid="agent-accounts-empty">
                {t(admin ? "agent.emptyAdmin" : "agent.empty", { handle })}
              </p>
            ) : (
              <DataCard>
                <AccountsTable accounts={list} admin={admin} now={now} onRenew={onRenew} onEdit={onEdit} onPause={pause} onDelete={onDelete} />
              </DataCard>
            )
          }
        </QueryView>
      </CardContent>
      {admin && dialog ? (
        <LazyAgentAccountDialog
          project={project}
          handle={handle}
          kind={dialog.kind}
          account={dialog.account}
          taken={(accounts ?? []).map((account) => account.name)}
          open={dialog.open}
          onOpenChange={(open) => setDialog((current) => (current ? { ...current, open } : current))}
        />
      ) : null}
      {admin && deleting ? (
        <ConfirmAction
          open={deleting.open}
          onOpenChange={(open) => (open ? undefined : setDeleting(null))}
          title={t("agent.deleteTitle", { name: deleting.account.name })}
          description={<p>{t("agent.deleteText", { name: deleting.account.name, variable: deleting.account.env_var, handle })}</p>}
          confirmLabel={t("agent.deleteConfirm")}
          pendingLabel={t("agent.deleting")}
          cancelLabel={t("agent.form.cancel")}
          pending={remove.isPending}
          error={remove.isError ? failureOf(remove.error) : null}
          onConfirm={() => void confirmDelete(deleting.account)}
          testId="agent-account-delete-dialog"
        />
      ) : null}
    </Card>
  );
}
