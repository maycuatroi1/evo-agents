"use client";

import { useQuery } from "@tanstack/react-query";
import { CalendarClock, ChevronRight, Pause, Pencil, Play, RefreshCw, UserRoundCog, UserRoundPlus } from "lucide-react";
import type { Route } from "next";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { RunRef } from "@/components/data/identifier";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { Day, StateCell, UsageCell } from "./account-cells";
import { AccountDialog } from "./account-dialog";
import { useChangeAccount, useFailure } from "./hooks";
import { isClaudeAccount, policyRows, renewalDue } from "./model";
import { type Account, accountListQuery, policiesQuery } from "./queries";
import { PolicySection } from "./policy-section";

const SECRETS_HREF = "/secrets" as Route;

// The Add Claude account and Renew token dialog loads the first time it opens, so it stays out of the page's first
// load; the buttons that open it ask for it as soon as the pointer or focus reaches them.
const loadLogin = () => import("./login-dialog");
const LazyLoginDialog = dynamic(() => loadLogin().then((module) => module.LoginDialog), { ssr: false });
const preloadLogin = () => void loadLogin();

const NARROW_HIDDEN = {
  projects: "hidden lg:table-cell",
  workers: "hidden xl:table-cell",
  usage: "hidden md:table-cell",
  runs: "hidden lg:table-cell",
  cost: "hidden lg:table-cell",
  heldBy: "hidden sm:table-cell",
};

function AccountsTable({
  accounts,
  now,
  onEdit,
  onPause,
  onRenew,
}: {
  accounts: Account[];
  now: number;
  onEdit: (account: Account) => void;
  onPause: (account: Account) => void;
  onRenew: (account: Account) => void;
}) {
  const t = useTranslations("accounts");
  const format = useFormatter();
  const columns = useMemo(() => {
    const usd = (value: number) => format.number(value, { style: "currency", currency: "USD" });
    const helper = dataTableColumns<Account>();
    return helper.columns([
      helper.accessor("name", {
        header: () => t("columns.account"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => {
          const account = info.row.original;
          const rank = t("rank", { weight: account.weight, priority: account.priority });
          return (
            <CellMain sub={`${account.env_var} · ${rank}`} subTitle={`${account.env_var} · ${rank}`} subTestId="account-target">
              <span className="truncate font-mono text-[13px]" title={account.name} data-account-name={account.name}>
                {account.name}
              </span>
            </CellMain>
          );
        },
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
      helper.display({ id: "usage", header: () => t("columns.usage"), cell: (info) => <UsageCell account={info.row.original} /> }),
      helper.accessor("runs_7d", {
        id: "runs",
        header: () => t("columns.runs"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => {
          const { runs_24h: day, runs_7d: week } = info.row.original;
          return (
            <span title={t("runsTitle", { day, week })} data-testid="account-runs">
              {day} / {week}
            </span>
          );
        },
      }),
      helper.accessor("cost_7d_usd", {
        id: "cost",
        header: () => t("columns.cost"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => {
          const day = usd(info.row.original.cost_24h_usd);
          const week = usd(info.row.original.cost_7d_usd);
          return (
            <span title={t("costTitle", { day, week })} data-testid="account-cost">
              {day} / {week}
            </span>
          );
        },
      }),
      helper.display({
        id: "heldBy",
        header: () => t("columns.heldBy"),
        cell: (info) => {
          const held = info.row.original.held_by;
          if (held.length === 0) return <span className="text-fg-subtle">{t("notHeld")}</span>;
          return (
            <span className="inline-flex flex-wrap gap-x-2" data-testid="account-held-by">
              {held.map((run) => (
                <RunRef
                  key={run.run_id}
                  id={run.run_id}
                  href={`/p/${encodeURIComponent(run.project)}/runs/${run.run_id}` as Route}
                  label={t("heldLabel", { id: run.run_id, project: run.project })}
                  data-run-id={run.run_id}
                />
              ))}
            </span>
          );
        },
      }),
      helper.display({
        id: "actions",
        header: () => <span className="sr-only">{t("columns.actions")}</span>,
        meta: { actions: true },
        cell: (info) => {
          const account = info.row.original;
          const paused = account.paused_at !== null;
          return (
            <>
              {isClaudeAccount(account) ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  aria-label={t("renewLabel", { name: account.name })}
                  onClick={() => onRenew(account)}
                  onPointerEnter={preloadLogin}
                  onFocus={preloadLogin}
                  data-testid="account-renew"
                >
                  <RefreshCw aria-hidden="true" />
                  <span className="hidden sm:inline">{t("renew")}</span>
                </Button>
              ) : null}
              <Button
                type="button"
                variant="ghost"
                size="sm"
                aria-label={t("editLabel", { name: account.name })}
                onClick={() => onEdit(account)}
                data-testid="account-edit"
              >
                <Pencil aria-hidden="true" />
                <span className="hidden sm:inline">{t("edit")}</span>
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                aria-label={t(paused ? "resumeLabel" : "pauseLabel", { name: account.name })}
                onClick={() => onPause(account)}
                data-testid={paused ? "account-resume" : "account-pause"}
              >
                {paused ? <Play aria-hidden="true" /> : <Pause aria-hidden="true" />}
                <span className="hidden sm:inline">{t(paused ? "resume" : "pause")}</span>
              </Button>
            </>
          );
        },
      }),
    ]);
  }, [t, format, now, onEdit, onPause, onRenew]);

  return (
    <DataTable
      data={accounts}
      columns={columns}
      caption={t("caption")}
      getRowId={(row) => row.name}
      initialSorting={[{ id: "name", desc: false }]}
      columnClassNames={NARROW_HIDDEN}
      testId="accounts-table"
    />
  );
}

/** The policies under the accounts, from the projects the accounts are bound to and the policies the visitor set. */
function Policies({ accounts }: { accounts: Account[] }) {
  const t = useTranslations("accounts.policies");
  const policies = useQuery(policiesQuery(browserApi));
  if (policies.isPending) return <TableSkeleton rows={2} />;
  if (policies.isError) {
    return (
      <p className="text-sm text-danger" role="alert" data-testid="account-policies-error">
        {t("loadFailed")}
      </p>
    );
  }
  return <PolicySection rows={policyRows(accounts, policies.data)} />;
}

/** The accounts whose token ends within 30 days, soonest first, each with its day and a way to renew it. */
function RenewNotice({ accounts, now, onRenew }: { accounts: Account[]; now: number; onRenew: (account: Account) => void }) {
  const t = useTranslations("accounts.renewNotice");
  const due = accounts
    .flatMap((account) => {
      const when = isClaudeAccount(account) && account.state !== "expired" ? renewalDue(account, now) : null;
      return when ? [{ account, when }] : [];
    })
    .sort((a, b) => a.when.at.localeCompare(b.when.at));
  if (due.length === 0) return null;
  return (
    <section
      aria-labelledby="accounts-renew-title"
      className="flex items-start gap-2.5 rounded-md border border-attention/20 bg-attention-soft px-3 py-2.5 text-sm text-attention"
      data-testid="accounts-renew-notice"
    >
      <CalendarClock className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div className="flex min-w-0 flex-col gap-1.5">
        <h2 id="accounts-renew-title" className="font-medium">
          {t("title", { count: due.length })}
        </h2>
        <ul className="flex flex-col gap-1">
          {due.map(({ account, when }) => (
            <li key={account.name} className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="font-mono text-[13px] [overflow-wrap:anywhere]">{account.name}</span>
              <span>{t.rich("ends", { day: () => <Day value={when.at} /> })}</span>
              <Button
                type="button"
                variant="link"
                size="sm"
                className="h-auto px-0 text-attention underline underline-offset-4"
                onClick={() => onRenew(account)}
                onPointerEnter={preloadLogin}
                onFocus={preloadLogin}
                aria-label={t("renewLabel", { name: account.name })}
                data-testid="accounts-renew-notice-renew"
              >
                {t("renew")}
              </Button>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

/**
 * The visitor's accounts (docs/credentials.md, section "Accounts"): each one's variable, bindings, state, the last
 * usage a run reported, its runs and their estimated cost over 24 hours and 7 days, and the runs holding it now; Renew
 * token (a Claude account's), Pause or Resume and Edit (weight and priority) on each, and a reminder for each token
 * that ends within 30 days; then the rotation policy of each project. Add Claude account opens the same dialog as
 * Renew token, empty. Never a value.
 */
export function AccountsPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("accounts");
  const state = useHubQuery(accountListQuery(browserApi), initialError);
  const accounts = state.status === "success" ? state.data : null;
  const [editing, setEditing] = useState<{ open: boolean; account: Account | null }>({ open: false, account: null });
  // Null until the dialog first opens: it mounts, and loads, only then.
  const [login, setLogin] = useState<{ open: boolean; account: Account | null } | null>(null);
  const [now] = useState(() => Date.now());
  const failureOf = useFailure();
  const { mutateAsync } = useChangeAccount();

  const edit = useCallback((account: Account) => setEditing({ open: true, account }), []);
  const add = () => setLogin({ open: true, account: null });
  const renew = useCallback((account: Account) => setLogin({ open: true, account }), []);
  const pause = useCallback(
    async (account: Account) => {
      const paused = account.paused_at === null;
      const name = account.name;
      try {
        await mutateAsync({ name, body: { paused } });
      } catch (error) {
        notifyFailure(t(paused ? "pauseFailed" : "resumeFailed", { name }), failureOf(error));
        return;
      }
      notify({ tone: "success", text: t(paused ? "paused" : "resumed", { name }), description: t(paused ? "pausedText" : "resumedText") });
    },
    [mutateAsync, failureOf, t],
  );
  const onPause = useCallback((account: Account) => void pause(account), [pause]);

  return (
    <>
      <PageHeader
        title={t("title")}
        tags={accounts ? <Badge variant="secondary">{t("count", { count: accounts.length })}</Badge> : null}
        actions={
          <>
            <Button asChild variant="outline">
              <Link href={SECRETS_HREF} data-testid="accounts-secrets-link">
                {t("secrets")}
                <ChevronRight aria-hidden="true" />
              </Link>
            </Button>
            <Button onClick={add} onPointerEnter={preloadLogin} onFocus={preloadLogin} data-testid="accounts-add">
              <UserRoundPlus aria-hidden="true" />
              {t("add")}
            </Button>
          </>
        }
        sub={accounts && accounts.length > 0 ? <span data-testid="accounts-list-summary">{t("listSummary")}</span> : null}
      />
      <QueryView state={state} loading={<TableSkeleton rows={4} />}>
        {(list) =>
          list.length === 0 ? (
            <EmptyState
              icon={UserRoundCog}
              title={t("empty.title")}
              description={t.rich("empty.description", {
                code: (chunks) => <code className="rounded-xs bg-muted px-1 font-mono text-xs text-foreground">{chunks}</code>,
              })}
            >
              <Button onClick={add} onPointerEnter={preloadLogin} onFocus={preloadLogin} data-testid="accounts-empty-add">
                <UserRoundPlus aria-hidden="true" />
                {t("add")}
              </Button>
            </EmptyState>
          ) : (
            <div className="flex flex-col gap-8">
              <div className="flex flex-col gap-4">
                <RenewNotice accounts={list} now={now} onRenew={renew} />
                <section aria-labelledby="accounts-list-title">
                  <h2 id="accounts-list-title" className="sr-only">
                    {t("caption")}
                  </h2>
                  <DataCard>
                    <AccountsTable accounts={list} now={now} onEdit={edit} onPause={onPause} onRenew={renew} />
                  </DataCard>
                </section>
              </div>
              <Policies accounts={list} />
            </div>
          )
        }
      </QueryView>
      {login ? (
        <LazyLoginDialog
          open={login.open}
          onOpenChange={(open) => setLogin((current) => (current ? { ...current, open } : current))}
          account={login.account}
          accounts={accounts ?? []}
        />
      ) : null}
      <AccountDialog
        account={editing.account}
        open={editing.open}
        onOpenChange={(open) => setEditing((current) => ({ ...current, open }))}
      />
    </>
  );
}
