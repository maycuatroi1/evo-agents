"use client";

import { useQuery } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo } from "react";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { Badge } from "@/components/ui/badge";
import { browserApi } from "@/lib/api/browser";

import { Day, StateCell } from "./account-cells";
import { CostCell, RunsCell } from "./account-columns";
import { ProviderLogo } from "./provider-logo";
import { type SharedAccount, sharedAccountListQuery } from "./queries";

const NARROW_HIDDEN = { projects: "hidden lg:table-cell", terms: "hidden md:table-cell", runs: "hidden lg:table-cell", cost: "hidden lg:table-cell" };

/** What the shares that lend an account to the visitor allow, in one line: their limits, else "no limit". */
function useTerms() {
  const t = useTranslations("sharing.list");
  const format = useFormatter();
  return (account: SharedAccount): string => {
    const usd = (value: number) => format.number(value, { style: "currency", currency: "USD" });
    const parts = account.shares.flatMap((share) => [
      share.level ? t("level", { level: share.level }) : null,
      share.budget_usd !== null ? t("budget", { usd: usd(share.budget_usd) }) : null,
      share.usage_ceiling_pct !== null ? t("ceiling", { pct: share.usage_ceiling_pct }) : null,
      share.max_running !== null ? t("running", { count: share.max_running }) : null,
    ]);
    const kept = [...new Set(parts.filter((part): part is string => part !== null))];
    return kept.length > 0 ? kept.join(", ") : t("noLimit");
  };
}

/** The share of `account` that ends first, if any ends. */
const firstEnd = (account: SharedAccount): string | null =>
  account.shares.flatMap((share) => (share.expires_at ? [share.expires_at] : [])).sort()[0] ?? null;

function SharedTable({ accounts, now }: { accounts: SharedAccount[]; now: number }) {
  const t = useTranslations("accounts.sharedWithYou");
  const tColumns = useTranslations("accounts.columns");
  const tList = useTranslations("sharing.list");
  const terms = useTerms();
  const columns = useMemo(() => {
    const helper = dataTableColumns<SharedAccount>();
    return helper.columns([
      helper.accessor("name", {
        id: "account",
        header: () => tColumns("account"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => {
          const account = info.row.original;
          const sub = t("ownedBy", { owner: account.owner, variable: account.env_var });
          return (
            <div className="flex min-w-48 items-center gap-2.5 sm:min-w-60">
              <ProviderLogo provider={account.provider} />
              <CellMain sub={sub} subTitle={sub}>
                <span className="min-w-0 font-mono text-[13px] [overflow-wrap:anywhere]" title={account.pick} data-account-name={account.name}>
                  {account.name}
                </span>
              </CellMain>
            </div>
          );
        },
      }),
      helper.accessor("owner", {
        id: "owner",
        header: () => t("owner"),
        sortFn: "alphanumeric",
        cell: (info) => (
          <Badge variant="info" data-testid="shared-account-owner">
            {info.getValue()}
          </Badge>
        ),
      }),
      helper.accessor((row) => row.projects.join(", "), {
        id: "projects",
        header: () => tColumns("projects"),
        sortFn: "alphanumeric",
        cell: (info) => <span className="[overflow-wrap:anywhere]">{info.getValue()}</span>,
      }),
      helper.accessor("state", {
        id: "state",
        header: () => tColumns("state"),
        sortFn: "alphanumeric",
        cell: (info) => <StateCell account={info.row.original} now={now} />,
      }),
      helper.accessor((row) => terms(row), {
        id: "terms",
        header: () => t("terms"),
        enableSorting: false,
        cell: (info) => {
          const ends = firstEnd(info.row.original);
          return (
            <span className="inline-flex flex-col gap-0.5 text-sm" data-testid="shared-account-terms">
              <span>{info.getValue()}</span>
              {ends ? <span className="text-xs text-fg-subtle">{tList.rich("ends", { day: () => <Day value={ends} /> })}</span> : null}
            </span>
          );
        },
      }),
      helper.accessor("runs_7d", {
        id: "runs",
        header: () => t("yourRuns"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => <RunsCell day={info.row.original.runs_24h} week={info.getValue()} />,
      }),
      helper.accessor((row) => row.cost_7d_usd ?? -1, {
        id: "cost",
        header: () => t("yourCost"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => <CostCell account={info.row.original} />,
      }),
    ]);
  }, [t, tColumns, tList, terms, now]);
  return (
    <DataTable
      data={accounts}
      columns={columns}
      caption={t("caption")}
      getRowId={(row) => row.pick}
      initialSorting={[{ id: "owner", desc: false }]}
      columnClassNames={NARROW_HIDDEN}
      testId="shared-accounts-table"
    />
  );
}

/**
 * The accounts other members share with the visitor (GET /v1/shared-accounts), each with its owner, the projects its
 * shares lend it on, its state, what those shares allow, and the visitor's own runs and cost with it. With
 * `hideEmpty` nothing shows while none is shared, as on the Providers tab; the Sharing tab says so instead.
 */
export function SharedWithYou({ now, hideEmpty = false }: { now: number; hideEmpty?: boolean }) {
  const t = useTranslations("accounts.sharedWithYou");
  const { data, isError } = useQuery(sharedAccountListQuery(browserApi));
  const accounts = data ?? [];
  if (hideEmpty && accounts.length === 0) return null;
  return (
    <section aria-labelledby="accounts-shared-with-you-title" className="flex flex-col gap-3" data-testid="accounts-shared-with-you">
      <div className="flex min-w-0 flex-col gap-1">
        <h2 id="accounts-shared-with-you-title" className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        <p className="text-sm text-pretty text-muted-foreground">{t("description")}</p>
      </div>
      {accounts.length === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid="shared-accounts-empty">
          {isError ? t("loadFailed") : t("empty")}
        </p>
      ) : (
        <DataCard>
          <SharedTable accounts={accounts} now={now} />
        </DataCard>
      )}
    </section>
  );
}
