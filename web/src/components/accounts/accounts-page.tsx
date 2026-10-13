"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronRight, CircleCheck, CirclePause, Hourglass, Pause, Pencil, Play, Snowflake, UserRoundCog } from "lucide-react";
import type { Route } from "next";
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

import { AccountDialog } from "./account-dialog";
import { useChangeAccount, useFailure } from "./hooks";
import { policyRows, usagePercent, usageResetsAt } from "./model";
import { type Account, accountListQuery, type AccountState, policiesQuery } from "./queries";
import { PolicySection } from "./policy-section";

const SECRETS_HREF = "/secrets" as Route;

const NARROW_HIDDEN = {
  projects: "hidden lg:table-cell",
  workers: "hidden xl:table-cell",
  usage: "hidden md:table-cell",
  runs: "hidden lg:table-cell",
  cost: "hidden lg:table-cell",
  heldBy: "hidden sm:table-cell",
};

const STATES: Record<AccountState, { icon: typeof CircleCheck; variant: "success" | "warning" | "secondary" | "destructive" }> = {
  ready: { icon: CircleCheck, variant: "success" },
  cooling: { icon: Snowflake, variant: "warning" },
  paused: { icon: CirclePause, variant: "secondary" },
  expired: { icon: Hourglass, variant: "destructive" },
};

/** A time in the visitor's short format, the full one as its tooltip. */
function At({ value }: { value: string }) {
  const format = useFormatter();
  const date = new Date(value);
  return (
    <time dateTime={value} title={format.dateTime(date, { dateStyle: "medium", timeStyle: "short" })} className="whitespace-nowrap tabular-nums">
      {format.dateTime(date, { dateStyle: "short", timeStyle: "short" })}
    </time>
  );
}

/** Ready, cooling until when, paused or expired, as the hub read it when it answered. */
function StateCell({ account }: { account: Account }) {
  const t = useTranslations("accounts");
  const { icon: Icon, variant } = STATES[account.state];
  return (
    <span className="inline-flex flex-col items-start gap-0.5">
      <Badge variant={variant} data-testid="account-state" data-state={account.state}>
        <Icon aria-hidden="true" />
        {t(`state.${account.state}`)}
      </Badge>
      {account.state === "cooling" && account.cooldown_until ? (
        <span className="text-xs text-fg-subtle" title={account.cooldown_reason ?? undefined} data-testid="account-cooling-until">
          {t.rich("until", { time: () => <At value={account.cooldown_until ?? ""} /> })}
        </span>
      ) : null}
    </span>
  );
}

/** The last usage a run's CLI reported for the account: the share of its limit used, what it said, when it resets. */
function UsageCell({ account }: { account: Account }) {
  const t = useTranslations("accounts.usage");
  const usage = account.last_usage;
  if (!usage) return <span className="text-fg-subtle">{t("none")}</span>;
  const percent = usagePercent(usage);
  const resets = usageResetsAt(usage);
  return (
    <span className="inline-flex flex-col items-start gap-0.5" data-testid="account-usage" data-status={usage.status}>
      <span className={usage.status === "allowed" ? "text-foreground" : "font-medium text-attention"}>
        {percent === null ? t(`status.${usage.status}`) : t("percent", { percent })}
      </span>
      <span className="text-xs text-fg-subtle">
        {account.last_usage_at ? t.rich("reported", { time: () => <At value={account.last_usage_at ?? ""} /> }) : null}
        {resets ? <> · {t.rich("resets", { time: () => <At value={resets} /> })}</> : null}
      </span>
    </span>
  );
}

function AccountsTable({
  accounts,
  onEdit,
  onPause,
}: {
  accounts: Account[];
  onEdit: (account: Account) => void;
  onPause: (account: Account) => void;
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
      helper.accessor("state", { header: () => t("columns.state"), sortFn: "text", cell: (info) => <StateCell account={info.row.original} /> }),
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
  }, [t, format, onEdit, onPause]);

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

/**
 * The visitor's accounts (docs/credentials.md, section "Accounts"): each one's variable, bindings, state, the last
 * usage a run reported, its runs and their estimated cost over 24 hours and 7 days, and the runs holding it now; Pause
 * or Resume and Edit (weight and priority) on each; then the rotation policy of each project. Never a value.
 */
export function AccountsPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("accounts");
  const state = useHubQuery(accountListQuery(browserApi), initialError);
  const accounts = state.status === "success" ? state.data : null;
  const [editing, setEditing] = useState<{ open: boolean; account: Account | null }>({ open: false, account: null });
  const failureOf = useFailure();
  const { mutateAsync } = useChangeAccount();

  const edit = useCallback((account: Account) => setEditing({ open: true, account }), []);
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
          <Button asChild variant="outline">
            <Link href={SECRETS_HREF} data-testid="accounts-secrets-link">
              {t("secrets")}
              <ChevronRight aria-hidden="true" />
            </Link>
          </Button>
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
            />
          ) : (
            <div className="flex flex-col gap-8">
              <section aria-labelledby="accounts-list-title">
                <h2 id="accounts-list-title" className="sr-only">
                  {t("caption")}
                </h2>
                <DataCard>
                  <AccountsTable accounts={list} onEdit={edit} onPause={onPause} />
                </DataCard>
              </section>
              <Policies accounts={list} />
            </div>
          )
        }
      </QueryView>
      <AccountDialog
        account={editing.account}
        open={editing.open}
        onOpenChange={(open) => setEditing((current) => ({ ...current, open }))}
      />
    </>
  );
}
