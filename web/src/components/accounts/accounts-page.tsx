"use client";

import { useQuery } from "@tanstack/react-query";
import { CalendarClock, ChevronRight, UserRoundCog, UserRoundPlus } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";

import { notify, notifyFailure } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type Actions } from "./account-columns";
import { AccountGroup } from "./account-group";
import { Day } from "./account-cells";
import { AccountDialog } from "./account-dialog";
import { useChangeAccount, useFailure } from "./hooks";
import { LazyLoginDialog, preloadLogin } from "./login-lazy";
import { groupAccounts, isClaudeAccount, policyRows, renewalDue, serviceEntries, sharedVariables } from "./model";
import { ModelTable } from "./model-table";
import { type Account, accountListQuery, policiesQuery } from "./queries";
import { PolicySection } from "./policy-section";
import { ServiceTable } from "./service-table";

const SECRETS_HREF = "/secrets" as Route;

/** The groups of accounts, each with its own table: model accounts, then service credentials. */
function Groups({ accounts, now, ...actions }: { accounts: Account[]; now: number } & Actions) {
  const groups = useMemo(() => groupAccounts(accounts), [accounts]);
  const shared = useMemo(() => sharedVariables(accounts), [accounts]);
  return groups.map(({ id, accounts: members }) => (
    <AccountGroup key={id} id={id} count={members.length}>
      {id === "model" ? (
        <ModelTable accounts={members} shared={shared} now={now} {...actions} />
      ) : (
        <ServiceTable entries={serviceEntries(members)} shared={shared} now={now} {...actions} />
      )}
    </AccountGroup>
  ));
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
 * The visitor's accounts (docs/credentials.md, section "Accounts"): each one's variable, bindings, state, its runs and
 * their estimated cost over 24 hours and 7 days, and the runs holding it now, with its usage limits as bars under its
 * row (a Claude account's, or any a run reported); Renew
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

  const actions = { onEdit: edit, onPause, onRenew: renew };

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
                <Groups accounts={list} now={now} {...actions} />
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
