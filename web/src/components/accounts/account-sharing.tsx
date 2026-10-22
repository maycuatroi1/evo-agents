"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useCallback, useMemo } from "react";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { type CredentialShare, liveShares, type ShareTarget } from "@/components/sharing/model";
import { SharesSection } from "@/components/sharing/shares-section";
import { Badge } from "@/components/ui/badge";
import { browserApi } from "@/lib/api/browser";
import type { components } from "@/lib/api/schema";
import { whoamiQuery } from "@/lib/queries";

import { type Account, accountKeys } from "./queries";
import { usageWindows } from "./usage-model";

type PrincipalCost = components["schemas"]["PrincipalCost"];
type CostRow = PrincipalCost & { key: string; account: string };

/** The highest usage of an account's windows not reset at `now`, in percent; null when it reported none. */
export function peakPercent(account: Pick<Account, "last_usage">, now: number): number | null {
  const shares = usageWindows(account.last_usage, now).flatMap((window) => (window.percent === null ? [] : [window.percent]));
  return shares.length > 0 ? Math.max(...shares) : null;
}

/** Whether the account behind `share` is at or above the share's usage ceiling: shared runs then wait for it. */
export function overCeiling(account: Pick<Account, "last_usage"> | undefined, share: CredentialShare, now: number): boolean {
  if (!account || share.usage_ceiling_pct === null) return false;
  const peak = peakPercent(account, now);
  return peak !== null && peak >= share.usage_ceiling_pct;
}

/** What an account may be shared as: one bound to a project, a Claude subscription asking for the warning. */
export const shareTargets = (accounts: Account[]): ShareTarget[] =>
  accounts
    .filter((account) => account.projects.length > 0)
    .map((account) => ({ name: account.name, projects: account.projects, subscription: account.provider === "claude" }));

/** Each account's runs and cost by the member they were made for, the visitor's own rows left out. */
export function costRows(accounts: Account[], me: string | null): CostRow[] {
  return accounts.flatMap((account) =>
    account.cost_by_principal
      .filter((row) => row.principal !== me)
      .map((row) => ({ ...row, account: account.name, key: `${account.name}:${row.principal ?? ""}` })),
  );
}

const NARROW_HIDDEN = { day: "hidden md:table-cell", week: "hidden sm:table-cell" };

function CostTable({ rows }: { rows: CostRow[] }) {
  const t = useTranslations("accounts.costByPrincipal");
  const format = useFormatter();
  const columns = useMemo(() => {
    const usd = (value: number) => format.number(value, { style: "currency", currency: "USD" });
    const helper = dataTableColumns<CostRow>();
    return helper.columns([
      helper.accessor((row) => row.principal ?? "", {
        id: "principal",
        header: () => t("columns.principal"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => (
          <CellMain sub={<span className="font-mono">{info.row.original.account}</span>}>
            <span className="truncate" data-testid="cost-principal">
              {info.row.original.principal ?? t("gone")}
            </span>
          </CellMain>
        ),
      }),
      helper.accessor("cost_24h_usd", { id: "day", header: () => t("columns.day"), sortFn: "basic", meta: { numeric: true }, cell: (info) => usd(info.getValue()) }),
      helper.accessor("cost_7d_usd", { id: "week", header: () => t("columns.week"), sortFn: "basic", meta: { numeric: true }, cell: (info) => usd(info.getValue()) }),
      helper.accessor("cost_month_usd", {
        id: "month",
        header: () => t("columns.month"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => <span data-testid="cost-month">{usd(info.getValue())}</span>,
      }),
      helper.accessor("runs_month", {
        id: "runs",
        header: () => t("columns.runs"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => t("runs", { day: info.row.original.runs_24h, week: info.row.original.runs_7d, month: info.getValue() }),
      }),
    ]);
  }, [t, format]);
  return (
    <DataTable
      data={rows}
      columns={columns}
      caption={t("caption")}
      getRowId={(row) => row.key}
      initialSorting={[{ id: "month", desc: true }]}
      columnClassNames={NARROW_HIDDEN}
      testId="cost-by-principal-table"
    />
  );
}

/**
 * What the visitor's accounts are shared as, and what the members they are shared with spent with them. The hub
 * answers a member an account is shared with with their own row alone (GET /v1/accounts/{name}?owner=); here the
 * visitor is the owner and reads every member's row but their own, which the account's row already shows.
 */
function CostByPrincipal({ accounts, shared }: { accounts: Account[]; shared: boolean }) {
  const t = useTranslations("accounts.costByPrincipal");
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const rows = me ? costRows(accounts, me.login) : [];
  if (!shared && rows.length === 0) return null;
  return (
    <section aria-labelledby="accounts-cost-title" className="flex flex-col gap-3" data-testid="accounts-cost-by-principal">
      <div className="flex flex-col gap-1">
        <h2 id="accounts-cost-title" className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        <p className="text-sm text-pretty text-muted-foreground">{t("description")}</p>
      </div>
      {rows.length === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-2.5 text-sm text-muted-foreground" data-testid="accounts-cost-empty">
          {t("empty")}
        </p>
      ) : (
        <DataCard>
          <CostTable rows={rows} />
        </DataCard>
      )}
    </section>
  );
}

/** The shares of the visitor's accounts, then what the members they went to spent, once there is a share or a cost. */
export function AccountSharing({ accounts, now }: { accounts: Account[]; now: number }) {
  const t = useTranslations("accounts.sharing");
  const queryClient = useQueryClient();
  const shares = useMemo(() => liveShares(accounts), [accounts]);
  const targets = useMemo(() => shareTargets(accounts), [accounts]);
  const byName = useMemo(() => new Map(accounts.map((account) => [account.name, account])), [accounts]);
  const onChanged = useCallback(
    () => Promise.all([queryClient.invalidateQueries({ queryKey: accountKeys.all }), queryClient.invalidateQueries({ queryKey: ["secrets"] })]),
    [queryClient],
  );
  const extra = useCallback(
    (share: CredentialShare) =>
      overCeiling(byName.get(share.secret), share, now) ? (
        <Badge variant="warning" data-testid="share-over-ceiling">
          {t("overCeiling")}
        </Badge>
      ) : null,
    [byName, now, t],
  );
  return (
    <>
      <SharesSection targets={targets} shares={shares} onChanged={onChanged} extra={extra} now={now} testId="account-shares" />
      <CostByPrincipal accounts={accounts} shared={shares.length > 0} />
    </>
  );
}
