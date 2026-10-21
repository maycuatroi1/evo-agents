"use client";

import { ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useMemo, useState } from "react";

import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { cn } from "@/lib/utils";

import { StateCell } from "./account-cells";
import { AccountCell, type Actions, ActionButtons, HeldByCell, LastUsedCell, NARROW_HIDDEN, RunsCell } from "./account-columns";
import { entryAccount, lastLeased, type ServiceEntry } from "./model";
import { ProviderLogo } from "./provider-logo";

/**
 * The service credentials: the tokens and keys a run uses for something other than the model. A row is one account,
 * or, for the accounts of one provider bound to the same projects, one line that opens into its variables, each with
 * its own Edit and Pause. No cost and no usage bars: a service account has neither.
 */
type Props = { entries: ServiceEntry[]; shared: Set<string>; now: number } & Actions;

/** The first cell of an entry with several variables: its provider, how many, and the button that opens them. */
function GroupCell({ entry, open, onToggle }: { entry: ServiceEntry; open: boolean; onToggle: () => void }) {
  const t = useTranslations("accounts");
  const name = t(`providers.${entry.provider}`);
  const sub = entry.accounts.map((account) => account.env_var).join(" · ");
  return (
    <div className="flex min-w-0 items-center gap-2.5">
      <ProviderLogo provider={entry.provider} />
      <CellMain sub={sub} subTitle={sub} subTestId="account-target">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          aria-controls={`service-${entry.key}`}
          className="-ml-1 inline-flex min-w-0 items-center gap-1 rounded-sm px-1 text-left"
          data-testid="service-group-toggle"
          data-provider={entry.provider}
        >
          <ChevronRight className={cn("size-4 shrink-0 transition-transform", open && "rotate-90")} aria-hidden="true" />
          <span className="truncate">{t("variables", { provider: name, count: entry.accounts.length })}</span>
        </button>
      </CellMain>
    </div>
  );
}

/** The variables of an open entry, each as the row of one account. */
function Variables({ entry, shared, now, ...actions }: { entry: ServiceEntry; shared: Set<string>; now: number } & Actions) {
  const t = useTranslations("accounts");
  return (
    <ul id={`service-${entry.key}`} aria-label={t("variablesLabel", { provider: t(`providers.${entry.provider}`) })} className="flex flex-col divide-y border-t bg-surface-sunken" data-testid="service-variables">
      {entry.accounts.map((account) => (
        <li key={account.name} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 py-1.5 pr-2 pl-12">
          <div className="min-w-0 flex-1 basis-48">
            <AccountCell account={account} ranked={shared.has(account.env_var)} />
          </div>
          <StateCell account={account} now={now} />
          <span className="text-xs text-fg-subtle">
            {t("columns.lastUsed")} <LastUsedCell at={account.last_leased_at} />
          </span>
          <HeldByCell held={account.held_by} />
          <span className="ml-auto flex items-center gap-0.5">
            <ActionButtons account={account} {...actions} />
          </span>
        </li>
      ))}
    </ul>
  );
}

export function ServiceTable({ entries, shared, now, onEdit, onPause, onRenew }: Props) {
  const t = useTranslations("accounts");
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const toggle = useCallback(
    (key: string) =>
      setOpen((current) => {
        const next = new Set(current);
        if (!next.delete(key)) next.add(key);
        return next;
      }),
    [],
  );
  const columns = useMemo(() => {
    const helper = dataTableColumns<ServiceEntry>();
    const only = (entry: ServiceEntry) => (entry.accounts.length === 1 ? entry.accounts[0] : null);
    return helper.columns([
      helper.accessor((entry) => entry.accounts[0].name, {
        id: "name",
        header: () => t("columns.account"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => {
          const entry = info.row.original;
          const account = only(entry);
          return account ? (
            <AccountCell account={account} ranked={shared.has(account.env_var)} />
          ) : (
            <GroupCell entry={entry} open={open.has(entry.key)} onToggle={() => toggle(entry.key)} />
          );
        },
      }),
      helper.display({
        id: "state",
        header: () => t("columns.state"),
        cell: (info) => <StateCell account={entryAccount(info.row.original)} now={now} />,
      }),
      helper.display({
        id: "projects",
        header: () => t("columns.projects"),
        cell: (info) => <span className="font-mono text-xs">{info.row.original.projects.join(", ") || "-"}</span>,
      }),
      helper.accessor((entry) => entry.accounts.reduce((sum, account) => sum + account.runs_7d, 0), {
        id: "runs",
        header: () => t("columns.runs"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => {
          const { accounts } = info.row.original;
          return <RunsCell day={accounts.reduce((sum, a) => sum + a.runs_24h, 0)} week={accounts.reduce((sum, a) => sum + a.runs_7d, 0)} />;
        },
      }),
      helper.display({
        id: "lastUsed",
        header: () => t("columns.lastUsed"),
        cell: (info) => <LastUsedCell at={lastLeased(info.row.original.accounts)} />,
      }),
      helper.display({
        id: "heldBy",
        header: () => t("columns.heldBy"),
        cell: (info) => <HeldByCell held={info.row.original.accounts.flatMap((account) => account.held_by)} />,
      }),
      helper.display({
        id: "actions",
        header: () => <span className="sr-only">{t("columns.actions")}</span>,
        meta: { actions: true },
        cell: (info) => {
          const account = only(info.row.original);
          return account ? <ActionButtons account={account} onEdit={onEdit} onPause={onPause} onRenew={onRenew} /> : null;
        },
      }),
    ]);
  }, [t, shared, now, open, toggle, onEdit, onPause, onRenew]);
  const detail = useCallback(
    (entry: ServiceEntry) =>
      entry.accounts.length > 1 && open.has(entry.key) ? (
        <Variables entry={entry} shared={shared} now={now} onEdit={onEdit} onPause={onPause} onRenew={onRenew} />
      ) : null,
    [open, shared, now, onEdit, onPause, onRenew],
  );
  return (
    <DataTable
      data={entries}
      columns={columns}
      caption={t("groups.service.title")}
      getRowId={(entry) => entry.key}
      initialSorting={[{ id: "name", desc: false }]}
      columnClassNames={NARROW_HIDDEN}
      detail={detail}
      testId="services-table"
    />
  );
}
